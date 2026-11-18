import copy
import math
import struct
import wave

import pytest

from supervisor.peaks import analyze_peaks, classify_clip, load_config, merge_windows


def transcript_at(events):
    words = []
    for second, text in events:
        for index, token in enumerate(text.split()):
            start = int((second + index * 0.22) * 1000)
            words.append({"text": token, "start": start, "end": start + 180})
    return {"words": words}


@pytest.mark.parametrize("text,expected", [
    ("Wait you can actually enter this hidden garage and unlock a new car", "NEW_MECHANIC"),
    ("This is my first time playing this I have never seen this before", "FIRST_REACTION"),
    ("Fuck this game I hate this stupid bullshit", "RAGE"),
    ("OH MY GOD WHAT THE HELL NO WAY!", "SHOCK"),
    ("hahaha that was hilarious I'm dying laughing", "FUNNY"),
    ("We are driving along the road to the next location", "OTHER"),
])
def test_classification_uses_actual_evidence(text, expected):
    assert classify_clip(text, {}) == expected


@pytest.mark.parametrize("text", ["[laughter]", "[laughs]"])
def test_transcribed_laughter_markers_qualify_as_funny(text):
    assert classify_clip(text, {}) == "FUNNY"
    result = analyze_peaks(transcript_at([(50, text)]), 120)
    assert len(result) == 1
    assert result[0]["type"] == "FUNNY"


@pytest.mark.parametrize("text", ["I found a secret", "this glitch unlocked", "wait it does that"])
def test_grounded_discovery_phrases_qualify(text):
    assert classify_clip(text, {}) == "NEW_MECHANIC"
    result = analyze_peaks(transcript_at([(50, text)]), 120)
    assert len(result) == 1
    assert result[0]["type"] == "NEW_MECHANIC"


def test_generic_game_nouns_still_do_not_qualify_as_discovery():
    text = "GTA car vehicle garage secret weapon mission police glitch"
    assert classify_clip(text, {}) == "OTHER"
    assert analyze_peaks(transcript_at([(50, text)]), 120) == []


def test_load_config_returns_independent_objects():
    config = load_config()
    config["weights"]["transcript_cues"] = 9
    assert load_config()["weights"]["transcript_cues"] != 9


def test_no_content_does_not_manufacture_highlights():
    assert analyze_peaks({}, 3600) == []
    assert analyze_peaks(transcript_at([(100, "The road continues north along the river")]), 3600) == []


def test_edges_word_boundaries_ids_and_determinism():
    source = transcript_at([(0, "OH MY GOD NO WAY!"), (115, "hahaha that was hilarious I'm dying laughing")])
    result = analyze_peaks(source, 120)
    assert len(result) == 2
    assert result == analyze_peaks(source, 120)
    for clip in result:
        assert 0 <= clip["start"] < clip["end"] <= 120
        assert 15 <= clip["end"] - clip["start"] <= 90
        assert len(clip["id"]) == 32
        int(clip["id"], 16)
        assert 0 <= clip["score"] <= 1
        assert clip["signals"]["audio_available"] is False


def test_short_source_clamps_without_exceeding_source():
    result = analyze_peaks(transcript_at([(2, "OH MY GOD NO WAY!")]), 8)
    assert len(result) == 1
    assert result[0]["start"] == 0
    assert result[0]["end"] == 8


def test_many_quality_peaks_are_not_capped_at_fifteen_per_hour():
    result = analyze_peaks(transcript_at([(100 + i * 150, "OH MY GOD WHAT THE HELL NO WAY!") for i in range(20)]), 3600)
    assert len(result) == 20
    ordered = sorted(result, key=lambda clip: clip["start"])
    assert all(a["end"] - b["start"] <= 5 for a, b in zip(ordered, ordered[1:]))


def test_lowering_threshold_is_explicit_and_does_not_fill_empty_slots():
    source = transcript_at([(100, "Oh my god no way"), (300, "hahaha hilarious")])
    config = load_config()
    config["threshold"] = 0.99
    result = analyze_peaks(source, 3600, config=config)
    assert 0 < len(result) <= 2
    assert all(clip["threshold_lowered"] and clip["low_confidence"] for clip in result)


def test_variety_keeps_three_genuine_types():
    source = transcript_at([(100, "OH MY GOD NO WAY!"), (300, "Fuck this stupid game I hate this bullshit"), (500, "hahaha that was hilarious I'm dying laughing")])
    result = analyze_peaks(source, 1800)
    assert {clip["type"] for clip in result} >= {"SHOCK", "RAGE", "FUNNY"}


def test_variety_can_promote_a_real_close_candidate_over_a_duplicate_type():
    source = transcript_at([
        (100, "OH MY GOD NO WAY!"),
        (111, "hahaha hilarious I'm dying laughing"),
        (500, "OH MY GOD NO WAY!"),
        (900, "Fuck this stupid game I hate this bullshit"),
    ])
    result = analyze_peaks(source, 1800)
    assert {clip["type"] for clip in result} >= {"SHOCK", "RAGE", "FUNNY"}
    ordered = sorted(result, key=lambda clip: clip["start"])
    assert all(a["end"] - b["start"] <= 5 for a, b in zip(ordered, ordered[1:]))


