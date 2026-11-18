import pytest
from supervisor.store import Store
from supervisor import native_capture as capture


def test_bounded_owner_scoped_requests(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    for end in (None, 0, 10801, float('nan')):
        with pytest.raises(ValueError):
            capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': end})
    with pytest.raises(ValueError):
        capture.create_session(store, 'owner', {'source': 'http://127.0.0.1', 'end': 20})
    row = capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'start': 5, 'end': 25})
    assert capture.sessions(store, 'other') == []
    with pytest.raises(ValueError):
        capture.stop_session(store, 'other', row['id'])
    with pytest.raises(ValueError):
        capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': 20})
    assert capture.stop_session(store, 'owner', row['id'])['state'] == 'stopped'


def test_recovery_preserves_finalized_handoff(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    store.put('native_capture', 'recording', {'id': 'recording', 'state': 'recording'})
    store.put('native_capture', 'ready', {'id': 'ready', 'state': 'ready', 'upload_url': 'upload://saved'})
    capture.recover(store)
    assert store.get('native_capture', 'recording')['state'] == 'interrupted'
    assert store.get('native_capture', 'ready')['upload_url'] == 'upload://saved'


def test_recovery_leaves_external_recorder_under_helper_ownership(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    store.put(capture.KIND, 'external', {'id': 'external', 'state': 'recording',
                                       'external_recorder': True, 'recorded_seconds': 60})
    capture.recover(store)
    assert store.get(capture.KIND, 'external')['state'] == 'recording'
    assert store.get(capture.KIND, 'external')['recorded_seconds'] == 60


def test_pending_handoff_can_be_cancelled_and_rejected_upload_releases_slot(tmp_path):
    import httpx
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    row = {'id': 'pending', 'user_id': 'owner', 'state': 'ready', 'upload_url': 'upload://saved'}
    store.put(capture.KIND, row['id'], row)
    assert capture.stop_session(store, 'owner', row['id'])['state'] == 'stopped'
    capture.handoff(store, row)  # A cancelled pending handoff never submits.
    request = httpx.Request('POST', 'http://backend/upload')
    error = httpx.HTTPStatusError('too large', request=request, response=httpx.Response(413, request=request))
    assert capture.failure_state({'state': 'handing_off'}, error) == 'failed'
    transient = httpx.HTTPStatusError('unavailable', request=request, response=httpx.Response(503, request=request))
    assert capture.failure_state({'state': 'handing_off'}, transient) == 'ready'
    store.put(capture.KIND, 'crashed', {'id': 'crashed', 'state': 'handing_off'})
    capture.recover(store)
    assert store.get(capture.KIND, 'crashed')['state'] == 'ready'


def test_handoff_reuses_upload_and_native_idempotency(tmp_path, monkeypatch):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    row = {'id': 'capture123', 'user_id': 'owner', 'state': 'ready', 'upload_url': 'upload://saved'}
    store.put('native_capture', row['id'], row)
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'task_id': 'native-task'}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(capture.httpx, 'Client', Client)
    monkeypatch.setenv('BACKEND_AUTH_SECRET', 'test')
    capture.handoff(store, row)
    assert len(calls) == 1
    assert calls[0][1]['headers']['Idempotency-Key'] == 'capture-capture123'
    assert calls[0][1]['json']['source']['url'] == 'upload://saved'
    assert store.get('native_capture', row['id'])['task_id'] == 'native-task'


def test_replay_skips_upload_preserves_normal_features(tmp_path, monkeypatch):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    options = {'output_format': 'vertical_streamer', 'streamer_webcam': [0, 0, .5, .5],
               'workflow': 'topic', 'workflow_options': {'topic': 'game'}, 'clip_length': '30',
               'font_options': {'font_size': 30}, 'add_subtitles': False,
               'cut_long_pauses': False, 'include_broll': False, 'continuous_parts': True, 'source': {'url': 'bad'},
               'user_id': 'attacker'}
    row = capture.create_session(store, 'owner', {'source': 'https://youtube.com/watch?v=replay',
                                                  'mode': 'replay', 'task_options': options})
    assert row['start'] == 0 and row['end'] is None
    capture.capture(store, row, lambda: False)
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return type('Response', (), {'raise_for_status': lambda _: None,
                                         'json': lambda _: {'task_id': 'replay-task'}})()
    monkeypatch.setattr(capture.httpx, 'Client', Client)
    monkeypatch.setenv('BACKEND_AUTH_SECRET', 'test')
    capture.handoff(store, store.get(capture.KIND, row['id']))
    assert len(calls) == 1 and calls[0][0].endswith('/tasks/')
    body = calls[0][1]['json']
    assert body['processing_mode'] == 'fast'
    assert body['continuous_parts'] is True
    assert body['source']['url'] == row['source']
    assert body['workflow_options'] == {'topic': 'game'}
    assert body['streamer_webcam'] == [0, 0, .5, .5]
    assert body['cut_long_pauses'] is False and body['add_subtitles'] is False
    assert 'user_id' not in body


def test_replay_rejects_ranges_instead_of_silently_ignoring_them(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    with pytest.raises(ValueError, match='entire'):
        capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example',
                                                'mode': 'replay', 'start': 5, 'end': 20})


