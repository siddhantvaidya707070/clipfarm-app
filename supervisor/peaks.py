"""Deterministic, local highlight detection with no model or numerical dependency.

Transcript word timestamps are milliseconds; all returned windows and chat times
are seconds. Scores are heuristic evidence strengths, not calibrated probabilities.
Audio is read one second at a time. Zero crossings describe timbre variation: they
are deliberately not presented as measured pitch. Missing modalities contribute
zero, rather than guessed evidence. No target count can manufacture a highlight.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, insort
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import uuid
import wave


TYPES = ("NEW_MECHANIC", "FIRST_REACTION", "RAGE", "SHOCK", "FUNNY", "OTHER")
_PHRASES = {
    "NEW_MECHANIC": (
        "you can actually", "i can actually", "didn't know you could", "did not know you could",
        "i didn't know", "i just discovered", "just found out", "hidden garage", "secret location",
        "new mechanic", "unlock a", "you can do that", "how did i not know", "first heist",
        "i found a secret", "this glitch unlocked", "wait it does that",
    ),
    "FIRST_REACTION": (
        "first time playing", "first time seeing", "first time trying", "my first reaction",
        "never seen this before", "never played this before", "first look", "first impression",
        "first time i've", "first time i have",
    ),
    "RAGE": (
        "fuck this", "fucking game", "i hate this", "stupid game", "this is bullshit",
        "bullshit", "rage quit", "are you kidding me", "so annoying", "i'm so mad", "i am so mad",
        "god damn it", "goddammit", "piss off", "this stupid", "i'm done with this",
    ),
    "SHOCK": (
        "oh my god", "oh my goodness", "no way", "what the hell", "what the fuck",
        "holy shit", "holy fuck", "oh shit", "no fucking way", "what just happened",
        "that is insane", "that's insane", "unbelievable", "i can't believe",
    ),
    "FUNNY": (
        "hahaha", "hahah", "hilarious", "dying laughing", "can't stop laughing",
        "cannot stop laughing", "that was funny", "that's so funny", "that is so funny",
        "laughing so hard", "lmao", "rofl", "[laughter]", "[laughs]",
    ),
}
_PROFANITY = re.compile(r"\b(?:fuck\w*|shit\w*|bullshit|bitch\w*|damn\w*|asshole\w*)\b", re.I)
_STRETCH = re.compile(r"([a-z])\1{2,}", re.I)
_GTA = re.compile(r"\b(?:heist|garage|wanted level|police|cops|mission|gta|los santos|car|vehicle|weapon|easter egg|secret|unlock|hidden)\b", re.I)
_LAUGHTER_EMOTES = {"KEKW", "OMEGALUL", "LUL"}


def load_config() -> dict:
    """Return a fresh copy of the colocated, editable scoring configuration."""
    return json.loads(Path(__file__).with_name("highlight_config.json").read_text(encoding="utf-8"))


def _config(overrides: dict | None) -> dict:
    config = load_config()
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def _number(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def _unit(value) -> float:
    return min(1.0, max(0.0, _number(value)))


def _text_signals(text: str) -> dict:
    lowered = text.lower().replace("’", "'")
    # Multiple phrases reinforce classification, but a single precise cue can
    # qualify without unavailable chat or audio. Generic GTA nouns cannot.
    evidence = {kind.lower(): sum(phrase in lowered for phrase in phrases) for kind, phrases in _PHRASES.items()}
    tokens = re.findall(r"[\w']+", text)
    letters = [token for token in tokens if len(token) >= 2 and any(char.isalpha() for char in token)]
    evidence.update({
        "transcript_cues": float(any(evidence.values())),
        "profanity": min(1.0, len(_PROFANITY.findall(text)) / 2),
        "gta_discovery": float(bool(evidence["new_mechanic"] and _GTA.search(text))),
        "caps": sum(token.isupper() for token in letters) / max(1, len(letters)),
        "stretched_words": min(1.0, sum(bool(_STRETCH.search(token)) for token in tokens) / 2),
    })
    return evidence


def classify_clip(text: str, signals: dict) -> str:
    """Classify genuine lexical evidence; volume alone remains OTHER.

    Specific discovery and first-experience cues win ties over generic exclamations.
    Specific laughter emotes can establish a funny audience reaction; generic
    emotes and volume alone cannot determine a reaction category.
    """
    evidence = _text_signals(text)
    strengths = {kind: max(_number(signals.get(kind.lower())), evidence.get(kind.lower(), 0)) for kind in TYPES[:-1]}
    strengths["FUNNY"] = max(strengths["FUNNY"], _unit(signals.get("chat_laughter")))
    best = max(strengths, key=strengths.get)
    return best if strengths[best] > 0 else "OTHER"


def _words(transcript: dict, duration: float) -> list[dict]:
    source = transcript.get("words") or [word for segment in transcript.get("segments", []) for word in segment.get("words", [])]
    words = []
    for word in source:
        start = max(0.0, _number(word.get("start")) / 1000)
        end = min(duration, _number(word.get("end")) / 1000)
        text = str(word.get("text", word.get("word", ""))).strip()
        if text and start < duration and end > start:
            words.append({"start": start, "end": end, "text": text})
    return sorted(words, key=lambda word: (word["start"], word["end"]))


def _rolling_medians(values: list[float], window: int) -> list[float]:
    """Prior-window median in O(n * window), without sorting every window."""
    ordered = []
    result = []
    for index, value in enumerate(values):
        length = len(ordered)
        middle = length // 2
        result.append(0.0 if not length else ordered[middle] if length % 2 else (ordered[middle - 1] + ordered[middle]) / 2)
        insort(ordered, value)
        if index >= window:
            ordered.pop(bisect_left(ordered, values[index - window]))
    return result


def _smooth(values: list[float], radius: int = 1) -> list[float]:
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    return [(prefix[min(len(values), index + radius + 1)] - prefix[max(0, index - radius)]) / (min(len(values), index + radius + 1) - max(0, index - radius)) for index in range(len(values))]


def _audio_features(path: Path | None, seconds: int, config: dict) -> tuple[list[float], list[float], bool]:
    rms = [0.0] * seconds
    crossings = [0.0] * seconds
    if path is None or not Path(path).is_file():
        return rms, crossings, False
    process = None
    reader = None
    try:
        try:
            reader = wave.open(str(path), "rb")
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getcomptype() != "NONE" or reader.getframerate() > 16000:
                reader.close()
                reader = None
        except (wave.Error, EOFError):
            reader = None
        if reader is not None:
            sample_rate = reader.getframerate()
            read = lambda: reader.readframes(sample_rate)
        else:
            sample_rate = max(1000, int(config["audio_sample_rate"]))
            process = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            read = lambda: process.stdout.read(sample_rate * 2)
        measured = 0
        for index in range(seconds):
            raw = read()
            if not raw:
                break
            samples = array("h")
            samples.frombytes(raw[:len(raw) - len(raw) % 2])
            if sys.byteorder != "little":
                samples.byteswap()
            if not samples:
                continue
            # One linear pass per decoded sample, bounded memory for hour streams.
            power = 0
            changes = 0
            previous = samples[0]
            for sample in samples:
                power += sample * sample
                changes += (sample >= 0) != (previous >= 0)
                previous = sample
            rms[index] = math.sqrt(power / len(samples)) / 32768
            crossings[index] = changes / len(samples)
            measured += 1
        return rms, crossings, measured > 0
    except (OSError, ValueError, EOFError, wave.Error):
        return [0.0] * seconds, [0.0] * seconds, False
    finally:
        if reader is not None:
            reader.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def _overlap(a: dict, b: dict) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def merge_windows(windows: list[dict], duration: float | None = None, max_duration: float = 90, max_overlap: float = 5) -> list[dict]:
    """Collapse competing windows to their strongest representative.

    This is score-ordered non-maximum suppression, not transitive interval union:
    a chain of adjacent peaks cannot turn into an unbounded clip. Input objects
    are copied, bounds are clamped, and up to five seconds of overlap is allowed.
    """
    normalized = []
    for source in windows:
        candidate = deepcopy(source)
        candidate["start"] = max(0.0, _number(candidate.get("start")))
        candidate["end"] = min(_number(candidate.get("end")), candidate["start"] + max_duration)
        if duration is not None:
            candidate["end"] = min(candidate["end"], duration)
        if candidate["end"] > candidate["start"]:
            normalized.append(candidate)
    chosen = []
    for candidate in sorted(normalized, key=lambda item: (-_number(item.get("score")), item["start"], str(item.get("id", "")))):
        if all(_overlap(candidate, previous) <= max_overlap for previous in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda item: (item["start"], item["end"]))


def _snap_window(peak: int, scores: list[float], words: list[dict], starts: list[float], duration: float, config: dict) -> tuple[float, float]:
    start = max(0.0, peak - config["pre_roll_seconds"])
    # Keep eight seconds after three quiet seconds, allowing a sustained reaction
    # to finish while the preferred maximum keeps the window bounded.
    settle = peak + config["minimum_post_roll_seconds"]
    settle_limit = peak + max(config["minimum_post_roll_seconds"], config["preferred_max_seconds"] - config["pre_roll_seconds"] - config["minimum_post_roll_seconds"])
    while settle < min(len(scores), settle_limit):
        if max(scores[max(peak + 1, settle - 2):settle + 1], default=0) < scores[peak] * 0.45:
            break
        settle += 1
    end = min(duration, float(settle + config["minimum_post_roll_seconds"]))
    desired = min(duration, max(config["preferred_min_seconds"], config["pre_roll_seconds"] + config["post_roll_seconds"]))
    if end - start < desired:
        end = min(duration, start + desired)
        if end - start < desired:
            start = max(0.0, end - desired)
    radius = config["boundary_snap_seconds"]
    # Gaps already are safe boundaries. Snap only within a nearby phrase, never
    # jump across a long quiet interval to an unrelated word.
    left = bisect_left(starts, max(0.0, start - radius))
    right = bisect_left(starts, end + radius + 1)
    nearby = words[left:right]
    start_options = [(word["start"], index == 0 or bool(re.search(r"[.!?]$", nearby[index - 1]["text"]))) for index, word in enumerate(nearby) if abs(word["start"] - start) <= radius and word["start"] <= peak]
    end_options = [(word["end"], bool(re.search(r"[.!?]$", word["text"]))) for word in nearby if abs(word["end"] - end) <= radius and word["end"] > peak]
    if start_options:
        start = min(start_options, key=lambda option: (not option[1], abs(option[0] - start)))[0]
    if end_options:
        end = min(end_options, key=lambda option: (not option[1], abs(option[0] - end)))[0]
    minimum = min(duration, config["hard_min_seconds"])
    if end - start < minimum:
        start = max(0.0, end - minimum)
        end = min(duration, max(end, start + minimum))
    return round(start, 3), round(min(duration, end, start + config["hard_max_seconds"]), 3)


def _promote_variety(selected: list[dict], candidates: list[dict], threshold: float, config: dict) -> list[dict]:
    """Rescue a nearby-quality genuine category without relabeling any moment."""
    selected = list(selected)
    margin = config["variety_score_margin"]
    overlap = config["maximum_overlap_seconds"]
    for candidate in sorted(candidates, key=lambda item: (-item["score"], item["start"])):
        present = {item["type"] for item in selected if item["type"] != "OTHER"}
        if len(present) >= config["variety_min_types"]:
            break
        if candidate["type"] in present or candidate["type"] == "OTHER" or candidate["score"] < max(config["minimum_threshold"], threshold - margin):
            continue
        conflicts = [item for item in selected if _overlap(item, candidate) > overlap]
        remaining = [item for item in selected if item not in conflicts]
        remaining_types = {item["type"] for item in remaining if item["type"] != "OTHER"}
        if any(item["score"] - candidate["score"] > margin for item in conflicts):
            continue
        if len(remaining_types | {candidate["type"]}) <= len(present):
            continue
        selected = remaining + [candidate]
    return sorted(selected, key=lambda item: item["start"])


def analyze_peaks(transcript: dict, duration: float, audio_path: Path | None = None, chat: list[dict] | None = None, config: dict | None = None) -> list[dict]:
    """Return all eligible non-overlapping highlight windows, strongest first.

    Detection and IDs are deterministic for identical inputs/configuration. The
    target is a fallback trigger, not a cap or promise. Unsupported variety and
    insufficient source content never cause synthetic clips or guessed labels.
    """
    duration = _number(duration)
    if duration <= 0:
        return []
    config = _config(config)
    seconds = math.ceil(duration)
    words = _words(transcript or {}, duration)
    starts = [word["start"] for word in words]
    word_bins = [[] for _ in range(seconds)]
    for word in words:
        word_bins[int(word["start"])].append(word["text"])
    chat_bins = [[] for _ in range(seconds)]
    for message in chat or []:
        when = _number(message.get("time"), -1)
        if 0 <= when < duration:
            chat_bins[int(when)].append(message)
    word_rate = _smooth([len(items) for items in word_bins], radius=2)
    chat_rate = _smooth([len(items) for items in chat_bins])
    window = max(1, int(config["rolling_baseline_seconds"]))
    word_baseline = _rolling_medians(word_rate, window)
    chat_baseline = _rolling_medians(chat_rate, window)
    rms, crossings, audio_available = _audio_features(audio_path, seconds, config)
    audio_baseline = _rolling_medians(rms, window)
    crossing_baseline = _rolling_medians(crossings, window)
    features = []
    scores = []
    contexts = []
    weights = config["weights"]
    for second in range(seconds):
        low, high = max(0, second - 2), min(seconds, second + 4)
        text = " ".join(token for items in word_bins[low:high] for token in items)
        contexts.append(text)
        signal = _text_signals(text)
        nearby_chat = [message for items in chat_bins[max(0, second - 1):min(seconds, second + 2)] for message in items]
        chat_text = " ".join(str(message.get("text", "")) for message in nearby_chat)
        chat_style = _text_signals(chat_text)
        signal["caps"] = max(signal["caps"], chat_style["caps"])
        signal["stretched_words"] = max(signal["stretched_words"], chat_style["stretched_words"])
        signal["chat_velocity"] = _unit((chat_rate[second] / max(0.4, chat_baseline[second]) - 1) / 6)
        signal["emote_spam"] = min(1.0, sum(bool(message.get("emotes")) for message in nearby_chat) / max(3, len(nearby_chat) / 2))
        laughter_messages = sum(any(str(emote).strip(": ").upper() in _LAUGHTER_EMOTES for emote in message.get("emotes", [])) for message in nearby_chat)
        signal["chat_laughter"] = min(1.0, laughter_messages / max(3, len(nearby_chat) / 2))
        signal["words_per_second_jump"] = _unit((word_rate[second] / max(1.0, word_baseline[second]) - 1) / 2)
        signal["audio_rms_spike"] = _unit((rms[second] / max(0.008, audio_baseline[second]) - 1.5) / 4.5) if audio_available else 0.0
        signal["zero_crossing_variation"] = _unit(abs(crossings[second] - crossing_baseline[second]) / 0.2) if audio_available and rms[second] > 0.008 else 0.0
        quiet_before = max(rms[max(0, second - 4):second], default=1.0) < max(0.005, audio_baseline[second] * 0.25)
        signal["silence_then_burst"] = float(audio_available and second >= 3 and quiet_before and rms[second] > max(0.025, audio_baseline[second] * 2.5))
        signal["audio_available"] = audio_available
        signal["chat_available"] = bool(chat)
        signal["transcript_available"] = bool(words)
        score = _unit(sum(_unit(signal.get(key, 0)) * max(0.0, _number(weight)) for key, weight in weights.items()))
        features.append(signal)
        scores.append(score)
    # A local peak plateau creates one candidate. Suppress only within a small
    # peak radius here; longer-window competition is handled by merge_windows.
    eligible = [index for index, score in enumerate(scores) if score >= config["minimum_threshold"]]
    peaks = []
    for index in sorted(eligible, key=lambda index: (-scores[index], -len(word_bins[index]), index)):
        if all(abs(index - previous) >= config["peak_separation_seconds"] for previous in peaks):
            peaks.append(index)
    candidates = []
    for peak in peaks:
        start, end = _snap_window(peak, scores, words, starts, duration, config)
        if end <= start:
            continue
        clip_words = words[bisect_left(starts, start):bisect_left(starts, end)]
        text = " ".join(word["text"] for word in clip_words)
        signal = {key: round(value, 4) if isinstance(value, float) else value for key, value in features[peak].items()}
        signal["peak_time"] = float(peak)
        score = round(scores[peak], 4)
        kind = classify_clip(contexts[peak], signal)
        identity = f"{duration:.3f}:{start:.3f}:{end:.3f}:{kind}:{text}"
        candidates.append({"id": uuid.uuid5(uuid.NAMESPACE_URL, identity).hex, "type": kind, "start": start, "end": end, "score": score, "signals": signal, "text": text, "threshold_lowered": False, "low_confidence": False})
    threshold = _unit(config["threshold"])
    minimum_threshold = min(threshold, _unit(config["minimum_threshold"]))
    target = max(1, math.ceil(config["target_per_hour"] * duration / 3600))
    def select(cutoff):
        return merge_windows([candidate for candidate in candidates if candidate["score"] >= cutoff], duration, config["hard_max_seconds"], config["maximum_overlap_seconds"])
    selected = select(threshold)
    effective_threshold = threshold
    step = max(0.01, _number(config["threshold_step"]))
    while len(selected) < target and effective_threshold > minimum_threshold:
        effective_threshold = max(minimum_threshold, round(effective_threshold - step, 8))
        selected = select(effective_threshold)
    selected = _promote_variety(selected, candidates, effective_threshold, config)
    for clip in selected:
        lowered = clip["score"] < threshold
        clip["threshold_lowered"] = lowered
        clip["low_confidence"] = lowered or (clip["type"] == "OTHER" and sum(bool(clip["signals"].get(key)) for key in ("audio_rms_spike", "chat_velocity", "transcript_cues")) < 2)
        clip["signals"]["selection_threshold"] = round(min(effective_threshold, clip["score"]) if lowered else threshold, 4)
    return sorted(selected, key=lambda item: (-item["score"], item["start"]))
