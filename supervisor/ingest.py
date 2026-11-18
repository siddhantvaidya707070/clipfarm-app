import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from .ranges import validate_range


DATA = Path(os.getenv('DATA_DIR', '/data'))


class ReplayPending(ValueError):
    """The source URL is not a completed recording yet."""


def enough_space():
    return shutil.disk_usage(DATA).free >= 20 * 1024 ** 3


def public_source(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    allowed = ('youtube.com', 'youtu.be', 'twitch.tv', 'kick.com')
    return parsed.scheme == 'https' and not parsed.username and any(host == d or host.endswith('.' + d) for d in allowed)


def acquire(job):
    if not enough_space():
        raise ValueError('Less than 20 GB free: downloads paused')
    source = job['source']
    if not source.startswith('https://'):
        path = Path(source).resolve()
        if (DATA / 'inbox').resolve() not in path.parents or not path.is_file():
            raise ValueError('Imported file is missing or outside the inbox')
        return trim_source(job, path)
    if not public_source(source):
        raise ValueError('URL acquisition supports public YouTube, Twitch and Kick URLs; import other files through the inbox')
    folder = DATA / 'media' / job['id']
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'source.mp4'
    if path.exists():
        return trim_source(job, path)
    details = subprocess.run(['yt-dlp', '--no-playlist', '--skip-download', '--dump-single-json', '--socket-timeout', '15', source], capture_output=True, text=True, timeout=90)
    if details.returncode == 0:
        status = json.loads(details.stdout).get('live_status')
        if status in ('is_live', 'is_upcoming', 'post_live'):
            raise ReplayPending('Waiting for completed replay at the original source URL')
    # Completed videos only. Live capture is handled by the segmented recorder.
    result = subprocess.run(['yt-dlp', '--no-playlist', '--match-filter', '!is_live', '--max-filesize', '4G', '--merge-output-format', 'mp4', '-f', 'bv*[height<=1080]+ba/b[height<=1080]', '-o', str(folder / 'source.%(ext)s'), source], capture_output=True, text=True, timeout=1800)
    (folder / 'acquisition.log').write_text(result.stderr[-50000:], encoding='utf-8')
    if result.returncode or not path.exists():
        raise ValueError('Source acquisition failed or stream is live; retry the replay or enable live recording. Details retained in acquisition.log.')
    return trim_source(job, path)


def trim_source(job, path):
    start, end = validate_range(job.get("source_offset", 0), job.get("source_end"))
    origin = float(job.get("media_source_offset", start if job.get("captured_at") else 0))
    local_start = start - origin
    if local_start < 0:
        raise ValueError("Requested range predates available local media")
    if local_start == 0 and end is None:
        return str(path)
    duration = probe(path)["duration"]
    local_end = duration if end is None else end - origin
    if local_start >= duration or local_end > duration + .1:
        raise ValueError("Requested range exceeds available media duration")
    folder = DATA / "media" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "selected.mp4"
    if not output.exists():
        temporary = folder / "selected.partial.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                        "-ss", str(local_start), "-t", str(local_end - local_start),
                        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264",
                        "-preset", "fast", "-crf", "20", "-c:a", "aac",
                        "-movflags", "+faststart", str(temporary)],
                       check=True, capture_output=True, timeout=1800)
        temporary.replace(output)
    return str(output)


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    audios = [s for s in data['streams'] if s['codec_type'] == 'audio']
    if not videos or not audios:
        raise ValueError('Clip must have both video and audio')
    return {'duration': float(data['format']['duration']), 'width': videos[0]['width'], 'height': videos[0]['height']}


def normalize_render(path):
    info = probe(path)
    if (info['width'], info['height']) != (1080, 1920):
        output = Path(path).with_name(Path(path).stem + '-1080.mp4')
        subprocess.run(['ffmpeg', '-y', '-i', str(path), '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2', '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-c:a', 'aac', '-movflags', '+faststart', str(output)], check=True, capture_output=True, timeout=600)
        path = str(output)
    return str(path), probe(path)
