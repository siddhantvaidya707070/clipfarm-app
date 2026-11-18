from datetime import datetime, timezone


def test_retention_preserves_unpublished_render(tmp_path):
    from supervisor.maintenance import removable
    file = tmp_path / 'clip.mp4'
    file.write_bytes(b'data')
    clip = {'path': str(file), 'state': 'review'}
    assert not removable(file, [clip], age_days=40)
    clip['state'] = 'published'
    assert removable(file, [clip], age_days=40)


def test_snapshot_due_windows_are_not_faked():
    from supervisor.analytics import due_windows
    assert due_windows(20 * 3600, []) == []
    assert due_windows(25 * 3600, []) == [24]
    assert due_windows(170 * 3600, [24, 72]) == [168]
