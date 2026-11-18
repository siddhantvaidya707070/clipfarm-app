"""Reject near-empty audio in selected footage; this is not speech recognition."""
from pathlib import Path
import math
import subprocess

import numpy as np

from ..youtube_source import ranges_from_segments


def filter_silent_highlights(video_path: Path, segments: list[dict], time_map=None):
    """Decode only audio once, then measure every selected interval in memory.

    A decoder failure is reported as unverified rather than mislabeled silence.
    Music can be active audio: the transcript/AI still decides semantic value.
    """
    try:
        result = subprocess.run([
            'ffmpeg', '-v', 'error', '-nostdin', '-i', str(video_path),
            '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '1000', '-f', 'f32le', 'pipe:1',
        ], capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('Audio decoder failed')
        samples = np.frombuffer(result.stdout, dtype='<f4')
        count = len(samples) // 100
        if not count:
            raise ValueError('Audio decoder returned no samples')
        frames = samples[:count * 100].reshape(count, 100)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
    except (OSError, ValueError, subprocess.SubprocessError):
        return list(segments), [{'index': i, 'decision': 'unverified'} for i in range(len(segments))]
    kept, metrics = [], []
    for index, segment in enumerate(segments):
        ranges = ranges_from_segments([segment])
        if time_map is not None:
            ranges = time_map.to_local_ranges(ranges)
        pieces = [rms[max(0, math.floor(start * 10)):min(count, math.ceil(end * 10))]
                  for start, end in ranges if math.isfinite(start) and math.isfinite(end) and end > start]
        pieces = [piece for piece in pieces if len(piece)]
        if not pieces:
            kept.append(segment)
            metrics.append({'index': index, 'decision': 'unverified'})
            continue
        values = np.concatenate(pieces)
        fraction = float(np.mean(values >= .003))
        peak = float(np.max(values))
        # A near-empty recording or a click surrounded by silence is not a
        # spoken highlight. Natural breaths remain for the pacing cleanup.
        silent = peak < .004 or fraction < .05
        metrics.append({'index': index, 'decision': 'silent' if silent else 'keep',
                        'active_fraction': round(fraction, 5), 'peak_rms': round(peak, 6)})
        if not silent:
            kept.append(segment)
    return kept, metrics
