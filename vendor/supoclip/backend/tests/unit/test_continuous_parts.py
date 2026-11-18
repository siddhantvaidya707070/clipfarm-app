import pytest
from src.services.video_service import continuous_part_segments

@pytest.mark.parametrize("duration", [1, 14, 60, 61, 121, 180, 1801])
def test_full_coverage_including_silence_and_tail(duration):
    parts = continuous_part_segments(duration)
    assert parts[0]['start_time'] == 0
    assert parts[-1]['end_time'] == duration
    assert sum(p['end_time']-p['start_time'] for p in parts) == duration
    assert all(a['end_time'] == b['start_time'] for a,b in zip(parts,parts[1:]))
    assert all(p['required'] and p['workflow_role'] == 'part' for p in parts)
    assert all(p['max_seconds'] == p['end_time']-p['start_time'] for p in parts)
    if duration > 60:
        assert parts[-1]['max_seconds'] >= 15
    if duration > 1800:
        assert len(parts) > 20


def test_words_only_appear_in_their_part():
    parts = continuous_part_segments(120, {'words': [{'start':61000,'text':'second'}]})
    assert [p['text'] for p in parts] == ['', 'second']
