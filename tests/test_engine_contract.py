import pytest


def test_engine_maps_absolute_source_ranges():
    from supervisor.engines import normalize_clip
    result = normalize_clip({'id': '1', 'start_time': '00:10', 'end_time': '00:40', 'filename': 'a.mp4', 'text': 'real text'}, offset=300)
    assert result['start'] == 310 and result['end'] == 340


def test_engine_rejects_escaping_media_path():
    from supervisor.engines import normalize_clip
    with pytest.raises(ValueError):
        normalize_clip({'id': '1', 'start_time': 0, 'end_time': 30, 'filename': '../secrets/key'})
