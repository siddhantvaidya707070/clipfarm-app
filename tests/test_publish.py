import pytest


def test_upload_requires_verified_destination():
    from supervisor.youtube import validate_destination
    with pytest.raises(ValueError):
        validate_destination('expected', 'wrong')
    validate_destination('same', 'same')


def test_next_slot_enforces_daily_count_and_spacing():
    from datetime import datetime, timezone
    from supervisor.youtube import next_slot
    t = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)
    slot = next_slot(t, [])
    assert slot > t
    assert next_slot(t, [slot.isoformat()]) > slot


def test_submission_deadline_from_publication():
    from supervisor.campaigns import submission_state
    assert submission_state(0, 60, False) == 'pending'
    assert submission_state(0, 1600, False) == 'urgent'
    assert submission_state(0, 1801, False) == 'expired'
    assert submission_state(0, 1801, True) == 'submitted'


def test_live_tail_file_is_not_a_completed_segment(tmp_path):
    from supervisor.live import closed_segments
    for name in ('00000.ts', '00001.ts', 'tail.ts'):
        (tmp_path / name).touch()
    assert [p.name for p in closed_segments(tmp_path, running=True)] == ['00000.ts']


def test_queue_rejects_traversal_in_local_import(client=None):
    from supervisor.ingest import public_source
    assert not public_source('https://youtube.com.attacker.test/video')
    assert not public_source('https://user:pass@youtube.com/video')


def test_youtube_oauth_requests_publication_scope():
    from supervisor.youtube import SCOPES
    assert 'https://www.googleapis.com/auth/youtube.force-ssl' in SCOPES.split()
