from datetime import datetime, timedelta, timezone
import pytest
from supervisor.store import Store


def test_youtube_aliases_create_one_job(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'db'))
    a = s.queue('gaming', 'https://www.youtube.com/watch?v=abc123&feature=share')
    b = s.queue('gaming', 'https://youtu.be/abc123?t=10')
    assert a['id'] == b['id']


def test_overdue_slots_obey_actual_spacing():
    from supervisor.youtube import release_capacity
    current = datetime(2026, 11, 12, 23, tzinfo=timezone.utc)
    assert not release_capacity(current, [(current - timedelta(hours=1)).isoformat()])
    assert not release_capacity(current, [(current - timedelta(hours=5)).isoformat(), (current - timedelta(hours=10)).isoformat()])


def test_malformed_engine_is_held_after_bounded_failures(tmp_path, monkeypatch):
    from supervisor.worker import process_one
    s = Store('sqlite:///' + str(tmp_path / 'db'))
    a = s.queue('gaming', 'https://youtube.com/watch?v=abc')
    s.update_job(a['id'], state='processing', engine_id='accepted')
    class Broken:
        def status(self, ident):
            raise ValueError('corrupt output')
    for _ in range(3):
        process_one(s, Broken())
        s.update_job(a['id'], next_attempt=0)
    assert s.get_job(a['id'])['state'] == 'held'


def test_campaign_expiry_does_not_pause_channel(tmp_path):
    from supervisor.campaigns import monitor_deadlines
    s = Store('sqlite:///' + str(tmp_path / 'db'))
    j = s.queue('tjr', 'https://youtube.com/watch?v=abc')
    c = s.add_clip(j['id'], {'start': 0, 'end': 30, 'path': '/media/c.mp4'})
    row = s.get('channel', 'tjr')
    s.put('channel', 'tjr', {**row, 'paused': False})
    s.update_clip(c['id'], state='published', youtube_id='test', published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    monitor_deadlines(s)
    assert not s.get('channel', 'tjr')['paused']
    assert s.get('campaign', 'tjr')['paused']
