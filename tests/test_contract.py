import tempfile
from pathlib import Path
import pytest


def store(tmp_path):
    from supervisor.store import Store
    return Store('sqlite:///' + str(tmp_path / 'test.db'))


def test_duplicate_jobs_are_idempotent(tmp_path):
    s = store(tmp_path)
    a = s.queue('tjr', 'https://youtube.com/watch?v=abc')
    assert s.queue('tjr', 'https://youtube.com/watch?v=abc')['id'] == a['id']


def test_spend_reservations_block_concurrent_overrun(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    s = store(tmp_path)
    def reserve(_):
        try:
            return s.reserve(600000, daily=1000000, monthly=10000000)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(x is not None for x in pool.map(reserve, range(2))) == 1


def test_month_limit_includes_previous_days(tmp_path):
    s = store(tmp_path)
    s.reserve(600000, daily=1000000, monthly=1000000)
    with pytest.raises(ValueError):
        s.reserve(500000, daily=2000000, monthly=1000000)


def test_settlement_never_frees_ambiguous_spend(tmp_path):
    s = store(tmp_path)
    rid = s.reserve(600000)
    s.settle(rid, None)
    with pytest.raises(ValueError):
        s.reserve(600000)


def test_clip_rules_reject_invalid_and_missing_hashtag():
    from supervisor.rules import validate
    assert 'timestamps' in validate({'start': 30, 'end': 20}, campaign='tjr')
    assert 'missing #TJR' in validate({'start': 0, 'end': 30, 'description': 'hello'}, campaign='tjr')
    assert 'duration' in validate({'start': 0, 'end': 100})


def test_duration_checks_the_rendered_media_not_nominal_source_span():
    from supervisor.rules import validate
    assert 'duration' not in validate({'start': 44, 'end': 62, 'probe': {'duration': 23.7}})


def test_approval_is_idempotent_and_does_not_enable_autopilot(tmp_path):
    s = store(tmp_path)
    job = s.queue('tjr', 'https://youtube.com/watch?v=abc')
    clip = s.add_clip(job['id'], {'start': 0, 'end': 30, 'path': '/media/a.mp4'})
    s.review(clip['id'], True)
    s.review(clip['id'], True)
    assert s.review_count() == 1
    assert not s.autopublish_ready()


def test_uncertain_upload_cannot_be_repeated(tmp_path):
    s = store(tmp_path)
    job = s.queue('gaming', 'https://youtube.com/watch?v=abc')
    clip = s.add_clip(job['id'], {'start': 0, 'end': 30, 'path': '/media/a.mp4'})
    s.update_clip(clip['id'], state='upload_unknown')
    assert not s.publishable(clip['id'])


def test_campaign_release_fails_closed(tmp_path):
    s = store(tmp_path)
    job = s.queue('tjr', 'https://youtube.com/watch?v=abc')
    clip = s.add_clip(job['id'], {'start': 0, 'end': 30, 'path': '/media/a.mp4'})
    s.review(clip['id'], True)
    assert not s.publishable(clip['id'])


def test_duplicate_source_intervals_do_not_create_two_clips(tmp_path):
    s = store(tmp_path)
    j = s.queue('gaming', 'https://youtube.com/watch?v=abc')
    a = {'start': 1, 'end': 31, 'path': '/media/a.mp4'}
    assert s.add_clip(j['id'], a)['id'] == s.add_clip(j['id'], a)['id']


def test_recovery_preserves_accepted_engine_id(tmp_path):
    s = store(tmp_path)
    j = s.queue('gaming', 'https://youtube.com/watch?v=abc')
    s.update_job(j['id'], state='processing', engine_id='accepted-task')
    from supervisor.store import Store
    reopened = Store('sqlite:///' + str(tmp_path / 'test.db'))
    assert reopened.get_job(j['id'])['engine_id'] == 'accepted-task'


def test_unpermitted_source_cannot_publish(tmp_path):
    s = store(tmp_path)
    j = s.queue('gaming', 'https://youtube.com/watch?v=abc')
    c = s.add_clip(j['id'], {'start': 0, 'end': 30, 'path': '/media/a.mp4'})
    s.review(c['id'], True)
    assert not s.publishable(c['id'])
