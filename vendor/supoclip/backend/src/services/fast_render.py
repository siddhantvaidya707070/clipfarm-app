"""Single-pass 720p/30 fast exports with native editable source/caption sidecars."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import json
import logging
import subprocess
import threading
import time
import uuid

from ..clip_source_map import normalize_source_ranges, save_clip_source_ranges, save_clip_broll, save_clip_caption_settings
from ..media.captions import build_assemblyai_ass_subtitles
from ..media.ffmpeg import ffprobe_video_size, subtitles_filter_fragment
from ..media.timeline import build_keep_ranges_from_source_ranges, get_words_for_keep_ranges
from ..media.transcription import load_cached_transcript_data

logger = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def _source_size(path: str, modified: int):
    return ffprobe_video_size(Path(path))


def _run_export(command: list[str], stop: threading.Event | None = None):
    if stop and stop.is_set():
        raise InterruptedError('Task cancelled')
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 120
    try:
        while True:
            try:
                _, errors = process.communicate(timeout=.25)
                if process.returncode:
                    logger.warning('Fast clip encoding failed: %s', errors.decode(errors='replace')[-1000:])
                    raise RuntimeError('Fast clip encoding failed')
                return
            except subprocess.TimeoutExpired:
                if (stop and stop.is_set()) or time.monotonic() >= deadline:
                    raise InterruptedError('Task cancelled or fast clip encoding timed out')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _srt(words):
    def stamp(seconds):
        ms = max(0, round(seconds * 1000))
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        secs, ms = divmod(ms, 1000)
        return f'{hours:02}:{minutes:02}:{secs:02},{ms:03}'
    chunks = [words[i:i + 6] for i in range(0, len(words), 6)]
    return '\n\n'.join(f"{i}\n{stamp(chunk[0]['start'])} --> {stamp(chunk[-1]['end'])}\n" +
                        ' '.join(w['text'] for w in chunk) for i, chunk in enumerate(chunks, 1)) + '\n'


def _validate_export(path: Path, expected: float) -> float:
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,duration,width,height', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Fast clip media validation failed')
    probe = json.loads(result.stdout)
    streams = probe.get('streams') or []
    if not {'video', 'audio'}.issubset({s.get('codec_type') for s in streams}):
        raise RuntimeError('Fast clip is missing video or audio')
    video = next(s for s in streams if s.get('codec_type') == 'video')
    if min(int(video.get('width') or 0), int(video.get('height') or 0)) < 720:
        raise RuntimeError('Fast clip is below the minimum output resolution')
    duration = float(probe.get('format', {}).get('duration') or 0)
    if duration <= 0 or abs(duration - expected) > .3:
        raise RuntimeError('Fast clip duration validation failed')
    av_durations = [float(s['duration']) for s in streams
                    if s.get('codec_type') in ('video', 'audio') and s.get('duration') not in (None, 'N/A')]
    if av_durations and max(av_durations) - min(av_durations) > .3:
        raise RuntimeError('Fast clip audio/video duration mismatch')
    return duration


def render_fast_clip(video_path: Path, segment: dict, clip_index: int, output_dir: Path, *,
                     output_format: str = 'vertical', add_subtitles: bool = True,
                     font_family=None, font_size=None, font_color=None, caption_template='default',
                     cleanup_settings: dict | None = None, max_clip_seconds: float = 60,
                     export_height: int = 720,
                     streamer_layout: dict | None = None,
                     stop: threading.Event | None = None) -> dict:
    """One decode/encode, hard-cut cleanup, static framing; original footage stays clean."""
    source = Path(video_path).resolve()
    source_ranges = normalize_source_ranges(segment.get('source_ranges') or segment.get('keep_ranges'))
    if not source_ranges:
        from ..media.render_ranges import segment_bounds
        source_ranges = [segment_bounds(segment)]
    keep = build_keep_ranges_from_source_ranges(source, source_ranges, cleanup_settings)
    clipped, remaining = [], max_clip_seconds
    for start, end in keep:
        if remaining <= .01:
            break
        end = min(end, start + remaining)
        if end > start:
            clipped.append((start, end))
            remaining -= end - start
    keep = clipped
    if not keep:
        raise ValueError('No playable fast highlight footage')
    offsets, duration = [], 0.0
    for start, end in keep:
        offsets.append(duration)
        duration += end - start
    data = load_cached_transcript_data(source) or {}
    words = get_words_for_keep_ranges(data, keep, offsets)
    width, height = _source_size(str(source), source.stat().st_mtime_ns)
    from .fast_webcam import validated_layout,layout_filter,save_clip_layout
    streamer_layout = validated_layout(streamer_layout) if output_format in {'vertical','vertical_split','vertical_streamer'} else None
    if output_format == 'vertical_streamer' and (streamer_layout is None or streamer_layout['top_fraction'] != .5):
        raise ValueError('vertical_streamer requires an explicit 50/50 streamer layout')
    export_height = max(720, min(1080, int(export_height)))
    if output_format in {'vertical','vertical_split','vertical_streamer'}:
        target_width, target_height = export_height, export_height * 16 // 9 // 2 * 2
        framing = f"crop=w='min(iw,ih*9/16)':h=ih:x='(iw-ow)/2':y=0,scale={target_width}:{target_height}:flags=fast_bilinear,setsar=1"
    else:
        ratio = export_height / min(width, height)
        target_width, target_height = int(width * ratio) // 2 * 2, int(height * ratio) // 2 * 2
        framing = f'scale={target_width}:{target_height}:flags=fast_bilinear,setsar=1'
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f'clip_{clip_index + 1}_fast_{uuid.uuid4().hex[:12]}.mp4'
    temporary = output.with_suffix('.partial.mp4')
    ass = output.with_suffix('.ass')
    report = {}
    filters = ['fps=30'] + ([] if streamer_layout else [framing])
    if add_subtitles and words:
        if build_assemblyai_ass_subtitles(source, keep[0][0], keep[-1][1], target_width, target_height, ass,
                font_family, font_size, font_color, caption_template, keep_ranges=keep, caption_words=words,
                output_offsets=offsets, caption_layout_report=report,
                **({'position_y_override':streamer_layout['top_fraction']+.07} if streamer_layout else {})):
            filters.append(subtitles_filter_fragment(ass, Path('/app/fonts') if Path('/app/fonts').is_dir() else None))
    seek, last = min(a for a, b in keep), max(b for a, b in keep)
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-threads', '1', '-filter_threads', '1',
               '-filter_complex_threads', '1', '-ss', f'{seek:.6f}', '-t', f'{last - seek:.6f}', '-i', str(source)]
    if len(keep) == 1 and not streamer_layout:
        command += ['-vf', ','.join(filters), '-map', '0:v:0', '-map', '0:a:0']
    elif len(keep) == 1:
        graph = layout_filter(streamer_layout,width,height,target_width,target_height)
        graph += ';[stacked]' + ','.join(filters) + '[framed]'
        command += ['-filter_complex',graph,'-map','[framed]','-map','0:a:0']
    else:
        count = len(keep)
        graph = [f"[0:v:0]split={count}" + ''.join(f'[vin{i}]' for i in range(count)),
                 f"[0:a:0]asplit={count}" + ''.join(f'[ain{i}]' for i in range(count))]
        for index, (start, end) in enumerate(keep):
            graph += [f'[vin{index}]trim=start={start - seek:.6f}:end={end - seek:.6f},setpts=PTS-STARTPTS[v{index}]',
                      f'[ain{index}]atrim=start={start - seek:.6f}:end={end - seek:.6f},asetpts=PTS-STARTPTS[a{index}]']
        graph.append(''.join(f'[v{i}][a{i}]' for i in range(count)) + f'concat=n={count}:v=1:a=1[video][audio]')
        if streamer_layout:
            graph.append(layout_filter(streamer_layout,width,height,target_width,target_height,input_label='video'))
            graph.append('[stacked]' + ','.join(filters) + '[framed]')
        else:
            graph.append('[video]' + ','.join(filters) + '[framed]')
        command += ['-filter_complex', ';'.join(graph), '-map', '[framed]', '-map', '[audio]']
    command += ['-t', f'{duration:.6f}', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '22', '-threads', '2',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(temporary)]
    try:
        _run_export(command, stop)
        actual_duration = _validate_export(temporary, duration)
        temporary.replace(output)
        save_clip_source_ranges(output, keep, offsets)
        save_clip_broll(output, [])
        if streamer_layout:
            save_clip_layout(output,streamer_layout)
        if report.get('caption_y') is not None:
            save_clip_caption_settings(output, {'position_y': report['caption_y'], 'position_is_center': True})
        relative = {**data, 'words': [{**w, 'start': round(w['start'] * 1000), 'end': round(w['end'] * 1000)} for w in words]}
        # Clip-specific sidecars are separate from the captioned delivery MP4.
        output.with_suffix('.transcript_cache.json').write_text(json.dumps(relative), encoding='utf-8')
        output.with_suffix('.srt').write_text(_srt(words), encoding='utf-8')
        return {'clip_id': clip_index + 1, 'filename': output.name, 'path': str(output),
                'start_time': segment['start_time'], 'end_time': segment['end_time'], 'duration': actual_duration,
                'text': ' '.join(w['text'] for w in words) or segment.get('text', ''),
                'keep_ranges': keep, 'output_offsets': offsets, 'group_output_starts': [],
                **{key: segment.get(key, 0 if key.endswith('_score') else '') for key in
                   ('relevance_score', 'reasoning', 'virality_score', 'hook_score', 'engagement_score', 'value_score', 'shareability_score', 'hook_type', 'hook_title')}}
    except BaseException:
        for path in (temporary, output, ass, output.with_suffix('.srt'), output.with_suffix('.source_map.json'), output.with_suffix('.transcript_cache.json')):
            path.unlink(missing_ok=True)
        raise
