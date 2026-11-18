"""Offline coverage for the shared, resumable transcription path."""

from pathlib import Path
import ast
import json
import logging
import time
from types import SimpleNamespace
import typing

import httpx
import pytest

from supervisor import transcription as tx


def word(text, start, end):
    return {"text": text, "start": start, "end": end, "confidence": None, "speaker": None}


def test_stitch_offsets_overlap_and_preserves_later_repeated_phrase():
    result = tx.stitch_chunks([
        {"offset_ms": 0, "duration_ms": 10000, "words": [word("Hello", 1000, 1500), word("again", 8500, 9000), word("friend.", 9000, 9500)]},
        {"offset_ms": 8000, "duration_ms": 10000, "words": [word("Again", 500, 1000), word("friend!", 1000, 1500), word("Again", 5000, 5500), word("friend.", 5500, 6000)]},
    ])
    assert [w["text"] for w in result["words"]] == ["Hello", "again", "friend.", "Again", "friend."]
    assert result["words"][-1]["start"] == 13500
    assert result["utterances"][-1]["end"] == 14000


def test_stitch_never_deduplicates_nonoverlapping_repetitions():
    result = tx.stitch_chunks([
        {"offset_ms": 0, "duration_ms": 1000, "words": [word("Yes", 100, 400)]},
        {"offset_ms": 2000, "duration_ms": 1000, "words": [word("Yes", 100, 400)]},
    ])
    assert result["text"] == "Yes Yes"
    assert [w["start"] for w in result["words"]] == [100, 2100]


def test_approximate_alignment_excludes_silence_and_marks_estimates():
    result = tx.align_text("One two three four.", [(1000, 3000), (7000, 9000)])
    assert [w["start"] for w in result["words"]] == [1000, 2000, 7000, 8000]
    assert all(w["confidence"] is None and w["timing_quality"] == "approximate" for w in result["words"])
    assert result["utterances"][1]["start"] == 7000


def test_silence_detection_handles_leading_and_trailing_silence():
    detected = "silence_start: 0\nsilence_end: 1.2 | silence_duration: 1.2\nsilence_start: 4\nsilence_end: 5 | silence_duration: 1\nsilence_start: 9"
    assert tx.speech_intervals(detected, 10000) == [(1200, 4000), (5000, 9000)]


def fake_audio(monkeypatch, tmp_path, duration=700000):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"original media")
    extracted = []

    def prepare(path, cache):
        extracted.append(path)
        audio = cache / "audio.mp3"
        audio.write_bytes(b"compressed audio")
        return audio, duration, [(0, duration)]

    def split(path, target, start_ms, duration_ms):
        target.write_bytes(f"{start_ms}/{duration_ms}".encode())

    monkeypatch.setattr(tx, "_prepare_audio", prepare)
    monkeypatch.setattr(tx, "_split_audio", split)
    return media, extracted


