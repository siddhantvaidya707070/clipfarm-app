import math
from unittest.mock import Mock
import pytest
from supervisor import ingest
from supervisor.store import Store


def test_timestamp_validation():
    from supervisor.ranges import validate_range
    assert validate_range('01:02:03.5', '3724') == (3723.5, 3724.0)
    for start, end in [(-1, 3), (True, 3), ('NaN', 3), (0, 'inf'), ('1:99:00', 8000), (3, 3), (5, 4)]:
        with pytest.raises(ValueError):
            validate_range(start, end)


def test_exact_trim_for_local_range_keeps_original_offsets(tmp_path, monkeypatch):
    source = tmp_path / 'inbox' / 'capture.mp4'
    source.parent.mkdir()
    source.write_bytes(b'media')
    monkeypatch.setattr(ingest, 'DATA', tmp_path)
    monkeypatch.setattr(ingest, 'enough_space', lambda: True)
    monkeypatch.setattr(ingest, 'probe', lambda _: {'duration': 360, 'width': 1920, 'height': 1080})
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        from pathlib import Path
        Path(args[-1]).write_bytes(b'trimmed')
        return Mock(returncode=0)
    monkeypatch.setattr(ingest.subprocess, 'run', run)
    job = {'id': 'range', 'source': str(source), 'source_offset': 330, 'source_end': 350, 'media_source_offset': 300}
    result = ingest.acquire(job)
    assert result != str(source)
    command = commands[-1]
    assert command.index('-ss') > command.index('-i')
    assert command[command.index('-ss') + 1] == '30.0'
    assert command[command.index('-t') + 1] == '20.0'
    assert 'copy' not in command
    assert job['source_offset'] == 330


def test_live_requests_queue_stop_and_recover(tmp_path):
    from supervisor.live_service import start_session, stop_session, get_session, recover_sessions
    store = Store('sqlite:///' + str(tmp_path / 'db'))
    first = start_session(store, 'gaming', 'https://youtube.com/watch?v=live', start='00:01:00', end=120)
    assert first['state'] == 'queued'
    assert first['time_basis'] == 'recording'
    with pytest.raises(ValueError, match='already'):
        start_session(store, 'gaming', 'https://youtube.com/watch?v=another')
    assert stop_session(store, first['id'])['state'] == 'stopped'
    second = start_session(store, 'gaming', 'https://youtube.com/watch?v=live')
    store.put('live_session', second['id'], {**second, 'state': 'recording'})
    recover_sessions(store)
    assert get_session(store, second['id'])['state'] == 'interrupted'


def test_live_original_timestamp_requests_queue_replay(tmp_path):
    from supervisor.live_service import start_session
    store = Store('sqlite:///' + str(tmp_path / 'db'))
    session = start_session(store, 'gaming', 'https://youtube.com/watch?v=live', start=50, end=100, time_basis='original')
    assert session['state'] == 'replay_pending'
    job = store.get_job(session['job_id'])
    assert job['source_offset'] == 50 and job['source_end'] == 100


def test_controller_claims_once_and_drains_on_stop(tmp_path, monkeypatch):
    from supervisor import live
    from supervisor.live_service import start_session, stop_session
    store = Store('sqlite:///' + str(tmp_path / 'db'))
    session = start_session(store, 'gaming', 'https://youtube.com/watch?v=live')
    events = []
    class FakeRecorder:
        ffmpeg = None
        creator = None
        offset = 30
        folder = tmp_path
        def start(self, creator):
            events.append('start')
            self.creator = creator
            self.started = 100
            self.ffmpeg = Mock(poll=lambda: None)
        def captured_seconds(self):
            return self.offset
        def drain(self, store):
            events.append('drain')
        def stop(self):
            events.append('stop')
            self.ffmpeg = None
    monkeypatch.setattr(live, 'Recorder', FakeRecorder)
    monkeypatch.setattr(live, 'enough_space', lambda: True)
    controller = live.LiveController(store)
    controller.tick()
    controller.tick()
    assert events.count('start') == 1
    assert store.get('live_session', session['id'])['state'] == 'recording'
    stop_session(store, session['id'])
    controller.tick()
    assert events[-2:] == ['stop', 'drain']
    assert store.get('live_session', session['id'])['state'] == 'stopped'


def test_capture_window_intersects_segment_and_preserves_origin(tmp_path, monkeypatch):
    from supervisor import live
    store = Store('sqlite:///' + str(tmp_path / 'db'))
    file = tmp_path / '00000.ts'
    file.write_bytes(b'capture')
    recorder = live.Recorder()
    recorder.folder = tmp_path
    recorder.creator = {'id': 'gaming', 'live_url': 'https://youtube.com/watch?v=live'}
    recorder.started = 1234
    recorder.session = {'start': 60, 'end': 120}
    monkeypatch.setattr(live, 'probe', lambda _: {'duration': 300})
    monkeypatch.setattr(live.subprocess, 'run', lambda *args, **kwargs: None)
    recorder.drain(store)
    job = store.list('job')[0]
    assert job['source_offset'] == 60
    assert job['source_end'] == 120
    assert job['media_source_offset'] == 0
    assert job['captured_at'] == 1234


@pytest.mark.parametrize('available,ended,expected', [(30, False, 'recording'), (120, False, 'stopped'), (0, True, 'failed')])
def test_session_end_uses_media_duration_not_process_wall_time(tmp_path, monkeypatch, available, ended, expected):
    from supervisor import live
    from supervisor.live_service import start_session
    store = Store('sqlite:///' + str(tmp_path / 'db'))
    session = start_session(store, 'gaming', 'https://youtube.com/watch?v=live', end=120)
    store.put('live_session', session['id'], {**session, 'state': 'recording'})
    events = []
    class FakeRecorder:
        creator = {'id': 'gaming'}
        started = 1
        offset = 0
        ffmpeg = Mock(poll=lambda: 0 if ended else None)
        def captured_seconds(self):
            return available
        def stop(self):
            events.append('stop')
            self.ffmpeg = None
        def drain(self, store):
            events.append('drain')
            self.offset = available
    controller = live.LiveController(store)
    controller.recorder = FakeRecorder()
    controller.session_id = session['id']
    monkeypatch.setattr(live, 'enough_space', lambda: True)
    controller.tick()
    row = store.get('live_session', session['id'])
    assert row['state'] == expected
    if expected == 'recording':
        assert events == ['drain']
        assert row['recorded_seconds'] == available
    else:
        assert events == ['stop', 'drain']
    if expected == 'failed':
        assert 'no footage' in row['message'].lower()


def test_captured_seconds_counts_only_unprocessed_numeric_segments(tmp_path, monkeypatch):
    from supervisor import live
    recorder = live.Recorder()
    recorder.folder = tmp_path
    recorder.offset = 300
    recorder.done.add('00000.ts')
    for name in ('00000.ts', '00001.ts', '00002.ts', 'tail.ts'):
        (tmp_path / name).write_bytes(b'data')
    def probe(path):
        if path.name == '00002.ts':
            raise ValueError('incomplete TS')
        return {'duration': 40}
    monkeypatch.setattr(live, 'probe', probe)
    assert recorder.captured_seconds() == 340