def test_replay_submits_immediately_while_external_recording_remains_active(tmp_path, monkeypatch):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    active = capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': 3600})
    capture.update(store, active['id'], state='recording', external_recorder=True)
    def submit(store, row):
        # Creating a new transaction here also exercises the network boundary:
        # handoff must run after create_session releases its transaction.
        assert row['state'] == 'ready' and row['url_handoff'] is True
        capture.update(store, row['id'], state='submitted', task_id='immediate-replay')
    monkeypatch.setattr(capture, 'handoff', submit)
    replay = capture.create_session(store, 'owner', {'source': 'https://youtube.com/watch?v=replay', 'mode': 'replay'})
    assert replay['state'] == 'submitted' and replay['task_id'] == 'immediate-replay'
    assert store.get(capture.KIND, active['id'])['state'] == 'recording'
    with pytest.raises(ValueError, match='active'):
        capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': 60})


def test_pending_replays_and_batch_rows_do_not_block_recording(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    store.put(capture.KIND, 'replay', {'id': 'replay', 'state': 'ready', 'mode': 'replay'})
    store.put(capture.KIND, 'batch', {'id': 'batch', 'state': 'handing_off', 'parent_id': 'old'})
    row = capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': 60})
    assert row['state'] == 'queued'


def test_rolling_batches_submit_first_minute_then_three_minutes(tmp_path, monkeypatch):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    row = capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': 3600,
                                                'task_options': {'output_format': 'vertical_streamer'}})
    monkeypatch.setattr(capture, 'probe', lambda _: {'duration': 60})
    def merge(args, **kwargs):
        from pathlib import Path
        Path(args[-1]).write_bytes(b'mp4')
    monkeypatch.setattr(capture.subprocess, 'run', merge)
    submitted = []
    def submit(store, batch):
        submitted.append(batch)
        capture.update(store, batch['id'], state='submitted', task_id='task-' + str(len(submitted)))
    monkeypatch.setattr(capture, 'handoff', submit)
    for index in range(4):
        (tmp_path / f'{index:05}.ts').write_bytes(b'ts')
    capture.submit_closed_batches(store, row, tmp_path, running=False)
    assert [(r['source_offset'], r['source_end']) for r in submitted] == [(0, 60), (60, 240)]
    assert all(r['task_options']['output_format'] == 'vertical_streamer' for r in submitted)
    assert store.get(capture.KIND, row['id'])['task_ids'] == ['task-1', 'task-2']
    capture.submit_closed_batches(store, store.get(capture.KIND, row['id']), tmp_path, running=False)
    assert len(submitted) == 2


def test_rolling_batches_do_not_read_open_segment(tmp_path, monkeypatch):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    row = capture.create_session(store, 'owner', {'source': 'https://twitch.tv/example', 'end': 3600})
    (tmp_path / '00000.ts').write_bytes(b'open')
    monkeypatch.setattr(capture, 'probe', lambda _: pytest.fail('Open segment must not be probed'))
    capture.process_available_batches(store, row, tmp_path, running=True)
    assert store.get(capture.KIND, row['id']).get('batch_count', 0) == 0


def test_completed_youtube_routes_to_normal_task_without_recorder(tmp_path, monkeypatch):
    store = Store('sqlite:///' + str(tmp_path / 'test.db'))
    row = capture.create_session(store, 'owner', {'source': 'https://youtube.com/watch?v=ended', 'end': 3600})
    monkeypatch.setattr(capture.subprocess, 'run', lambda *a, **k:
                        type('Metadata', (), {'returncode': 0, 'stdout': '{"live_status":"was_live"}'})())
    monkeypatch.setattr(capture, 'Recorder', lambda: pytest.fail('Completed replay must not record'))
    capture.capture(store, row, lambda: False)
    latest = store.get(capture.KIND, row['id'])
    assert latest['state'] == 'ready' and latest['url_handoff'] is True


def test_native_api_requires_signature_and_isolates_owners(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from supervisor.api import app
    monkeypatch.setenv('SUPERVISOR_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'api.db'))
    monkeypatch.setenv('BACKEND_AUTH_SECRET', 'test-signed-secret')
    monkeypatch.setenv('CONTROL_TOKEN', 'legacy-token')
    with TestClient(app, base_url='http://localhost:8787') as client:
        assert client.get('/native/capture', headers={'Authorization': 'Bearer legacy-token'}).status_code == 401
        owner = capture.backend_headers('owner', 'test')
        result = client.post('/native/capture', headers=owner, json={'source': 'https://twitch.tv/example', 'end': 20})
        assert result.status_code == 200
        ident = result.json()['id']
        other = capture.backend_headers('other', 'test')
        assert client.get('/native/capture', headers=other).json() == []
        assert client.post(f'/native/capture/{ident}/stop', headers=other).status_code == 422
        assert client.post(f'/native/capture/{ident}/stop', headers=owner).status_code == 200
