"""Fast timed YouTube captions, preserving provider text and original timestamps."""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..media.timeline import seconds_to_mmss
from ..youtube_proxy import ByteBudget, DownloadBudgetExceeded, download_options, source_info

logger = logging.getLogger(__name__)


def choose_original_caption(info: dict):
    """Never substitute translated English captions for another source language."""
    language = (info.get('language') or '').lower().split('-')[0]
    automatic = info.get('automatic_captions') or {}
    original_english = automatic.get('en-orig')
    if language and language != 'en':
        return None
    if language == 'en':
        for key, formats in (info.get('subtitles') or {}).items():
            if key.lower().split('-')[0] == 'en':
                candidate = next((f for f in formats if f.get('ext') == 'json3'), None)
                if candidate:
                    return 'en', candidate, False
    formats = original_english or (automatic.get('en') if language == 'en' else None) or []
    candidate = next((f for f in formats if f.get('ext') == 'json3'), None)
    return ('en', candidate, True) if candidate else None


def parse_json3_transcript(raw: dict, *, duration: float, language: str = 'en') -> dict:
    """Keep provider starts; infer ends only from the next start/cue end.

    A phrase delivered as one timed segment stays one phrase, rather than
    inventing per-word offsets. Timing provenance explicitly records this.
    """
    words = {}
    phrase_timing = False
    duration_ms = int(duration * 1000)
    for event in raw.get('events') or []:
        try:
            base = float(event.get('tStartMs', -1))
            cue_end = base + float(event.get('dDurationMs') or 0)
            if not math.isfinite(base) or not math.isfinite(cue_end) or base < 0:
                continue
            for segment in event.get('segs') or []:
                text = ' '.join(str(segment.get('utf8') or '').split())
                if not text:
                    continue
                start = round(base + float(segment.get('tOffsetMs') or 0))
                if start < 0 or start >= duration_ms:
                    continue
                phrase_timing = phrase_timing or len(text.split()) > 1
                words[(start, text)] = {'text': text, 'start': start, 'end': min(duration_ms, round(cue_end))}
        except (ValueError, TypeError, OverflowError):
            continue
    ordered = sorted(words.values(), key=lambda w: (w['start'], w['text']))
    cleaned = []
    for index, word in enumerate(ordered):
        following = next((w['start'] for w in ordered[index + 1:index + 5] if w['start'] > word['start']), None)
        if following is not None:
            word['end'] = min(word['end'], following) if word['end'] > word['start'] else following
        if word['end'] > word['start']:
            cleaned.append(word)
    utterances = []
    for index in range(0, len(cleaned), 30):
        group = cleaned[index:index + 30]
        utterances.append({'start': group[0]['start'], 'end': group[-1]['end'],
                           'text': ' '.join(w['text'] for w in group), 'words': group})
    return {'version': 2, 'provider': 'youtube_captions', 'language_code': language,
            'timing_quality': 'provider_phrase_timing' if phrase_timing else 'provider_word_starts_inferred_ends',
            'word_end_times_inferred': True, 'words': cleaned, 'utterances': utterances}


def caption_analysis_text(data: dict) -> str:
    return '\n'.join(f"[{seconds_to_mmss(u['start'] / 1000)} - {seconds_to_mmss(u['end'] / 1000)}] {u['text']}"
                     for u in data.get('utterances') or [])


def _atomic_json(path: Path, value):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_fast_youtube_captions(url: str, video_id: str, folder: Path, budget: ByteBudget,
                                *, duration: float, task_id=None):
    """Return actual JSON3 path/native sidecar/text, or None for audio fallback."""
    path = Path(folder) / 'captions.json'
    sidecar = path.with_suffix('.transcript_cache.json')
    try:
        if path.is_file() and sidecar.is_file():
            data = json.loads(sidecar.read_text(encoding='utf-8'))
            if data.get('provider') == 'youtube_captions' and data.get('words'):
                return path, data, caption_analysis_text(data)
        import yt_dlp
        from yt_dlp.networking import Request
        started = time.monotonic()
        options = download_options(task_id, video_id, budget)
        options.update({'quiet': True, 'no_warnings': True, 'skip_download': True,
                        'socket_timeout': 6, 'retries': 0, 'extractor_retries': 0})
        info = source_info(url, options, budget)
        selected = choose_original_caption(info)
        if not selected:
            return None
        language, fmt, automatic = selected
        parsed = urlparse(fmt.get('url') or '')
        if (parsed.scheme != 'https' or parsed.hostname not in ('www.youtube.com', 'youtube.com')
                or parsed.path != '/api/timedtext' or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        with yt_dlp.YoutubeDL(options) as client:
            with client.urlopen(Request(fmt['url'], headers=info.get('http_headers') or {})) as response:
                payload = response.read(8_000_001)
        budget.add(len(payload))
        if len(payload) > 8_000_000:
            return None
        raw = json.loads(payload)
        data = parse_json3_transcript(raw, duration=duration, language=language)
        if len(data['words']) < 6 or data['words'][-1]['end'] - data['words'][0]['start'] < 15000:
            return None
        data.update(is_auto_generated=automatic, source_video_id=video_id,
                    timings={'caption_fetch_seconds': round(time.monotonic() - started, 3), 'transcription_seconds': 0})
        folder.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, raw)
        _atomic_json(sidecar, data)
        logger.info('youtube_fast_captions video_id=%s words=%d timing=%s seconds=%.3f',
                    video_id, len(data['words']), data['timing_quality'], time.monotonic() - started)
        return path, data, caption_analysis_text(data)
    except DownloadBudgetExceeded:
        raise
    except Exception as exc:
        logger.info('youtube_fast_captions_unavailable video_id=%s error_type=%s', video_id, type(exc).__name__)
        return None