def test_multipart_uses_exact_model_without_unsupported_word_parameters(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=2000)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "Hello world."})

    real_client = httpx.Client
    monkeypatch.setattr(tx.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    result = tx.transcribe_media(media, cache_dir=tmp_path / "cache", api_key="secret")
    assert result["model"] == "gpt-transcribe"
    assert result["version"] == 2 and result["timing_quality"] == "approximate"
    assert result["words"][-1]["end"] == 2000
    body = requests[0].content
    assert b"gpt-transcribe" in body and b'timestamp_granularities' not in body
    assert b'verbose_json' not in body and b'name="response_format"\r\n\r\njson' in body


def test_completed_chunks_resume_and_model_change_invalidates(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path)
    calls = []

    def request(client, path, model, language):
        calls.append((path.name, model, language))
        return {"text": "One useful sentence. " * 500}

    monkeypatch.setattr(tx, "_request_transcript", request)
    cache = tmp_path / "cache"
    first = tx.transcribe_media(media, cache, api_key="test", language="en")
    assert len(calls) == 2
    resumed = tx.transcribe_media(media, cache, api_key="test", language="en")
    assert {k: v for k, v in resumed.items() if k != "timings"} == {k: v for k, v in first.items() if k != "timings"}
    assert resumed["timings"]["transcription_seconds"] >= 0
    assert len(calls) == 2
    tx.transcribe_media(media, cache, model="gpt-4o-transcribe", api_key="test", language="en")
    assert len(calls) == 4
    media.write_bytes(b"changed source")
    tx.transcribe_media(media, cache, api_key="test", language="en")
    assert len(calls) == 6


def test_transient_failure_retries_but_errors_never_expose_provider_body(monkeypatch, tmp_path):
    path = tmp_path / "chunk.mp3"
    path.write_bytes(b"audio")
    attempts = []
    sleeps = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(429 if len(attempts) < 3 else 200, json={"text": "ok"})

    monkeypatch.setattr(tx.time, "sleep", sleeps.append)
    with httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(respond)) as client:
        assert tx._request_transcript(client, path, "gpt-transcribe", None)["text"] == "ok"
    assert len(attempts) == 3 and sleeps == [1.0, 2.0]
    with httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(lambda r: httpx.Response(400, text="secret-key provider dump"))) as client:
        with pytest.raises(tx.TranscriptionError, match="HTTP 400") as error:
            tx._request_transcript(client, path, "gpt-transcribe", None)
    assert "secret-key" not in str(error.value)


def test_timeout_retry_is_bounded_and_sanitized(monkeypatch, tmp_path):
    path = tmp_path / "chunk.mp3"
    path.write_bytes(b"audio")
    attempts = []
    monkeypatch.setattr(tx.time, "sleep", lambda _: None)

    def fail(request):
        attempts.append(request)
        raise httpx.ReadTimeout("secret-key leaked in transport", request=request)

    with httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(tx.TranscriptionError, match="4 attempts") as error:
            tx._request_transcript(client, path, "gpt-transcribe", None)
    assert len(attempts) == 4 and "secret-key" not in str(error.value)


