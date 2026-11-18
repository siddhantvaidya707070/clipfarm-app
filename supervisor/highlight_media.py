"""Small, cached yt-dlp ingestion and ffmpeg exports for highlight streams."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin

_WINDOW_LOCKS = {}
_WINDOW_LOCKS_GUARD = threading.Lock()


def youtube_id(source):
    if not isinstance(source, str):
        raise ValueError('Enter a YouTube HTTPS URL')
    url = urlparse(source)
    host = (url.hostname or '').lower()
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise ValueError('Enter a public YouTube HTTPS URL')
    if host == 'youtu.be':
        ident = url.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        parts = url.path.strip('/').split('/')
        ident = parse_qs(url.query).get('v', [''])[0]
        if not ident and len(parts) == 2 and parts[0] in ('live', 'shorts', 'embed'):
            ident = parts[1]
    else:
        raise ValueError('Enter a public YouTube URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
        raise ValueError('The YouTube URL needs a valid video ID')
    return ident


def run(command, *, timeout=900, cwd=None):
    """Keep provider URLs, signed query strings and stderr out of API errors."""
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f'{Path(command[0]).name} unavailable or timed out') from None
    if result.returncode:
        raise RuntimeError(f'{Path(command[0]).name} failed; check source availability and installed tools')
    return result.stdout


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def metadata(source, folder):
    path = folder / 'info.json'
    if path.exists():
        info = json.loads(path.read_text(encoding='utf-8'))
        if not info.get('is_live'):
            return info
    raw = json.loads(run(['yt-dlp', '--no-playlist', '--skip-download', '--dump-single-json',
                         '--socket-timeout', '20', '--retries', '2', source], timeout=90))
    info = {k: raw.get(k) for k in ('id', 'title', 'duration', 'is_live', 'live_status', 'release_timestamp')}
    if info.get('live_status') == 'is_upcoming':
        raise ValueError('This livestream has not started yet')
    info['is_live'] = bool(info.get('is_live') or info.get('live_status') == 'is_live')
    atomic_json(path, info)
    return info


def download_audio(source, folder):
    path = next((p for p in folder.glob('audio.*') if p.suffix in ('.m4a', '.webm', '.mp3', '.opus', '.ogg', '.mp4')), None)
    if path and path.stat().st_size:
        return path
    video = existing_video(folder)
    if video.exists() and video.stat().st_size:
        return video  # Already acquired; the transcriber extracts only its audio.
    run(['yt-dlp', '--no-playlist', '--no-progress', '--socket-timeout', '20', '--retries', '3',
         '-f', 'bestaudio', '-o', str(folder / 'audio.%(ext)s'), source], timeout=1800)
    path = next((p for p in folder.glob('audio.*') if p.suffix in ('.m4a', '.webm', '.mp3', '.opus', '.ogg', '.mp4')), None)
    if not path:
        raise RuntimeError('Audio download did not produce a file')
    return path


def download_video(source, folder):
    path = existing_video(folder)
    if path.exists() and path.stat().st_size:
        return path
    run(['yt-dlp', '--no-playlist', '--no-progress', '--socket-timeout', '20', '--retries', '3',
         '-f', 'bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]',
         '--merge-output-format', 'mp4', '--remux-video', 'mp4',
         '-o', str(folder / 'source.%(ext)s'), source], timeout=3600)
    if not path.exists():
        raise RuntimeError('Video download did not produce an MP4')
    return path


def existing_video(folder):
    path = folder / 'source.mp4'
    if path.exists():
        return path
    # The native editor and this workflow share the already downloaded source.
    # A filename is never accepted from a client; folder names are canonical IDs.
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', folder.name) and len(folder.parents) >= 3:
        native = folder.parents[2] / 'engine-temp' / 'youtube-cache' / folder.name
        candidates = sorted(native.glob('full-*.mp4'), reverse=True)
        if candidates:
            return candidates[0]
    return path


def plan_source_windows(clips, duration, cached, padding=10):
    """Merge editing handles, then subtract footage already retained on disk."""
    merged = []
    for start, end in sorted((max(0, c['start'] - padding), min(duration, c['end'] + padding)) for c in clips):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    missing = []
    for start, end in merged:
        cursor = start
        for piece in sorted(cached, key=lambda p: p['start']):
            if piece['end'] <= cursor or piece['start'] >= end:
                continue
            if piece['start'] > cursor:
                missing.append((cursor, piece['start']))
            cursor = max(cursor, piece['end'])
        if cursor < end:
            missing.append((cursor, end))
    return missing


def clip_sources(row, clip):
    """Resolve original source bounds into one or more clean local intervals."""
    full = row.get('video_path')
    if full and Path(full).is_file() and Path(full).stat().st_size:
        return [{'path': full, 'start': clip['start'], 'end': clip['end']}]
    pieces = [p for p in row.get('source_windows', []) if Path(p['path']).is_file() and Path(p['path']).stat().st_size]
    cursor, result = clip['start'], []
    while cursor < clip['end'] - .001:
        available = [p for p in pieces if p['start'] <= cursor + .001 and p['end'] > cursor + .001]
        if not available:
            raise ValueError('Selected footage needs to be downloaded before export')
        piece = max(available, key=lambda p: p['end'])
        end = min(clip['end'], piece['end'])
        result.append({'path': piece['path'], 'start': max(0, cursor - piece['start']), 'end': end - piece['start']})
        cursor = end
    return result


def _source_address(base, value):
    address = urljoin(base, value)
    parsed = urlparse(address)
    if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.googlevideo.com')
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or any(c in address for c in '\r\n"')):
        raise ValueError('Unsupported YouTube media address')
    return address


def parse_source_playlist(text, base, duration):
    """Accept complete unencrypted timed HLS only; never guess source offsets."""
    from yt_dlp.utils import parse_m3u8_attributes
    lines = [line.strip() for line in text.lstrip('\ufeff').splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U' or lines[-1] != '#EXT-X-ENDLIST':
        raise ValueError('Selected-window acquisition requires a complete HLS replay')
    result, cursor, pending, sequence = [], 0.0, None, 0
    for line in lines[1:]:
        if line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-I-FRAME', '#EXT-X-BYTERANGE', '#EXT-X-DISCONTINUITY', '#EXT-X-GAP', '#EXT-X-SKIP', '#EXT-X-START')):
            if line != '#EXT-X-DISCONTINUITY-SEQUENCE:0':
                raise ValueError('This source playlist cannot be safely sliced')
        elif line.startswith('#EXT-X-KEY:'):
            if parse_m3u8_attributes(line.partition(':')[2]).get('METHOD') != 'NONE':
                raise ValueError('Encrypted source windows are unsupported')
        elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            if cursor:
                raise ValueError('Source playlist sequence changed')
            sequence = int(line.partition(':')[2])
        elif line.startswith('#EXT-X-MAP:'):
            attrs = parse_m3u8_attributes(line.partition(':')[2])
            if result or attrs.get('BYTERANGE') or not attrs.get('URI'):
                raise ValueError('Unsupported source initialization')
            result.append({'url': _source_address(base, attrs['URI']), 'init': True})
        elif line.startswith('#EXTINF:'):
            if pending is not None:
                raise ValueError('Missing source fragment URL')
            pending = float(line.partition(':')[2].split(',')[0])
            if not math.isfinite(pending) or pending <= 0:
                raise ValueError('Invalid source fragment duration')
        elif not line.startswith('#'):
            if pending is None:
                raise ValueError('Untimed source fragment')
            result.append({'url': _source_address(base, line), 'start': cursor,
                           'end': cursor + pending, 'duration': pending, 'sequence': sequence})
            cursor += pending
            sequence += 1
            pending = None
    if pending is not None or cursor <= 0 or abs(cursor - duration) > max(2, duration * .001):
        raise ValueError('Source playlist does not cover the original timeline')
    return result


def slice_source_playlist(fragments, start, end):
    media = [f for f in fragments if not f.get('init') and f['end'] > start and f['start'] < end]
    if not media:
        raise ValueError('No fragments cover the selected source interval')
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', f"#EXT-X-TARGETDURATION:{math.ceil(max(f['duration'] for f in media))}",
             f"#EXT-X-MEDIA-SEQUENCE:{media[0]['sequence']}", '#EXT-X-PLAYLIST-TYPE:VOD']
    for fragment in fragments:
        if fragment.get('init'):
            lines.append(f'#EXT-X-MAP:URI="{fragment["url"]}"')
    for fragment in media:
        lines.extend([f"#EXTINF:{fragment['duration']:.9f},", fragment['url']])
    return '\n'.join(lines + ['#EXT-X-ENDLIST', '']), media[0]['start'], media[-1]['end']


def select_cheaper_full_source(info, chosen, window_bytes, byte_limit):
    """Use exact-size full tracks only when their cost clearly beats the estimate.

    Keep resolution and frame rate, require the selected audio language, and
    leave a 25% uncertainty margin for variable-bitrate HLS estimates.
    """
    if not window_bytes or window_bytes <= 0:
        return None
    selected_video, selected_audio = chosen[0], chosen[-1]
    language = selected_audio.get('language') or info.get('language')
    formats = [f for f in info.get('formats', []) if f.get('protocol') == 'https'
               and not f.get('has_drm') and (f.get('filesize') or 0) > 0]
    videos = [f for f in formats if f.get('vcodec') not in (None, 'none')
              and (f.get('height'), f.get('width')) == (selected_video.get('height'), selected_video.get('width'))
              and f.get('fps') and selected_video.get('fps')
              and abs(f['fps'] - selected_video['fps']) < .01]
    audios = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')
              and language and f.get('language') == language
              and (f.get('abr') or 0) >= (selected_audio.get('abr') or 0)
              and not re.search(r'\b(?:dubbed|descriptive)\b', f.get('format_note') or '', re.I)]
    candidates = []
    for video in videos:
        if video.get('acodec') not in (None, 'none'):
            if language and video.get('language') == language:
                candidates.append([video])
        elif audios:
            candidates.append([video, min(audios, key=lambda f: f['filesize'])])
    eligible = [tracks for tracks in candidates
                if sum(f['filesize'] for f in tracks) <= min(byte_limit / 1.05, window_bytes * .75)]
    return min(eligible, key=lambda tracks: sum(f['filesize'] for f in tracks)) if eligible else None


def acquire_source_windows(source, folder, clips, duration, cached=(), *, stop=None, on_window=None):
    # Different owners may score the same source concurrently. Serialize writes
    # to its shared cache and re-read the manifest after the preceding writer.
    with _WINDOW_LOCKS_GUARD:
        lock = _WINDOW_LOCKS.setdefault(str(Path(folder).resolve()), threading.Lock())
    with lock:
        return _acquire_source_windows(source, folder, clips, duration, cached, stop=stop, on_window=on_window)


def _acquire_source_windows(source, folder, clips, duration, cached=(), *, stop=None, on_window=None):
    """Retain clean fragment-aligned windows using fresh batch metadata.

    ffmpeg sees local media only. Unsupported section transports fail visibly,
    rather than silently downloading a multi-gigabyte full source.
    """
    full = existing_video(folder)
    if full.is_file() and full.stat().st_size:
        return str(full), []
    manifest = folder / 'source-windows.json'
    saved = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else []
    windows = {p['path']: p for p in [*saved, *cached] if Path(p['path']).is_file() and Path(p['path']).stat().st_size}
    missing = plan_source_windows(clips, duration, list(windows.values()))
    if not missing:
        return '', list(windows.values())
    import yt_dlp
    from yt_dlp.networking import Request
    lock, seen = threading.Lock(), {}
    total = 0
    deadline = time.monotonic() + max(60, int(os.getenv('HIGHLIGHT_DOWNLOAD_TIMEOUT', '1800')))
    limit = max(1, int(os.getenv('HIGHLIGHT_DOWNLOAD_MAX_BYTES', '2000000000')))

    def progress(event):
        nonlocal total
        with lock:
            name = event.get('filename', '')
            amount = max(0, int(event.get('downloaded_bytes') or 0))
            total += max(0, amount - seen.get(name, 0))
            seen[name] = max(amount, seen.get(name, 0))
            if total > limit:
                raise ValueError('Selected source windows exceeded the download byte budget')
        if (stop and stop.is_set()) or time.monotonic() > deadline:
            raise ValueError('Source-window download stopped or exceeded its time budget')

    options = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'noplaylist': True, 'socket_timeout': 20,
               'retries': 2, 'fragment_retries': 2, 'concurrent_fragment_downloads': 4,
               'skip_unavailable_fragments': False, 'progress_hooks': [progress],
               'external_downloader': {'default': 'native'}, 'overwrites': True}
    # Initialize plugins/extractors in the parent before any native download threads.
    with yt_dlp.YoutubeDL(options) as client:
        info = client.extract_info(source, download=False)
        formats = [f for f in info.get('formats', []) if 'm3u8' in (f.get('protocol') or '') and not f.get('has_drm')]
        videos = [f for f in formats if f.get('vcodec') not in (None, 'none') and 0 < (f.get('height') or 0) <= 1080]
        if not videos:
            raise ValueError('No safely sliceable video format is available for this source')
        video = max(videos, key=lambda f: (f.get('height') or 0, f.get('fps') or 0, f.get('tbr') or 0))
        if video.get('acodec') not in (None, 'none'):
            chosen = [video]
        else:
            # HLS audio playlists often omit acodec until downloaded (233/234).
            audios = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
                      and not re.search(r'\b(?:dubbed|descriptive)\b', f.get('format_note') or '', re.I)]
            language = info.get('language')
            original = [f for f in info.get('formats', []) if 'original' in (f.get('format_note') or '').lower() and f.get('language')]
            if original:
                language = original[0]['language']
            matching = [f for f in audios if language and f.get('language') == language]
            audios = matching or audios
            # yt-dlp marks original/default language tracks with language_preference.
            if not audios:
                raise ValueError('No safely sliceable audio format is available for this source')
            audio = max(audios, key=lambda f: (f.get('language_preference') or 0, f.get('abr') or f.get('tbr') or 0))
            chosen = [video, audio]
        expanded = []
        for fmt in chosen:
            with client.urlopen(Request(_source_address(fmt['url'], fmt['url']), headers=fmt.get('http_headers') or {})) as response:
                payload = response.read(4_000_001)
                if len(payload) > 4_000_000:
                    raise ValueError('Source playlist exceeds its size limit')
                expanded.append((fmt, parse_source_playlist(payload.decode('utf-8'), response.url, duration)))

    # Compare only missing footage, including actual fragment alignment and
    # handles. Missing bitrate/size data keeps the bounded window strategy.
    window_bytes = 0
    for fmt, fragments in expanded:
        bitrate = fmt.get('tbr') or fmt.get('abr')
        if not bitrate:
            window_bytes = None
            break
        for start, end in missing:
            _, aligned_start, aligned_end = slice_source_playlist(fragments, start, end)
            window_bytes += (aligned_end - aligned_start) * bitrate * 1000 / 8
    full_tracks = select_cheaper_full_source(info, chosen, window_bytes, limit)
    atomic_json(folder / 'source-acquisition.json', {
        'mode': 'full' if full_tracks else 'windows', 'estimated_window_bytes': round(window_bytes) if window_bytes else None,
        'full_bytes': sum(f['filesize'] for f in full_tracks) if full_tracks else None,
        'format_ids': [f['format_id'] for f in (full_tracks or chosen)],
    })
    if full_tracks:
        progress({})
        with tempfile.TemporaryDirectory(prefix='full-', dir=folder) as workdir:
            params = {**options, 'format': '+'.join(f['format_id'] for f in full_tracks),
                      'outtmpl': str(Path(workdir) / 'source.%(ext)s'), 'merge_output_format': 'mp4',
                      'postprocessors': [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]}
            prepared = {k: v for k, v in info.items() if k not in ('requested_formats', 'requested_downloads')}
            with yt_dlp.YoutubeDL(params) as client:
                client.process_ie_result(prepared, download=True)
            path = Path(workdir) / 'source.mp4'
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError('Adaptive full-source acquisition did not produce an MP4')
            path.replace(folder / 'source.mp4')
        return str(folder / 'source.mp4'), list(windows.values())

    def download_window(bounds):
        start, end = bounds
        slices = [(fmt, *slice_source_playlist(fragments, start, end)) for fmt, fragments in expanded]
        media_start, media_end = max(s[2] for s in slices), min(s[3] for s in slices)
        if media_start > start + .01 or media_end < end - .01:
            raise ValueError('Source fragments do not cover the requested edit')
        target = folder / f'window-{int(media_start * 1000)}-{int(media_end * 1000)}.mp4'
        with tempfile.TemporaryDirectory(prefix='window-', dir=folder) as workdir:
            inputs = []
            for index, (fmt, playlist, first, last) in enumerate(slices):
                progress({})
                stem = Path(workdir) / f'track-{index}'
                params = {**options, 'outtmpl': str(stem) + '.%(ext)s'}
                media = {**fmt, 'id': info['id'], 'title': info['id'], 'extractor': 'youtube',
                         'protocol': 'm3u8_native', 'hls_media_playlist_data': playlist}
                media.pop('fragments', None)
                with yt_dlp.YoutubeDL(params) as client:
                    client.process_info(media)
                path = stem.with_suffix('.' + (fmt.get('ext') or 'mp4'))
                if not path.is_file():
                    raise RuntimeError('Source-window download did not produce media')
                inputs.extend(['-ss', str(max(0, media_start - first)), '-i', str(path)])
            temporary = Path(workdir) / 'clean.mp4'
            run(['ffmpeg', '-v', 'error', '-y', *inputs, '-t', str(media_end - media_start),
                 '-map', '0:v:0', '-map', '0:a:0' if len(slices) == 1 else '1:a:0',
                 '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', str(temporary)], timeout=120)
            temporary.replace(target)
        return {'path': str(target), 'start': media_start, 'end': media_end}

    with ThreadPoolExecutor(max_workers=max(1, min(3, int(os.getenv('HIGHLIGHT_DOWNLOAD_WORKERS', '3'))))) as pool:
        futures = [pool.submit(download_window, bounds) for bounds in missing]
        failure = None
        for future in as_completed(futures):
            try:
                piece = future.result()
                windows[piece['path']] = piece
                atomic_json(manifest, list(windows.values()))
                if on_window:
                    on_window(list(windows.values()))
            except Exception as exc:
                failure = failure or exc
                for pending in futures:
                    pending.cancel()
        if failure:
            raise failure
    return '', list(windows.values())


def capture_live(source, folder, seconds, stop):
    """Capture from now. Stop writes a valid partial recording before processing it."""
    path = folder / 'source.mp4'
    if path.exists() and path.stat().st_size:
        return path
    urls = run(['yt-dlp', '--no-playlist', '--socket-timeout', '20', '-f',
                'bv*[height<=720]+ba/b[height<=720]', '--get-url', source], timeout=90).decode().strip().splitlines()
    if not 1 <= len(urls) <= 2 or any(not u.startswith('https://') for u in urls):
        raise RuntimeError('Could not resolve livestream media')
    temporary = folder / 'recording.ts'
    command = ['ffmpeg', '-v', 'error', '-y']
    for url in urls:
        command += ['-rw_timeout', '20000000', '-i', url]
    command += ['-map', '0:v:0', '-map', '1:a:0' if len(urls) == 2 else '0:a:0',
                '-t', str(seconds), '-c', 'copy', '-f', 'mpegts', str(temporary)]
    atomic_json(folder / 'recording-clock.json', {'started': time.time()})
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + seconds + 120
    try:
        while process.poll() is None:
            if stop.wait(.5) or time.monotonic() >= deadline:
                try:
                    process.communicate(input=b'q', timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                break
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    if not temporary.exists() or temporary.stat().st_size < 10000:
        raise RuntimeError('No usable live footage was captured')
    run(['ffmpeg', '-v', 'error', '-y', '-i', str(temporary), '-map', '0:v:0', '-map', '0:a:0',
         '-c', 'copy', '-movflags', '+faststart', str(path)], timeout=120)
    return path


def parse_chat(path, recording_started=None):
    """Read yt-dlp JSONL replay or live chat; retain reaction text and emotes."""
    messages, seen = [], set()

    def visit(node, offset=None):
        if isinstance(node, list):
            for item in node:
                visit(item, offset)
        elif isinstance(node, dict):
            replay = node.get('replayChatItemAction')
            if replay:
                try:
                    offset = float(replay.get('videoOffsetTimeMsec', 0)) / 1000
                except (TypeError, ValueError):
                    return
            for key in ('liveChatTextMessageRenderer', 'liveChatPaidMessageRenderer'):
                renderer = node.get(key)
                if not renderer:
                    continue
                stamp = offset
                if recording_started is not None:
                    try:
                        stamp = float(renderer['timestampUsec']) / 1000000 - recording_started
                    except (KeyError, TypeError, ValueError):
                        continue
                if stamp is None or not math.isfinite(stamp) or stamp < 0:
                    continue
                runs = renderer.get('message', {}).get('runs', [])
                emotes = [str(r['emoji'].get('emojiId', '')) for r in runs if 'emoji' in r]
                text = ''.join(r.get('text', '') or ' '.join(r.get('emoji', {}).get('shortcuts', [])) for r in runs)
                ident = renderer.get('id') or (stamp, text)
                if ident not in seen and text:
                    seen.add(ident)
                    messages.append({'time': round(stamp, 3), 'text': text[:2000], 'emotes': emotes})
            for key, value in node.items():
                if key not in ('liveChatTextMessageRenderer', 'liveChatPaidMessageRenderer'):
                    visit(value, offset)

    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                visit(json.loads(line))
            except (ValueError, TypeError):
                # yt-dlp may be sleeping before committing its latest live-chat
                # fragment when a bounded capture closes. Its first fragment is
                # an HTML page containing JSON, not executable application code.
                for marker in re.finditer(r'''(?:ytInitialData|ytInitialLiveChatData)(?:["']\])?\s*=\s*''', line):
                    try:
                        data, _ = json.JSONDecoder().raw_decode(line, marker.end())
                        visit(data)
                    except (ValueError, TypeError):
                        continue
    return sorted(messages, key=lambda message: message['time'])


def download_chat(source, folder, done, *, live=False, recording_started=None, seconds=600):
    cached = folder / 'chat.json'
    if cached.exists():
        return json.loads(cached.read_text(encoding='utf-8'))
    command = ['yt-dlp', '--no-playlist', '--skip-download', '--write-subs', '--sub-langs',
               'live_chat', '--no-progress', '--socket-timeout', '15', '--retries', '1',
               '-o', str(folder / 'chat.%(ext)s'), source]
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + (seconds + 120 if live else 90)
        while process.poll() is None:
            stopped = done.wait(.25) if live else False
            if not live:
                time.sleep(.25)
            if stopped or time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                break
        messages = {}
        for path in sorted(folder.glob('chat.live_chat.json*')):
            if not path.is_file() or not path.stat().st_size:
                continue
            for message in parse_chat(path, recording_started if live else None):
                key = (message['time'], message['text'], tuple(message['emotes']))
                messages[key] = message
        result = sorted(messages.values(), key=lambda message: message['time'])
    except (OSError, subprocess.SubprocessError):
        result = []
    # Missing replay is not cached permanently; a completed live replay may appear later.
    if result:
        atomic_json(cached, result)
    return result


def clip_captions(transcript, start, end):
    selected = [w for w in transcript.get('words', [])
                if float(w.get('end', 0)) / 1000 > start and float(w.get('start', 0)) / 1000 < end]
    captions, group = [], []
    for index, word in enumerate(selected):
        group.append(word)
        span = (float(word['end']) - float(group[0]['start'])) / 1000
        if len(group) >= 8 or span >= 3 or re.search(r'[.!?]$', str(word.get('text', ''))) or index == len(selected) - 1:
            a = max(0, float(group[0]['start']) / 1000 - start)
            b = min(end - start, float(group[-1]['end']) / 1000 - start)
            if b > a:
                captions.append({'start': round(a, 3), 'end': round(b, 3),
                                 'text': ' '.join(str(w.get('text', '')).strip() for w in group)})
            group = []
    return captions


def srt_text(captions):
    def stamp(seconds):
        millis = max(0, round(seconds * 1000))
        hours, millis = divmod(millis, 3600000)
        minutes, millis = divmod(millis, 60000)
        secs, millis = divmod(millis, 1000)
        return f'{hours:02}:{minutes:02}:{secs:02},{millis:03}'
    return '\n\n'.join(f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}"
                       for i, c in enumerate(captions, 1)) + '\n'


def keyframe_aligned(video_path, start, end):
    """Stream copy only exact keyframe cuts, otherwise captions and cuts drift."""
    try:
        data = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                               '-read_intervals', f'{max(0, start - 3)}%{end + 1}',
                               '-skip_frame', 'nokey', '-show_frames',
                               '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json',
                               str(video_path)], timeout=30))
        times = [float(frame['best_effort_timestamp_time']) for frame in data.get('frames', [])
                 if 'best_effort_timestamp_time' in frame]
        return all(any(abs(point - stamp) < .025 for stamp in times) for point in (start, end))
    except (ValueError, RuntimeError):
        return False


def export_clip(video_path, folder, clip, defaults):
    """Seek once per clip. Filters encode only the selected interval, never the stream."""
    ident = clip['id']
    output = folder / f'{ident}.mp4'
    temporary = folder / f'{ident}.partial.mp4'
    srt = folder / f'{ident}.srt'
    srt.write_text(srt_text(clip.get('captions', [])), encoding='utf-8')
    portrait = clip.get('output_format', defaults.get('output_format')) == 'vertical'
    burn = clip.get('burn_captions', defaults.get('burn_captions', True))
    height = int(clip.get('export_height', defaults.get('export_height', 720)))
    filters = []
    parts = clip.get('source_parts') or [{'path': str(video_path), 'start': clip['start'], 'end': clip['end']}]
    video_path = parts[0]['path']
    if portrait:
        center = float(clip.get('crop_center', .5))
        filters.append(f"crop=w='min(iw,ih*9/16)':h=ih:x='max(0,min(iw-ow,iw*{center}-ow/2))':y=0")
        filters.append(f'scale={height}:{height * 16 // 9}:flags=fast_bilinear,setsar=1')
    elif burn:
        filters.append(f"scale=-2:'min(ih,{height})':flags=fast_bilinear")
    else:
        info = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                               '-show_entries', 'stream=height', '-of', 'json', str(video_path)], timeout=30))
        if any(int(stream.get('height', 0)) > height for stream in info.get('streams', [])):
            filters.append(f'scale=-2:{height}:flags=fast_bilinear')
    if burn and clip.get('captions'):
        # Only an internally generated hex ID is interpolated into the filter.
        filters.append(f"subtitles=filename='{ident}.srt':force_style='FontName=DejaVu Sans,FontSize=22,Outline=2,MarginV=25'")
    if filters:
        filters.insert(0, 'fps=30')
    command = ['ffmpeg', '-v', 'error', '-y', '-threads', '2', '-filter_threads', '1', '-filter_complex_threads', '1']
    for part in parts:
        command += ['-ss', str(part['start']), '-t', str(part['end'] - part['start']), '-i', str(Path(part['path']).resolve())]
    if len(parts) > 1:
        graph = []
        for index in range(len(parts)):
            graph += [f'[{index}:v:0]setpts=PTS-STARTPTS[v{index}]',
                      f'[{index}:a:0]asetpts=PTS-STARTPTS[a{index}]']
        graph.append(''.join(f'[v{i}][a{i}]' for i in range(len(parts))) + f'concat=n={len(parts)}:v=1:a=1[video][audio]')
        if filters:
            graph.append('[video]' + ','.join(filters) + '[filtered]')
        command += ['-filter_complex', ';'.join(graph), '-map', '[filtered]' if filters else '[video]', '-map', '[audio]']
    else:
        command += ['-map', '0:v:0', '-map', '0:a:0']
    command += ['-t', str(clip['end'] - clip['start'])]
    if len(parts) > 1 or filters or not keyframe_aligned(video_path, parts[0]['start'], parts[0]['end']):
        if filters and len(parts) == 1:
            command += ['-vf', ','.join(filters)]
        command += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '22',
                    '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k']
    else:
        command += ['-c', 'copy', '-avoid_negative_ts', 'make_zero']
    command += ['-movflags', '+faststart', str(temporary.resolve())]
    run(command, timeout=600, cwd=str(folder))
    raw = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type',
                          '-of', 'json', str(temporary)], timeout=30))
    if not {'video', 'audio'}.issubset({s['codec_type'] for s in raw.get('streams', [])}):
        raise RuntimeError('Export is missing video or audio')
    if float(raw.get('format', {}).get('duration', 0)) < 1:
        raise RuntimeError('Export has no playable duration')
    temporary.replace(output)
    return {'export_state': 'ready', 'mp4_path': str(output), 'srt_path': str(srt)}
