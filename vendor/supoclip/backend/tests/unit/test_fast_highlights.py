"""Fast native highlight selection and rendering contracts."""
import json
import math
from pathlib import Path

from src.services import video_service as service_module


def transcript_for_hour():
    words = []
    for index in range(7200):
        text = 'Amazing!' if index % 120 == 20 else ('That is incredible!' if index % 120 == 22 else 'grounded')
        words.append({'start': index * 500, 'end': index * 500 + 350, 'text': text})
    return {'words': words}


def test_fast_local_plan_returns_eighteen_grounded_disjoint_clips():
    from src.services import fast_highlights as fast
    transcript = transcript_for_hour()
    clips = fast.select_fast_highlights(transcript, 3600)
    assert len(clips) == 18
    ordered = sorted(clips, key=lambda c: c['source_ranges'][0][0])
    for clip in ordered:
        start, end = clip['source_ranges'][0]
        assert 0 <= start < end <= 3600
        assert 15 <= end - start <= 30
        expected = ' '.join(word['text'] for word in transcript['words'] if word['end'] / 1000 > start and word['start'] / 1000 < end)
        assert clip['text'] == expected
        assert clip['fast_highlight'] is True
    assert all(a['source_ranges'][0][1] <= b['source_ranges'][0][0] for a,b in zip(ordered, ordered[1:]))


def test_fast_volume_profile_cannot_reuse_legacy_three_clip_cache(monkeypatch):
    from types import SimpleNamespace
    from src.services import task_service
    config = SimpleNamespace(fast_highlights_enabled=False, fast_highlights_per_hour=18,
                             fast_highlights_max_per_hour=20, fast_highlights_export_height=720)
    monkeypatch.setattr(task_service, 'get_config', lambda: config)
    old = task_service.TaskService._build_cache_key('https://youtu.be/abcdefghijk', 'youtube', 'fast')
    config.fast_highlights_enabled = True
    new = task_service.TaskService._build_cache_key('https://youtu.be/abcdefghijk', 'youtube', 'fast')
    assert new != old
    config.fast_highlights_per_hour = 20
    assert task_service.TaskService._build_cache_key('https://youtu.be/abcdefghijk', 'youtube', 'fast') != new


def test_fast_local_fallback_does_not_fill_quota_with_ordinary_speech():
    from src.services import fast_highlights as fast
    quiet = {'words': [{'text': 'ordinary', 'start': i * 500, 'end': i * 500 + 300} for i in range(7200)]}
    clips = fast.select_fast_highlights(quiet, 3600)
    assert clips == []
    assert fast.select_fast_highlights({'words': []}, 3600) == []


def test_fast_local_plan_respects_requested_count_and_duration_preset():
    from src.services import fast_highlights as fast
    clips = fast.select_fast_highlights(transcript_for_hour(), 3600, clip_length='medium', max_clips=7)
    assert len(clips) == 7
    assert all(30 <= c['source_ranges'][0][1] - c['source_ranges'][0][0] <= 60 for c in clips)