def test_missing_key_fails_before_extraction(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "source.mp4"
    path.write_bytes(b"media")
    with pytest.raises(tx.TranscriptionError, match="OPENAI_API_KEY"):
        tx.transcribe_media(path)


def test_chunk_concurrency_is_bounded(monkeypatch):
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_CONCURRENCY", "100")
    assert tx._concurrency() == 8
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_CONCURRENCY", "1")
    assert tx._concurrency() == 4


def adapter_functions():
    """Load pure adapter functions without optional CV/Whisper dependencies."""
    source = Path(__file__).parents[1] / "vendor/supoclip/backend/src/media/transcription.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {name: getattr(typing, name) for name in ("Any", "Dict", "List", "Optional")}
    namespace.update(Path=Path, json=json, time=time, logger=logging.getLogger(__name__),
                     TRANSCRIPT_CACHE_SCHEMA_VERSION=2)
    namespace.update({node.id: 100000 for node in ast.walk(tree)
                      if isinstance(node, ast.Name) and node.id.startswith("ANALYSIS_")})
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace


def test_native_adapter_preserves_v2_cache_and_millisecond_format(tmp_path):
    adapter = adapter_functions()
    payload = {"version": 2, "text": "Hello.", "timing_quality": "approximate", "model": "gpt-transcribe",
               "words": [word("Hello.", 1200, 2500)],
               "utterances": [{"text": "Hello.", "start": 1200, "end": 2500, "words": [word("Hello.", 1200, 2500)]}]}
    path = tmp_path / "source.mp4"
    adapter["cache_transcript_data"](path, payload)
    assert json.loads(path.with_suffix(".transcript_cache.json").read_text()) == payload
    assert adapter["format_transcript_for_analysis"](payload) == ["[00:01 - 00:02] Hello."]


def test_native_whisper_integer_seconds_convert_to_milliseconds():
    adapter = adapter_functions()
    result = adapter["_whisper_result_to_transcript_data"]({"segments": [
        {"start": 1, "end": 2, "text": "Hello.", "words": [{"word": "Hello.", "start": 1, "end": 2}]}]})
    assert result["words"][0]["start"] == 1000
    assert result["words"][0]["end"] == 2000


def test_prepared_native_mp3_is_reused_without_another_encode(monkeypatch, tmp_path):
    path = tmp_path / "native.transcription.mp3"
    path.write_bytes(b"mono 16k audio")
    cache = tmp_path / "cache"
    cache.mkdir()
    commands = []

    def run(command, timeout=900):
        commands.append(command)
        if "stream=codec_name,sample_rate,channels" in command:
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_name": "mp3", "sample_rate": "16000", "channels": 1}]}))
        if "format=duration" in command:
            return SimpleNamespace(stdout='{"format":{"duration":"2"}}')
        return SimpleNamespace(stderr="")

    monkeypatch.setattr(tx, "_run", run)
    audio, duration, _ = tx._prepare_audio(path, cache)
    assert audio.read_bytes() == path.read_bytes() and duration == 2000
    assert not any("-b:a" in command for command in commands)
    first_count = len(commands)
    tx._prepare_audio(path, cache)
    assert len(commands) == first_count


def test_failure_keeps_completed_chunks_for_retry(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path)
    completed = []
    failing = True

    def request(client, path, model, language):
        if "596000" in path.name and failing:
            raise tx.TranscriptionError("temporary outage")
        completed.append(path.name)
        return {"text": "A successful chunk. " * 500}

    monkeypatch.setattr(tx, "_request_transcript", request)
    with pytest.raises(tx.TranscriptionError, match="temporary outage"):
        tx.transcribe_media(media, tmp_path / "cache", api_key="test", language="en")
    assert len(completed) == 1
    failing = False
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test", language="en")
    assert len(completed) == 2 and result["words"]


def test_native_provider_requires_opt_in_for_whisper():
    adapter = adapter_functions()
    adapter["get_config"] = lambda: SimpleNamespace(transcription_provider="whisper", use_local_whisper=False)
    with pytest.raises(RuntimeError, match="USE_LOCAL_WHISPER=true"):
        adapter["get_video_transcript"](Path("source.mp4"))


def test_native_openai_uses_dedicated_key_and_standard_cache(monkeypatch, tmp_path):
    import sys

    adapter = adapter_functions()
    payload = {"version": 2, "text": "Hello.", "words": [word("Hello.", 1000, 2000)],
               "utterances": [{"text": "Hello.", "start": 1000, "end": 2000}], "timing_quality": "approximate"}
    passed = {}

    def transcribe(path, **kwargs):
        assert path == source
        passed.update(kwargs)
        return payload

    monkeypatch.setitem(sys.modules, "clipfarm_transcription", SimpleNamespace(transcribe_media=transcribe, TranscriptionError=tx.TranscriptionError))
    adapter["_prepare_audio_for_transcription"] = lambda path: pytest.fail('Shared helper must prepare audio only once')
    adapter["select_audio_stream_index"] = lambda path: 0
    config = SimpleNamespace(openai_transcription_model="gpt-transcribe", transcription_language="en",
                             openai_transcription_api_key="dedicated-key", openai_transcription_base_url=None)
    source = tmp_path / "source.mp4"
    result = adapter["_get_transcript_with_openai"](source, config)
    assert result == "[00:01 - 00:02] Hello."
    assert passed["api_key"] == "dedicated-key"
    assert passed['audio_stream_index'] == 0
    assert json.loads(source.with_suffix(".transcript_cache.json").read_text())["words"][0]["start"] == 1000


def test_configurable_shorter_uploads_preserve_full_recording(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=700000)
    monkeypatch.setenv('OPENAI_TRANSCRIPTION_CHUNK_SECONDS', '300')
    calls = []
    def request(client, path, model, language):
        start, duration = map(int, path.stem.removeprefix('chunk-').split('-'))
        calls.append((start, duration))
        return {'text': 'Spoken words. ' * 180, 'language': 'en'}
    monkeypatch.setattr(tx, '_request_transcript', request)
    result = tx.transcribe_media(media, tmp_path/'cache', api_key='test', language='en')
    assert sorted(calls) == [(0, 300000), (296000, 300000), (592000, 108000)]
    assert result['words'][-1]['end'] == 700000


def test_normalization_and_speech_detection_use_one_pass(monkeypatch, tmp_path):
    path = tmp_path/'audio.m4a'
    path.write_bytes(b'source')
    cache = tmp_path/'cache'; cache.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'ffmpeg':
            Path(command[-1]).write_bytes(b'normalized')
            return SimpleNamespace(stderr='silence_start: 0\nsilence_end: 0.2')
        return SimpleNamespace(stdout='{"format":{"duration":"2"}}')
    monkeypatch.setattr(tx, '_run', run)
    _, duration, intervals = tx._prepare_audio(path, cache, audio_stream_index=1)
    assert duration == 2000 and intervals == [(200, 2000)]
    assert len([c for c in calls if c[0] == 'ffmpeg']) == 1
    assert '0:a:1' in calls[0] and 'silencedetect=noise=-35dB:d=0.35' in calls[0]


def test_current_model_uses_plural_language_hint_and_detected_language(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=2000)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "Hello.", "languages": [{"code": "en"}]})

    real_client = httpx.Client
    monkeypatch.setattr(tx.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test", language="en")
    assert b'name="languages[]"\r\n\r\nen' in requests[0].content
    assert b'name="language"' not in requests[0].content
    with real_client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(respond)) as client:
        assert tx._request_transcript(client, media, "gpt-transcribe", None)["language"] == "en"
    assert result["language_code"] == "en"


