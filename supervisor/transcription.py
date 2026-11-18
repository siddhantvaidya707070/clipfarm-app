"""Resumable OpenAI file transcription, shared with the native backend.

Only stdlib and httpx are required. GPT-Transcribe returns text, not word
timestamps: timings below distribute words across detected speech intervals.
They are estimates, including when a speech interval is long.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

import httpx


CHUNK_MS = 600_000
OVERLAP_MS = 4_000
CACHE_VERSION = 4
AUDIO_CACHE_VERSION = 3
SHORT_CHUNK_MS = 60_000


class TranscriptionError(RuntimeError):
    """A safe, user-visible transcription failure without provider payloads."""


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _run(command: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        raise TranscriptionError("Audio preparation failed; check ffmpeg/ffprobe availability and media.") from None
    if result.returncode:
        raise TranscriptionError("Audio preparation failed; check that the source contains readable audio.")
    return result


def speech_intervals(log: str, duration_ms: int) -> list[tuple[int, int]]:
    """Invert ffmpeg silencedetect events into millisecond speech intervals."""
    intervals = []
    cursor = 0
    for match in re.finditer(r"silence_(start|end):\s*([\d.]+)", log):
        position = min(duration_ms, max(0, round(float(match[2]) * 1000)))
        if match[1] == "start":
            if position > cursor:
                intervals.append((cursor, position))
        cursor = max(cursor, position)
    # A final silence_start without silence_end extends to the media end.
    events = list(re.finditer(r"silence_(start|end):\s*([\d.]+)", log))
    if (not events or events[-1][1] == "end") and cursor < duration_ms:
        intervals.append((cursor, duration_ms))
    return intervals


def _prepare_audio(media_path: Path, cache: Path, audio_stream_index: int | None = None) -> tuple[Path, int, list]:
    """Extract once; subsequent models and retries reuse this compact MP3."""
    audio = cache / "audio.mp3"
    metadata_path = cache / "audio.json"
    metadata = _read_json(metadata_path)
    if audio.exists() and audio.stat().st_size and isinstance(metadata, dict):
        if metadata.get("duration_ms", 0) > 0 and isinstance(metadata.get("speech"), list):
            return audio, metadata["duration_ms"], metadata["speech"]
    detected = None
    if not audio.exists() or not audio.stat().st_size:
        temporary = cache / f"audio.{uuid.uuid4().hex}.mp3"
        try:
            prepared = False
            if media_path.suffix.lower() == ".mp3":
                source_probe = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                     "stream=codec_name,sample_rate,channels", "-of", "json", str(media_path)], timeout=60)
                try:
                    stream = json.loads(source_probe.stdout)["streams"][0]
                    prepared = (stream.get("codec_name") == "mp3" and
                                int(stream.get("sample_rate", 0)) == 16000 and stream.get("channels") == 1)
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
            if prepared:
                shutil.copyfile(media_path, temporary)
            else:
                mapping = ["-map", f"0:a:{audio_stream_index}"] if audio_stream_index is not None else []
                detected = _run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(media_path), *mapping, "-vn",
                      "-ac", "1", "-ar", "16000", "-af", "silencedetect=noise=-35dB:d=0.35", "-b:a", "64k", str(temporary)])
            temporary.replace(audio)
        finally:
            temporary.unlink(missing_ok=True)
    probe = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "json", str(audio)], timeout=60)
    try:
        duration_ms = round(float(json.loads(probe.stdout)["format"]["duration"]) * 1000)
        if duration_ms <= 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise TranscriptionError("Could not determine audio duration.") from None
    if detected is None:
        detected = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-af",
                         "silencedetect=noise=-35dB:d=0.35", "-f", "null", "-"])
    intervals = speech_intervals(detected.stderr, duration_ms)
    _write_json(metadata_path, {"duration_ms": duration_ms, "speech": intervals})
    return audio, duration_ms, intervals


def _split_audio(audio: Path, target: Path, start_ms: int, duration_ms: int) -> None:
    if target.exists() and target.stat().st_size:
        return
    temporary = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.mp3")
    try:
        _run(["ffmpeg", "-v", "error", "-y", "-ss", str(start_ms / 1000), "-i", str(audio),
              "-t", str(duration_ms / 1000), "-c", "copy", str(temporary)])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _utterances(words: list[dict]) -> list[dict]:
    result = []
    current = []
    for word in words:
        if current and (word["start"] - current[-1]["end"] > 350 or
                        word["end"] - current[0]["start"] > 8000 or len(current) >= 30):
            result.append(_utterance(current))
            current = []
        current.append(word)
        if re.search(r"[.!?]$", word["text"]):
            result.append(_utterance(current))
            current = []
    if current:
        result.append(_utterance(current))
    return result


def _utterance(words: list[dict]) -> dict:
    return {"text": " ".join(word["text"] for word in words), "start": words[0]["start"],
            "end": words[-1]["end"], "speaker": None, "words": words,
            "timing_quality": "word" if all(word.get("timing_quality") == "word" for word in words) else "approximate"}


def align_text(text: str, intervals: list[tuple[int, int]]) -> dict:
    """Estimate words by speech duration; never claim acoustically aligned words."""
    tokens = text.split()
    intervals = [(int(start), int(end)) for start, end in intervals if end > start]
    total = sum(end - start for start, end in intervals)
    if not tokens or total <= 0:
        return {"text": text, "words": [], "utterances": []}
    words = []
    consumed = 0
    token_index = 0
    for interval_index, (start, end) in enumerate(intervals):
        consumed += end - start
        boundary = len(tokens) if interval_index == len(intervals) - 1 else round(consumed / total * len(tokens))
        count = boundary - token_index
        for index in range(count):
            words.append({"text": tokens[token_index + index],
                          "start": start + round((end - start) * index / count),
                          "end": start + round((end - start) * (index + 1) / count),
                          "confidence": None, "speaker": None, "timing_quality": "approximate"})
        token_index = boundary
    return {"text": text, "words": words, "utterances": _utterances(words)}


def stitch_chunks(chunks: list[dict]) -> dict:
    """Offset local millisecond words and remove duplicates only within overlaps.

    Each chunk supplies offset_ms, duration_ms and words. Repeated speech outside
    the actual overlap is retained; no global text replacement is used.
    """
    words = []
    previous_end = 0
    texts = []
    for chunk in sorted(chunks, key=lambda item: item.get("offset_ms", 0)):
        offset = int(chunk.get("offset_ms", 0))
        incoming = [dict(word, start=int(word["start"]) + offset, end=int(word["end"]) + offset)
                    for word in chunk.get("words", [])]
        duplicate_count = 0
        if words and offset < previous_end:
            tail = [word for word in words if word["end"] > offset]
            head = [word for word in incoming if word["start"] < previous_end]
            normalize = lambda word: re.sub(r"[^\w]", "", word["text"]).casefold()
            for count in range(min(len(tail), len(head)), 0, -1):
                if ([normalize(word) for word in tail[-count:]] ==
                        [normalize(word) for word in head[:count]]):
                    duplicate_count = count
                    break
        # Approximate timings can disagree near a seam; maintain valid ordering.
        for word in incoming[duplicate_count:]:
            word["start"] = max(word["start"], words[-1]["end"] if words else 0)
            word["end"] = max(word["start"], word["end"])
            words.append(word)
        if chunk.get("text"):
            texts.append(chunk["text"])
        previous_end = max(previous_end, offset + int(chunk.get("duration_ms", 0)))
    return {"text": " ".join(word["text"] for word in words) if words else " ".join(texts),
            "words": words, "utterances": _utterances(words)}


def _request_transcript(client: httpx.Client, path: Path, model: str, language: str | None) -> dict:
    data = {"model": model, "response_format": "verbose_json" if model == "whisper-1" else "json"}
    if model == "gpt-transcribe" or model.startswith("gpt-transcribe-"):
        data["chunking_strategy"] = "auto"
    if model == "whisper-1":
        data["timestamp_granularities[]"] = "word"
    if language:
        data["languages[]" if model == "gpt-transcribe" or model.startswith("gpt-transcribe-") else "language"] = language
    for attempt in range(4):
        status = None
        try:
            with path.open("rb") as audio:
                response = client.post("audio/transcriptions", data=data,
                                       files={"file": (path.name, audio, "audio/mpeg")})
            status = response.status_code
        except httpx.TransportError:
            if attempt == 3:
                raise TranscriptionError("OpenAI transcription unavailable after 4 attempts (network timeout or connection failure).") from None
        else:
            if 200 <= status < 300:
                try:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                        raise ValueError
                    detected_language = payload.get("language")
                    languages = payload.get("languages")
                    if isinstance(languages, list):
                        detected_language = next((item.get("code") for item in languages
                                                  if isinstance(item, dict) and isinstance(item.get("code"), str)), None)
                    return {"text": payload["text"], "language": detected_language, "words": payload.get("words")}
                except (ValueError, TypeError):
                    raise TranscriptionError("OpenAI transcription returned an invalid transcript.") from None
            if status != 429 and status < 500:
                raise TranscriptionError(f"OpenAI transcription rejected the request (HTTP {status}); check model, language and credentials.")
            if attempt == 3:
                raise TranscriptionError(f"OpenAI transcription unavailable after 4 attempts (HTTP {status}).")
        time.sleep(min(8.0, 2.0 ** attempt))
    raise TranscriptionError("OpenAI transcription unavailable.")


def _concurrency() -> int:
    try:
        return min(8, max(4, int(os.getenv("OPENAI_TRANSCRIPTION_CONCURRENCY", "6"))))
    except ValueError:
        return 6


def _short_chunks(start: int, duration: int, intervals: list) -> list[tuple[int, int]]:
    """Prefer nearby silence boundaries; cap timing anchors at one minute."""
    chunks = []
    limit = start + duration
    cursor = start
    while cursor < limit:
        end = min(limit, cursor + SHORT_CHUNK_MS)
        if end < limit:
            boundaries = [right for _, right in intervals if end - 8000 <= right <= end and right > cursor + 30000]
            if boundaries:
                end = max(boundaries)
        chunks.append((cursor, end - cursor))
        if end == limit:
            break
        cursor = end - 1000
    return chunks


def _suspicious_text(text: str, speech_ms: int, duration: int) -> bool:
    # silencedetect measures acoustic activity, which can also be music. A
    # short model probe confirms actual speech before an empty long response
    # triggers full subdivision. Character count covers unspaced languages.
    units = max(len(text.split()), len(re.sub(r"\s", "", text)) / 6)
    return duration > SHORT_CHUNK_MS and speech_ms >= 15000 and units < speech_ms / 1000 * 0.65


def transcribe_media(media_path: Path, cache_dir: Path | None = None, source_id: str | None = None,
                     model: str | None = None, language: str | None = None,
                     api_key: str | None = None, base_url: str | None = None, audio_stream_index: int | None = None) -> dict:
    """Return schema v2 with millisecond words and honestly approximate timings.

    600-second uploads overlap by four seconds; successful chunks persist even
    if a later chunk fails. No local model is imported or silently substituted.
    """
    started = time.monotonic()
    key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
    if not key:
        raise TranscriptionError("OPENAI_API_KEY is required for OpenAI transcription.")
    model = (model or os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-transcribe")).strip()
    if not model or "diarize" in model.lower():
        raise TranscriptionError("Use a file-transcription model supporting JSON text responses; diarize models are unsupported.")
    language = (language or "").strip().lower().replace("-", "_").split("_", 1)[0] or None
    if language and not re.fullmatch(r"[a-z]{2,3}", language):
        raise TranscriptionError("Transcription language must be an ISO language code or empty for auto-detection.")
    endpoint = (base_url or os.getenv("OPENAI_TRANSCRIPTION_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/"
    media_path = Path(media_path)
    identity = [AUDIO_CACHE_VERSION, source_id, _digest_file(media_path)]
    if audio_stream_index is not None:
        if not isinstance(audio_stream_index, int) or audio_stream_index < 0:
            raise TranscriptionError("Invalid audio stream index")
        identity.append(audio_stream_index)
    source_key = _key(identity)
    cache = Path(cache_dir) if cache_dir is not None else media_path.parent / ".transcription_cache"
    source_cache = cache / source_key
    source_cache.mkdir(parents=True, exist_ok=True)
    audio_started = time.monotonic()
    audio, duration_ms, intervals = (_prepare_audio(media_path, source_cache, audio_stream_index)
                                   if audio_stream_index is not None else _prepare_audio(media_path, source_cache))
    extraction_seconds = time.monotonic() - audio_started
    audio_signature = _digest_file(audio)
    chunks = []
    try:
        chunk_ms = max(SHORT_CHUNK_MS, min(CHUNK_MS, int(os.getenv("OPENAI_TRANSCRIPTION_CHUNK_SECONDS", "600")) * 1000))
    except ValueError:
        chunk_ms = CHUNK_MS
    for start in range(0, duration_ms, chunk_ms - OVERLAP_MS):
        duration = min(chunk_ms, duration_ms - start)
        chunks.append((start, duration))
        if start + duration >= duration_ms:
            break
    transcription_started = time.monotonic()
    with httpx.Client(base_url=endpoint, headers={"Authorization": f"Bearer {key}"},
                      timeout=httpx.Timeout(180.0, connect=20.0), follow_redirects=False) as client:
        # Long input language detection can return HTTP 200 with whitespace,
        # despite clear speech. Detect on bounded audio and reuse the hint.
        if not language and duration_ms > SHORT_CHUNK_MS and intervals and (model == "gpt-transcribe" or model.startswith("gpt-transcribe-")):
            detection_path = source_cache / f"language-{_key([CACHE_VERSION, audio_signature, model, endpoint])}.json"
            detection = _read_json(detection_path)
            if not isinstance(detection, dict):
                detection = {"language": None}
                positions = [intervals[0][0], duration_ms // 2, max(0, duration_ms - 30000)]
                for position in dict.fromkeys(positions):
                    probe_duration = min(30000, duration_ms - position)
                    probe = source_cache / f"language-probe-{position}-{probe_duration}.mp3"
                    _split_audio(audio, probe, position, probe_duration)
                    detected = _request_transcript(client, probe, model, None)
                    code = detected.get("language")
                    if isinstance(code, str) and re.fullmatch(r"[a-z]{2,3}", code):
                        detection["language"] = code
                        break
                _write_json(detection_path, detection)
            language = detection.get("language")

        def process(item):
            start, duration = item
            chunk_path = source_cache / f"chunk-{start}-{duration}.mp3"
            cache_key = _key([CACHE_VERSION, audio_signature, model, language, endpoint, start, duration])
            result_path = source_cache / f"transcript-{cache_key}.json"
            cached = _read_json(result_path)
            valid_cached = isinstance(cached, dict) and isinstance(cached.get("words"), list) and isinstance(cached.get("text"), str)
            if valid_cached and cached.get("complete"):
                return cached
            if valid_cached and isinstance(cached.get("response"), dict):
                response = cached["response"]
            else:
                _split_audio(audio, chunk_path, start, duration)
                response = _request_transcript(client, chunk_path, model, language)
            local_intervals = [(max(start, left) - start, min(start + duration, right) - start)
                               for left, right in intervals if right > start and left < start + duration]
            # A returned text for undetected quiet speech still gets clearly
            # marked estimates, rather than silently disappearing.
            if model == "whisper-1" and response.get("words"):
                provider_words = []
                try:
                    for word in response["words"]:
                        start_ms, end_ms = float(word["start"]) * 1000, float(word["end"]) * 1000
                        if not math.isfinite(start_ms) or not math.isfinite(end_ms) or end_ms < start_ms:
                            raise ValueError
                        provider_words.append({"text": str(word["word"]).strip(),
                                               "start": min(duration, max(0, round(start_ms))),
                                               "end": min(duration, max(0, round(end_ms))),
                                               "confidence": None, "speaker": None, "timing_quality": "word"})
                except (ValueError, TypeError, KeyError):
                    raise TranscriptionError("OpenAI transcription returned invalid word timestamps.") from None
                result = {"text": response["text"], "words": provider_words, "utterances": _utterances(provider_words)}
            else:
                result = align_text(response["text"], local_intervals or [(0, duration)])
            result.update(offset_ms=start, duration_ms=duration, language=response.get("language"))
            speech_ms = sum(right - left for left, right in local_intervals)
            suspicious = _suspicious_text(response["text"], speech_ms, duration)
            result.update(complete=not suspicious, response=response)
            _write_json(result_path, result)
            if suspicious:
                children = _short_chunks(start, duration, intervals)
                confirmed_speech = bool(response["text"].strip())
                # Music also defeats silencedetect. Avoid paying for every
                # minute of it when three bounded probes find no spoken text.
                if not confirmed_speech:
                    for index in dict.fromkeys([0, len(children) // 2, len(children) - 1]):
                        if process(children[index])["words"]:
                            confirmed_speech = True
                            break
                if confirmed_speech:
                    recovered = stitch_chunks([process(child) for child in children])
                    local_words = [dict(word, start=word["start"] - start, end=word["end"] - start)
                                   for word in recovered["words"]]
                    result.update(text=recovered["text"], words=local_words, utterances=_utterances(local_words),
                                  recovery="short_speech_chunks", subchunks=len(children))
                else:
                    result["speech_status"] = "no_speech_detected_in_probes"
                result["complete"] = True
                _write_json(result_path, result)
            return result
        with ThreadPoolExecutor(max_workers=_concurrency()) as executor:
            results = list(executor.map(process, chunks))
    transcript = stitch_chunks(results)
    timing_quality = "word" if transcript["words"] and all(word.get("timing_quality") == "word" for word in transcript["words"]) else "approximate"
    transcript.update(version=2, timing_quality=timing_quality, model=model,
                      language_code=language or next((r.get("language") for r in results if r.get("language")), None))
    transcript["timings"] = {"audio_extraction_seconds": round(extraction_seconds, 6),
                             "transcription_seconds": round(time.monotonic() - transcription_started, 6),
                             "total_seconds": round(time.monotonic() - started, 6)}
    return transcript