def test_isolated_moment_targets_roughly_thirty_five_seconds_and_real_peak():
    result = analyze_peaks(transcript_at([(100, "OH MY GOD NO WAY!")]), 300)
    assert len(result) == 1
    assert 30 <= result[0]["end"] - result[0]["start"] <= 40
    assert 100 <= result[0]["signals"]["peak_time"] <= 102


def test_sentence_boundaries_are_used_when_near_the_window_edge():
    source = transcript_at([(84, "The setup ends."), (100, "OH MY GOD NO WAY!"), (119, "And that settles it.")])
    result = analyze_peaks(source, 300)
    assert len(result) == 1
    word_starts = {word["start"] / 1000 for word in source["words"]}
    word_ends = {word["end"] / 1000 for word in source["words"]}
    assert result[0]["start"] in word_starts
    assert result[0]["end"] in word_ends


def test_sustained_reaction_keeps_the_resolution_after_the_spike():
    source = transcript_at([(second, "OH MY GOD NO WAY!") for second in range(100, 126, 5)])
    result = analyze_peaks(source, 300)
    assert result
    first = min(result, key=lambda clip: clip["start"])
    assert first["end"] >= 133
    assert first["end"] - first["start"] <= 60


def test_merge_retains_best_candidate_and_bounds_chain():
    windows = [
        {"id": "a", "start": 0, "end": 60, "score": 0.7},
        {"id": "b", "start": 40, "end": 100, "score": 0.9},
        {"id": "c", "start": 80, "end": 140, "score": 0.8},
        {"id": "d", "start": 140, "end": 250, "score": 0.6},
    ]
    result = merge_windows(windows, duration=200)
    assert "b" in {item["id"] for item in result}
    assert "a" not in {item["id"] for item in result}
    assert all(0 <= item["start"] < item["end"] <= 200 for item in result)
    assert all(item["end"] - item["start"] <= 90 for item in result)
    assert windows[-1]["end"] == 250


def test_chat_only_spike_uses_rolling_median_and_keeps_type_other():
    chat = [{"time": second, "text": "normal"} for second in range(80)]
    chat += [{"time": 81 + index / 100, "text": "WOOOOOW", "emotes": ["PogChamp"]} for index in range(90)]
    result = analyze_peaks({}, 120, chat=chat)
    assert result
    strongest = max(result, key=lambda item: item["score"])
    assert strongest["signals"]["chat_velocity"] > 0
    assert strongest["signals"]["emote_spam"] > 0
    assert strongest["type"] == "OTHER"


@pytest.mark.parametrize("emote", ["KEKW", "OMEGALUL", "LUL"])
def test_chat_laughter_emote_flooding_qualifies_as_funny_without_speech(emote):
    chat = [{"time": second, "text": "normal"} for second in range(80)]
    chat += [{"time": 81 + index / 100, "text": emote, "emotes": [emote]} for index in range(90)]
    result = analyze_peaks({}, 120, chat=chat)
    assert result
    strongest = max(result, key=lambda item: item["score"])
    assert strongest["type"] == "FUNNY"
    assert strongest["signals"]["chat_laughter"] > 0
    assert strongest["signals"]["transcript_available"] is False
    assert strongest["signals"]["transcript_cues"] == 0


def test_audio_silence_then_burst_reports_measured_signals(tmp_path):
    audio = tmp_path / "burst.wav"
    sample_rate = 8000
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for second in range(65):
            amplitude = 23000 if second in (30, 31, 32) else (0 if 26 <= second < 30 else 300)
            frames.extend(b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * index / sample_rate))) for index in range(sample_rate)))
        output.writeframes(frames)
    result = analyze_peaks({}, 65, audio_path=audio)
    assert result
    signal = max(result, key=lambda item: item["score"])["signals"]
    assert signal["audio_available"] is True
    assert signal["audio_rms_spike"] > 0
    assert signal["silence_then_burst"] > 0
    assert "zero_crossing_variation" in signal
    assert "pitch" not in signal


def test_missing_audio_is_graceful_and_does_not_invent_audio_signal(tmp_path):
    result = analyze_peaks(transcript_at([(30, "OH MY GOD NO WAY!")]), 90, audio_path=tmp_path / "missing.mp3")
    assert result
    assert result[0]["signals"]["audio_available"] is False
    assert result[0]["signals"]["audio_rms_spike"] == 0


def test_nested_words_and_millisecond_timestamps():
    source = transcript_at([(1800, "OH MY GOD NO WAY!")])
    result = analyze_peaks({"segments": [{"words": source["words"]}]}, 3600)
    assert len(result) == 1
    assert result[0]["start"] > 1700
    assert result[0]["end"] < 1900


def test_input_objects_are_unchanged():
    source = transcript_at([(20, "OH MY GOD NO WAY!")])
    original = copy.deepcopy(source)
    analyze_peaks(source, 100)
    assert source == original
