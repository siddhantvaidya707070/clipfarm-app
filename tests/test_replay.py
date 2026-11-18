import time
from unittest.mock import Mock
from supervisor.store import Store
from supervisor import worker


def test_ongoing_stream_waits_for_replay_without_blocking_other_jobs(tmp_path, monkeypatch):
    from supervisor.ingest import ReplayPending
    store = Store('sqlite:///' + str(tmp_path / 'jobs.db'))
    job = store.queue('gaming', 'https://youtube.com/watch?v=live')
    monkeypatch.setattr(worker, 'enough_space', lambda: True)
    monkeypatch.setattr(worker, 'read_secret', lambda _: 'configured')
    monkeypatch.setattr(worker, 'acquire', Mock(side_effect=ReplayPending('Stream ongoing')))
    engine = Mock()
    worker._process_one(store, engine)
    held = store.get_job(job['id'])
    assert held['state'] == 'retry'
    assert held['replay_deadline'] > time.time() + 86000
    assert held['next_attempt'] > time.time()
    assert held['attempts'] == 0
    engine.submit.assert_not_called()


def test_replay_wait_expires_after_24_hours(tmp_path, monkeypatch):
    from supervisor.ingest import ReplayPending
    store = Store('sqlite:///' + str(tmp_path / 'jobs.db'))
    job = store.queue('gaming', 'https://youtube.com/watch?v=live')
    store.update_job(job['id'], replay_deadline=time.time() - 1)
    monkeypatch.setattr(worker, 'enough_space', lambda: True)
    monkeypatch.setattr(worker, 'read_secret', lambda _: 'configured')
    monkeypatch.setattr(worker, 'acquire', Mock(side_effect=ReplayPending('Stream ongoing')))
    worker._process_one(store, Mock())
    assert store.get_job(job['id'])['state'] == 'held'