def test_whisper_api_requests_and_preserves_provider_word_timestamps(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=5000)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "Hello friend.", "language": "en", "words": [
            {"word": "Hello", "start": 1, "end": 1.5}, {"word": "friend.", "start": 2, "end": 2.5}]})

    real_client = httpx.Client
    monkeypatch.setattr(tx.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test", model="whisper-1", language="en")
    assert b'name="response_format"\r\n\r\nverbose_json' in requests[0].content
    assert b'name="timestamp_granularities[]"\r\n\r\nword' in requests[0].content
    assert b'name="language"\r\n\r\nen' in requests[0].content
    assert [w["start"] for w in result["words"]] == [1000, 2000]
    assert result["words"][-1]["end"] == 2500
    assert result["timing_quality"] == result["utterances"][0]["timing_quality"] == "word"


def native_config_class():
    import math
    import os

    source = Path(__file__).parents[1] / "vendor/supoclip/backend/src/config.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    namespace = {"os": os, "math": math, "get_cached_setting": lambda _: None,
                 "setting_prefers_admin": lambda _: False, "LOCAL_OLLAMA_BASE_URL": "http://localhost:11434/v1",
                 "DOCKER_OLLAMA_BASE_URL": "http://host.docker.internal:11434/v1"}
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace["Config"]