def test_fast_renderer_keeps_native_edit_maps_and_relative_caption_words(tmp_path):
    import hashlib
    import subprocess
    from src.services.fast_render import render_fast_clip
    from src.clip_source_map import load_clip_source_ranges
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30:duration=4',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', '-shortest', str(source)], check=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    source.with_suffix('.transcript_cache.json').write_text(json.dumps({'words': [
        {'text': 'Grounded', 'start': 1000, 'end': 1500}, {'text': 'caption.', 'start': 1700, 'end': 2200}]}))
    segment = {'start_time': '00:01', 'end_time': '00:03', 'source_ranges': [(1, 3)],
               'text': 'Grounded caption.', 'relevance_score': .4}
    result = render_fast_clip(source, segment, 0, tmp_path, output_format='original', add_subtitles=True)
    assert result and result['duration'] == 2
    assert load_clip_source_ranges(Path(result['path'])) == [(1, 3)]
    words = json.loads(Path(result['path']).with_suffix('.transcript_cache.json').read_text())['words']
    assert words[0]['start'] == 0 and words[0]['end'] == 500
    assert words[1]['start'] == 700
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,width,height,r_frame_rate', '-of', 'json', result['path']]))
    assert {s['codec_type'] for s in probe['streams']} == {'video', 'audio'}
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    assert (video['width'], video['height'], video['r_frame_rate']) == (1280, 720, '30/1')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize('source_type', ['youtube', 'upload'])
async def test_native_youtube_fast_pipeline_uses_one_ai_pass_filters_silence_and_preserves_retry_plan(monkeypatch, tmp_path, source_type):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.services.video_service import VideoService
    from src.services import fast_ai_selection, fast_audio_activity
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'cached clean source')
    source.with_suffix('.transcript_cache.json').write_text(json.dumps(transcript_for_hour()))
    config = SimpleNamespace(max_video_duration=10800, max_clips=20, fast_mode_max_clips=20, fast_highlights_enabled=True,
                             youtube_download_max_bytes=2000000000, fast_ai_selection_enabled=True,
                             fast_validate_audio_activity=True)
    requests = []
    async def choose(data, duration, config, **kwargs):
        from src.services.fast_highlights import select_fast_highlights
        requests.append((len(data['words']), duration, kwargs['target']))
        segments = select_fast_highlights(data, duration)
        for segment in segments:
            segment['selection_provider'] = 'ai'
        return {'segments': segments, 'selection_provider': 'ai', 'model': 'test-model', 'seconds': .01}
    monkeypatch.setattr(fast_ai_selection, 'select_ai_highlights', choose)
    def activity(path, segments, time_map):
        assert path == source
        return (segments[:-1] if len(segments) > 1 else segments), [{'index': 17, 'decision': 'silent'}]
    monkeypatch.setattr(fast_audio_activity, 'filter_silent_highlights', activity)
    monkeypatch.setattr(service_module, 'get_config', lambda: config)
    monkeypatch.setattr(service_module, 'async_get_youtube_video_info', AsyncMock(return_value={'duration': 3600}))
    monkeypatch.setattr(service_module, 'cached_full_media', lambda *args: source)
    monkeypatch.setattr(VideoService, '_get_file_duration', lambda *args: 3600)
    monkeypatch.setattr(VideoService, 'prepare_source', AsyncMock(return_value=source))
    monkeypatch.setattr(VideoService, 'analyze_transcript', AsyncMock(side_effect=AssertionError('Do not use the verbose legacy analysis chain')))
    monkeypatch.setattr(VideoService, '_drop_static_segments', lambda *args: (_ for _ in ()).throw(AssertionError('No per-clip visual scan in fast mode')))
    monkeypatch.setattr(service_module, 'MediaStorage', lambda: SimpleNamespace(save_source_metadata=lambda *args: None))
    monkeypatch.setattr(service_module, 'persist_cache_sidecar', lambda *args: None)
    monkeypatch.setattr(service_module, 'materialize_stored_upload', AsyncMock(return_value=source))
    monkeypatch.setattr(service_module, 'upload_needs_remote_copy', AsyncMock(return_value=False))
    saved = {}
    async def checkpoint(patch):
        saved.update(patch)
    result = await VideoService.process_video_complete('https://youtube.com/watch?v=abcdefghijk', source_type,
        processing_mode='fast', cached_transcript='Grounded original transcript', checkpoint_callback=checkpoint)
    assert result.get('fast_highlights') is True
    assert len(result['segments_to_render']) == 17
    assert saved['segments'] == result['segments_to_render']
    preserved = result['segments_to_render'][:1]
    replay = await VideoService.process_video_complete('https://youtube.com/watch?v=abcdefghijk', source_type,
        processing_mode='fast', cached_transcript='Grounded original transcript', checkpoint={'segments': preserved})
    assert replay['segments_to_render'] == preserved
    assert requests == [(7200, 3600, 18)]
    assert saved['audio_activity'][0]['decision'] == 'silent'
    assert json.loads(result['analysis_json'])['selection_provider'] == 'ai'


@pytest.mark.parametrize('streams', [[{'codec_type': 'video', 'width': 1280, 'height': 720}],
    [{'codec_type': 'video', 'width': 640, 'height': 360}, {'codec_type': 'audio'}],
    [{'codec_type': 'video', 'width': 1280, 'height': 720, 'duration': '2'}, {'codec_type': 'audio', 'duration': '1'}]])
def test_fast_export_validation_rejects_missing_audio_low_resolution_or_desync(monkeypatch, tmp_path, streams):
    from types import SimpleNamespace
    from src.services import fast_render
    monkeypatch.setattr(fast_render.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({'streams': streams, 'format': {'duration': '2'}})))
    with pytest.raises(RuntimeError):
        fast_render._validate_export(tmp_path / 'clip.mp4', 2)
