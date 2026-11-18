"""Local, grounded selection for the explicit fast YouTube highlights mode.

These scores are heuristics, not editorial or factual judgments. Weak selections
carry a visible low-confidence explanation; silence never becomes invented text.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from pathlib import Path
import math
import re
import subprocess

from ..clip_duration import clip_duration
from ..media.timeline import seconds_to_mmss

FAST_HIGHLIGHT_VERSION = 'fast-local-fallback-v2'
REACTION = re.compile(r'\b(?:wow|whoa|what|amazing|incredible|unbelievable|finally|never|first|why|how|insane|laugh\w*|hilarious|surpris\w*|discover\w*|watch|look|wait|oh my|no way)\b', re.I)


def audio_energy_bins(path: Path, duration: float) -> list[float]:
    """One compact decode, independent of the number of selected clips."""
    import numpy as np
    try:
        result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1',
                                 '-ar', '1000', '-t', str(duration), '-f', 's16le', 'pipe:1'],
                                capture_output=True, timeout=20)
        if result.returncode:
            return []
        samples = np.frombuffer(result.stdout, dtype='<i2').astype(np.float32)
        count = len(samples) // 1000
        if not count:
            return []
        energy = np.sqrt(np.mean(samples[:count * 1000].reshape(count, 1000) ** 2, axis=1))
        baseline = max(100, float(np.percentile(energy, 75)))
        return np.minimum(3, energy / baseline).tolist()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def select_fast_highlights(transcript_data: dict, duration: float, *, clip_length: str = 'auto',
                           max_clips: int | None = None, per_hour: int = 18, cap: int = 20,
                           audio_energy: list[float] | None = None, instructions: str = '') -> list[dict]:
    """Signal-qualified fallback only: never fill a quota with ordinary speech."""
    if not math.isfinite(duration) or duration < 15:
        return []
    words = []
    for item in (transcript_data or {}).get('words', []):
        try:
            start, end = float(item['start']) / 1000, float(item['end']) / 1000
            text = str(item.get('text') or '').strip()
            if text and math.isfinite(start) and math.isfinite(end) and 0 <= start < end and start < duration:
                words.append({'start': start, 'end': min(duration, end), 'text': text})
        except (KeyError, ValueError, TypeError):
            continue
    words.sort(key=lambda w: (w['start'], w['end']))
    if not words:
        return []
    preset = clip_duration(clip_length)
    target_seconds = 24 if clip_length == 'auto' else {'short': 22, 'medium': 42, 'long': 90}[clip_length]
    target = min(cap, max(1, round(duration * per_hour / 3600)))
    if max_clips is not None:
        target = max(1, min(cap, int(max_clips)))
    target = min(target, max(1, int(duration // preset.minimum)))
    starts = [w['start'] for w in words]
    ends = [w['end'] for w in words]
    instruction_terms = set(re.findall(r'\w{4,}', instructions.lower()))
    anchors = {0}
    # Dense enough to locate local reactions without quadratic word-by-word scans.
    for second in range(0, math.ceil(duration), 8):
        position = bisect_left(starts, second)
        if position < len(words):
            anchors.add(position)
    for index, word in enumerate(words):
        if REACTION.search(word['text']) or '!' in word['text'] or '?' in word['text']:
            anchors.add(max(0, bisect_left(starts, max(0, word['start'] - 4))))
    candidates = []
    for anchor in sorted(anchors):
        start = words[anchor]['start']
        # Rewind to a nearby sentence/pause when the sparse anchor lands mid-thought.
        for index in range(anchor, max(-1, anchor - 12), -1):
            if start - words[index]['start'] > 3:
                break
            if index == 0 or re.search(r'[.!?][\"\']?$', words[index - 1]['text']) or words[index]['start'] - words[index - 1]['end'] > .5:
                anchor, start = index, words[index]['start']
                break
        low = bisect_left(ends, start + max(preset.minimum, target_seconds - 5))
        high = bisect_right(ends, min(duration, start + min(preset.maximum, target_seconds + 6)))
        if high <= low:
            continue
        sentence_ends = [index for index in range(low, high) if re.search(r'[.!?][\"\']?$', words[index]['text'])]
        last = min(sentence_ends or range(low, high), key=lambda i: abs(words[i]['end'] - start - target_seconds))
        end = words[last]['end']
        if not preset.minimum <= end - start <= preset.maximum:
            continue
        selected = [w for w in words[anchor:last + 1] if w['end'] > start and w['start'] < end]
        if len(selected) < 6:
            continue
        text = ' '.join(w['text'] for w in selected)
        reactions = len(REACTION.findall(text))
        emphatic = text.count('!') + .5 * text.count('?')
        density = len(selected) / (end - start)
        audio = (audio_energy or [])[int(start):math.ceil(end)]
        audio_peak = max(audio, default=1)
        audio_jump = max(0, audio_peak - 1.3)
        preference = len(instruction_terms & set(re.findall(r'\w+', text.lower())))
        evidence = min(1, reactions / 4 + emphatic / 8 + audio_jump / 3)
        score = min(.95, .15 + .6 * evidence + .1 * min(1, density / 3) + .05 * min(1, preference / 3))
        weak = evidence < .25
        if weak or density < .5:
            continue
        candidates.append({'start_time': seconds_to_mmss(start), 'end_time': seconds_to_mmss(end),
            'source_ranges': [(round(start, 3), round(end, 3))], 'keep_ranges': [(round(start, 3), round(end, 3))],
            'text': text, 'relevance_score': round(score, 4), 'fast_highlight': True,
            'selection_version': FAST_HIGHLIGHT_VERSION, 'selection_provider': 'local_fallback',
            'low_confidence': True, 'threshold_lowered': False,
            'reasoning': 'AI selection unavailable: local signal fallback (low confidence). Reaction language, emphasis or audio peak; review before publishing.',
            'virality_score': 0, 'hook_score': 0, 'engagement_score': 0, 'value_score': 0, 'shareability_score': 0,
            'hook_type': 'local_heuristic', 'hook_title': None, 'audio_peak': round(audio_peak, 3)})
    ranked = sorted(candidates, key=lambda c: (-c['relevance_score'], c['source_ranges'][0][0]))
    chosen = []
    def compatible(candidate):
        start, end = candidate['source_ranges'][0]
        return all(end <= item['source_ranges'][0][0] or start >= item['source_ranges'][0][1] for item in chosen)
    for candidate in ranked:
        if len(chosen) >= target:
            break
        if compatible(candidate):
            chosen.append(candidate)
    return sorted(chosen, key=lambda c: c['source_ranges'][0][0])