def test_native_direct_openai_key_fallback_never_uses_gateway_token(monkeypatch):
    config_class = native_config_class()
    monkeypatch.delenv("OPENAI_TRANSCRIPTION_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert config_class().openai_transcription_api_key == "real-openai-key"
    monkeypatch.setenv("OPENAI_BASE_URL", "http://supervisor:8787/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "gateway-token")
    assert config_class().openai_transcription_api_key is None
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_API_KEY", "dedicated-real-key")
    assert config_class().openai_transcription_api_key == "dedicated-real-key"


def test_auto_language_is_detected_on_short_audio_then_hints_long_upload(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=600000)
    calls = []

    def request(client, path, model, language):
        calls.append((path.name, language))
        return {"text": "Recognizable speech. " * (40 if "language-probe" in path.name else 700), "language": "en"}

    monkeypatch.setattr(tx, "_request_transcript", request)
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test")
    assert len(calls) == 2 and calls[0][1] is None and calls[1][1] == "en"
    assert "language-probe" in calls[0][0]
    assert len(result["words"]) == 1400 and result["language_code"] == "en"
    tx.transcribe_media(media, tmp_path / "cache", api_key="test")
    assert len(calls) == 2


def test_whitespace_speech_chunk_subdivides_and_anchors_short_uploads(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=600000)
    calls = []

    def request(client, path, model, language):
        start, duration = map(int, path.stem.removeprefix("chunk-").split("-"))
        calls.append((start, duration))
        return {"text": " \n \t" if duration > 60000 else "Clear spoken words. " * 40, "language": "en"}

    monkeypatch.setattr(tx, "_request_transcript", request)
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test", language="en")
    assert len(result["words"]) >= 1000
    assert all(duration <= 60000 for _, duration in calls[1:])
    assert result["words"][-1]["end"] == 600000
    assert any(50000 < word["start"] < 70000 for word in result["words"])
    before = len(calls)
    tx.transcribe_media(media, tmp_path / "cache", api_key="test", language="en")
    assert len(calls) == before


def test_true_music_empty_short_probes_do_not_subdivide_entire_recording(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=600000)
    calls = []

    def request(client, path, model, language):
        calls.append(path.name)
        return {"text": "", "language": None}

    monkeypatch.setattr(tx, "_request_transcript", request)
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test", language="en")
    assert result["words"] == []
    assert len(calls) <= 4  # long upload plus at most three speech confirmation probes


def test_silent_audio_never_retries_or_subdivides_empty_result(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=600000)
    original_prepare = tx._prepare_audio

    def silent(path, cache):
        audio, duration, _ = original_prepare(path, cache)
        return audio, duration, []

    calls = []
    monkeypatch.setattr(tx, "_prepare_audio", silent)
    monkeypatch.setattr(tx, "_request_transcript", lambda *args: calls.append(args) or {"text": ""})
    result = tx.transcribe_media(media, tmp_path / "cache", api_key="test")
    assert not result["words"] and len(calls) <= 1


def test_failed_recovery_stays_incomplete_and_resumes_paid_subchunks(monkeypatch, tmp_path):
    media, _ = fake_audio(monkeypatch, tmp_path, duration=180000)
    calls = []
    failing = True

    def request(client, path, model, language):
        start, duration = map(int, path.stem.removeprefix("chunk-").split("-"))
        calls.append((start, duration))
        if start == 59000 and failing:
            raise tx.TranscriptionError("temporary speech recovery outage")
        return {"text": " " if duration > 60000 else "Clear spoken words. " * 40, "language": "en"}

    cache = tmp_path / "cache"
    monkeypatch.setattr(tx, "_request_transcript", request)
    with pytest.raises(tx.TranscriptionError, match="temporary speech recovery outage"):
        tx.transcribe_media(media, cache, api_key="test", language="en")
    root = next(json.loads(path.read_text()) for path in cache.glob("*/transcript-*.json")
                if json.loads(path.read_text())["duration_ms"] == 180000)
    assert root["complete"] is False
    failing = False
    result = tx.transcribe_media(media, cache, api_key="test", language="en")
    assert len(result["words"]) > 300
    assert calls.count((0, 180000)) == 1 and calls.count((0, 60000)) == 1


def test_native_local_fallback_does_not_hide_programming_errors(monkeypatch):
    import sys

    adapter = adapter_functions()
    monkeypatch.setitem(sys.modules, "clipfarm_transcription", SimpleNamespace(TranscriptionError=tx.TranscriptionError))
    adapter["get_config"] = lambda: SimpleNamespace(transcription_provider="openai", use_local_whisper=True)

    def broken(*args):
        raise ValueError("programming error")

    adapter["_get_transcript_with_openai"] = broken
    adapter["_get_transcript_with_whisper"] = lambda *args: "local fallback"
    with pytest.raises(ValueError, match="programming error"):
        adapter["get_video_transcript"](Path("source.mp4"))

    def unavailable(*args):
        raise tx.TranscriptionError("provider unavailable")

    adapter["_get_transcript_with_openai"] = unavailable
    assert adapter["get_video_transcript"](Path("source.mp4")) == "local fallback"
