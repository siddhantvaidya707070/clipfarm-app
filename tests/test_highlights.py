import json

import pytest

from supervisor.store import Store
from supervisor import highlights as h
from supervisor.highlight_media import parse_chat, clip_captions, srt_text, youtube_id


@pytest.fixture
def service(tmp_path):
    value = h.HighlightService(Store('sqlite:///' + str(tmp_path / 'db.sqlite')), tmp_path)
    yield value
    value.close()


def prepared(service):
    row = service.create('owner', {'source': 'https://youtube.com/watch?v=abcdefghijk'}, schedule=False)
    transcript = {'words': [{'text': 'No', 'start': 20000, 'end': 20200},
                            {'text': 'way!', 'start': 20300, 'end': 20800}]}
    path = service.folder(row['id']) / 'transcript.json'
    path.write_text(json.dumps(transcript))
    service.update(row['id'], state='completed', duration=180, transcript_path=str(path))
    return row


def test_ownership_and_input_validation(service):
    for source in ('http://youtube.com/watch?v=abcdefghijk', 'https://youtube.com.evil.test/abc',
                   'https://user@youtube.com/watch?v=abcdefghijk', 'https://youtu.be/nope'):
        with pytest.raises(ValueError):
            service.create('owner', {'source': source}, schedule=False)
    row = prepared(service)
    assert service.list('other') == []
    with pytest.raises(ValueError):
        service.get('other', row['id'])
    assert youtube_id('https://youtu.be/abcdefghijk?t=10') == 'abcdefghijk'


def test_edit_merge_delete_and_reexport_keep_transcript(service):
    row = prepared(service)
    first = service.add_clip('owner', row['id'], {'start': 10, 'end': 40})
    one = first['clips'][0]['id']
    service.edit_clip('owner', row['id'], one, {'type': 'SHOCK', 'title': 'No way', 'tags': ['GTA', 'reaction', 'shock']})
    second = service.add_clip('owner', row['id'], {'start': 40, 'end': 70})['clips'][1]['id']
    merged = service.merge('owner', row['id'], [one, second])
    assert len(merged['clips']) == 1
    clip = merged['clips'][0]
    assert (clip['start'], clip['end']) == (10, 70)
    assert clip['type'] == 'SHOCK'
    assert clip['export_state'] == 'stale'
    assert len(clip['captions']) == 1
    assert service.transcript(service.get('owner', row['id']))['words'][0]['text'] == 'No'
    manifest = json.loads((service.folder(row['id']) / 'manifest.json').read_text())
    assert manifest['clips'][0]['id'] == clip['id']
    assert 'user_id' not in manifest
    assert 'transcript_path' not in manifest
    assert 'OPENAI' not in json.dumps(manifest)
    service.delete_clip('owner', row['id'], clip['id'])
    assert service.get('owner', row['id'])['clips'] == []


def test_invalid_edits_do_not_corrupt_saved_clip(service):
    row = prepared(service)
    clip = service.add_clip('owner', row['id'], {'start': 10, 'end': 40})['clips'][0]
    for fields in ({'start': float('nan')}, {'end': 500}, {'type': 'invented'},
                   {'start': 39}, {'end': 150}, {'crop_center': 2},
                   {'export_height': 999},
                   {'captions': [{'start': 0, 'end': 50, 'text': 'outside clip'}]}):
        with pytest.raises(ValueError):
            service.edit_clip('owner', row['id'], clip['id'], fields)
    assert service.get('owner', row['id'])['clips'][0]['start'] == 10


def test_metadata_only_edits_do_not_require_reencoding(service):
    row = prepared(service)
    row = service.add_clip('owner', row['id'], {'start': 10, 'end': 40})
    row['clips'][0]['export_state'] = 'ready'
    service.update(row['id'], clips=row['clips'])
    result = service.edit_clip('owner', row['id'], row['clips'][0]['id'], {
        'start': 10, 'end': 40, 'title': 'New title', 'type': 'FUNNY',
        'captions': row['clips'][0]['captions'], 'output_format': 'original',
        'burn_captions': True, 'crop_center': .5, 'export_height': 720,
    })
    assert result['clips'][0]['export_state'] == 'ready'


def test_chat_replay_timestamps_emotes_and_dedup(tmp_path):
    path = tmp_path / 'chat.json'
    action = {'replayChatItemAction': {'videoOffsetTimeMsec': '12000', 'actions': [
        {'addChatItemAction': {'item': {'liveChatTextMessageRenderer': {
            'id': 'same', 'message': {'runs': [{'text': 'LMAOOO '}, {'emoji': {'emojiId': 'Kappa', 'shortcuts': [':Kappa:']}}]}
        }}}}]}}
    path.write_text(json.dumps(action) + '\n' + json.dumps(action) + '\nmalformed\n')
    messages = parse_chat(path)
    assert len(messages) == 1
    assert messages[0]['time'] == 12
    assert 'LMAOOO' in messages[0]['text']
    assert messages[0]['emotes'] == ['Kappa']


def test_bounded_live_chat_recovers_uncommitted_html_fragment(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from supervisor import highlight_media as media
    renderer = {'liveChatTextMessageRenderer': {'id': 'live-message', 'timestampUsec': '1010000000',
                 'message': {'runs': [{'text': 'LMAOOO'}]}}}
    payload = {'continuationContents': {'liveChatContinuation': {'actions': [renderer]}}}
    (tmp_path / 'chat.live_chat.json.part').write_text('')
    html = '<html><script>var ytInitialData = ' + json.dumps(payload) + ';</script></html>'
    (tmp_path / 'chat.live_chat.json.part-Frag1').write_text(html)
    (tmp_path / 'chat.live_chat.json.part-Frag2').write_text(json.dumps(payload))
    monkeypatch.setattr(media.subprocess, 'Popen', lambda *args, **kwargs: SimpleNamespace(
        poll=lambda: None, terminate=lambda: None, wait=lambda **kwargs: 0))
    done = threading.Event()
    done.set()
    messages = media.download_chat('https://youtu.be/abcdefghijk', tmp_path, done, live=True, recording_started=1000)
    assert messages == [{'time': 10, 'text': 'LMAOOO', 'emotes': []}]


def test_captions_use_clip_relative_times_and_srt():
    transcript = {'words': [{'text': 'Hello', 'start': 9000, 'end': 10300},
                            {'text': 'world!', 'start': 10400, 'end': 12000}]}
    captions = clip_captions(transcript, 10, 11)
    assert captions[0] == {'start': 0, 'end': 1, 'text': 'Hello world!'}
    assert '00:00:00,000 --> 00:00:01,000' in srt_text(captions)


def test_stream_copy_only_for_exact_keyframe_boundaries(monkeypatch, tmp_path):
    from supervisor import highlight_media as media
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        if '-show_frames' in command:
            return b'{"frames": [{"best_effort_timestamp_time":"10.0"}, {"best_effort_timestamp_time":"30.0"}]}'
        if command[0] == 'ffprobe':
            return b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"20"}}'
        from pathlib import Path
        Path(command[-1]).write_bytes(b'encoded')
        return b''
    monkeypatch.setattr(media, 'run', execute)
    clip = {'id': 'a' * 32, 'start': 10.1, 'end': 30.1, 'captions': []}
    media.export_clip(tmp_path / 'input.mp4', tmp_path, clip, {'burn_captions': False})
    assert any(command[0] == 'ffmpeg' and 'libx264' in command for command in commands)
    commands.clear()
    media.export_clip(tmp_path / 'input.mp4', tmp_path, {**clip, 'start': 10, 'end': 30}, {'burn_captions': False})
    assert any(command[0] == 'ffmpeg' and 'copy' in command for command in commands)


def test_highlights_reuses_native_video_without_redownloading(tmp_path):
    from supervisor import highlight_media as media
    folder = tmp_path / 'highlights' / 'cache' / 'abcdefghijk'
    folder.mkdir(parents=True)
    native = tmp_path / 'engine-temp' / 'youtube-cache' / 'abcdefghijk' / 'full-1080.mp4'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'already downloaded video')
    assert media.download_audio('https://youtu.be/abcdefghijk', folder) == native
    assert media.download_video('https://youtu.be/abcdefghijk', folder) == native


def test_signed_api_keeps_streams_owner_scoped(monkeypatch, tmp_path):
    import hashlib
    import hmac
    import time
    from fastapi.testclient import TestClient
    from supervisor import api
    monkeypatch.setenv('SUPERVISOR_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'api.db'))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('BACKEND_AUTH_SECRET', 'test-signature-secret')
    monkeypatch.setattr(h.HighlightService, 'schedule', lambda *args: None)
    def signed(owner):
        stamp = str(int(time.time()))
        signature = hmac.new(b'test-signature-secret', f'{owner}:{stamp}'.encode(), hashlib.sha256).hexdigest()
        return {'x-supoclip-user-id': owner, 'x-supoclip-ts': stamp,
                'x-supoclip-signature': signature}
    with TestClient(api.app, base_url='http://localhost') as client:
        assert client.get('/native/highlights').status_code == 401
        response = client.post('/native/highlights', headers=signed('owner'),
                               json={'source': 'https://youtu.be/abcdefghijk'})
        assert response.status_code == 200
        row = response.json()
        assert 'user_id' not in row
        assert len(client.get('/native/highlights', headers=signed('owner')).json()['streams']) == 1
        assert client.get('/native/highlights', headers=signed('other')).json()['streams'] == []
        assert client.get('/native/highlights/' + row['id'], headers=signed('other')).status_code == 422
        assert client.get('/native/highlights/' + row['id'] + '/manifest', headers=signed('owner')).status_code == 200


def test_pipeline_exports_manifest_and_retry_preserves_edits(service, monkeypatch, tmp_path):
    from supervisor import transcription, peaks, openai_gateway
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'video')
    monkeypatch.setattr(h, 'metadata', lambda *args: {'id': 'abcdefghijk', 'title': 'Test broadcast', 'duration': 3600})
    monkeypatch.setattr(h, 'download_audio', lambda *args: source)
    monkeypatch.setattr(h, 'acquire_source_windows', lambda *args, **kwargs: (str(source), []))
    monkeypatch.setattr(h, 'download_chat', lambda *args, **kwargs: [])
    monkeypatch.setattr(h, 'run', lambda *args, **kwargs: b'{"format":{"duration":3600}}')
    transcript = {'version': 2, 'text': 'No way!', 'words': [{'text': 'No way!', 'start': 20000, 'end': 21000}],
                  'timing_quality': 'approximate', 'timings': {'transcription_seconds': 1}}
    calls = []
    monkeypatch.setattr(transcription, 'transcribe_media', lambda *args, **kwargs: calls.append(1) or transcript)
    monkeypatch.setattr(peaks, 'analyze_peaks', lambda *args, **kwargs: [dict(
        id='b' * 32, type='SHOCK', start=10, end=45, score=.9, signals={}, text='No way!',
        low_confidence=False, threshold_lowered=False)])
    async def model(*args):
        return 200, {'choices': [{'message': {'content': json.dumps({'clips': [{
            'id': 'b' * 32, 'title': 'A surprise reveal',
            'description': 'The streamer reacts to a surprise. They exclaim in disbelief.',
            'tags': ['reaction', 'shock', 'stream']}]})}}]}
    monkeypatch.setattr(openai_gateway, 'forward', model)
    def render(video, folder, clip, defaults):
        (folder / (clip['id'] + '.mp4')).write_bytes(b'export')
        (folder / (clip['id'] + '.srt')).write_text('captions')
        return {'export_state': 'ready'}
    monkeypatch.setattr(h, 'export_clip', render)
    row = service.create('owner', {'source': 'https://youtu.be/abcdefghijk'}, schedule=False)
    import threading
    service.stops[row['id']] = threading.Event()
    service._process(row['id'])
    result = service.public(service.get('owner', row['id']))
    assert result['state'] == 'completed'
    assert result['chat_status'] == 'no chat data'
    assert result['clips'][0]['metadata_generated'] is True
    assert result['clips'][0]['mp4_url'].endswith('.mp4')
    assert result['timings']['processing_seconds'] >= 0
    service.edit_clip('owner', row['id'], 'b' * 32, {'title': 'My own title'})
    service._process(row['id'])
    assert service.get('owner', row['id'])['clips'][0]['title'] == 'My own title'
    assert len(calls) == 2  # Shared transcriber itself reuses completed chunks.


def test_metadata_does_not_turn_food_discovery_into_game_mechanic(service, monkeypatch):
    from supervisor import openai_gateway
    row = prepared(service)
    clip = dict(id='c' * 32, type='SHOCK', start=10, end=45, score=.8, signals={},
                text='What is this? This is hollandaise sauce.', title='', description='', tags=[])
    service.update(row['id'], clips=[clip])
    async def model(*args):
        return 200, {'choices': [{'message': {'content': json.dumps({'clips': [{
            'id': clip['id'], 'title': 'Trying hollandaise sauce', 'description': 'They try the sauce. They ask what it is.',
            'tags': ['food', 'reaction', 'sauce'], 'type': 'NEW_MECHANIC', 'type_reason': 'A new dish discovery.'}]})}}]}
    monkeypatch.setattr(openai_gateway, 'forward', model)
    service._metadata(row['id'])
    saved = service.get('owner', row['id'])['clips'][0]
    assert saved['type'] == 'OTHER'
    assert saved['low_confidence'] is True


def test_retry_of_ended_live_stream_preserves_capture_timeline(service, monkeypatch):
    import threading
    from supervisor import transcription
    row = prepared(service)
    recording = service.folder(row['id']) / 'recording'
    recording.mkdir()
    source = recording / 'source.mp4'
    source.write_bytes(b'already captured video')
    (recording / 'recording-clock.json').write_text('{"started":1000}')
    service.update(row['id'], is_live=True)
    service.add_clip('owner', row['id'], {'start': 10, 'end': 40})
    def never_download(*args, **kwargs):
        raise AssertionError('A retry must not replace the capture with the full replay')
    monkeypatch.setattr(h, 'metadata', never_download)
    monkeypatch.setattr(h, 'download_audio', never_download)
    monkeypatch.setattr(h, 'download_video', never_download)
    def chat(*args, **kwargs):
        assert kwargs['recording_started'] == 1000
        return [{'time': 20, 'text': 'KEKW', 'emotes': ['KEKW']}]
    monkeypatch.setattr(h, 'download_chat', chat)
    monkeypatch.setattr(h, 'run', lambda *args, **kwargs: b'{"format":{"duration":180}}')
    calls = []
    def transcribe(path, **kwargs):
        calls.append((path, kwargs['source_id']))
        return {'words': [], 'text': '', 'timing_quality': 'approximate'}
    monkeypatch.setattr(transcription, 'transcribe_media', transcribe)
    monkeypatch.setattr(h, 'export_clip', lambda *args: {'export_state': 'ready'})
    service.stops[row['id']] = threading.Event()
    service._process(row['id'])
    result = service.get('owner', row['id'])
    assert result['state'] == 'completed'
    assert result['duration'] == 180
    assert result['is_live'] is True
    assert calls == [(source, row['id'])]


def test_export_of_extended_edit_acquires_missing_source_and_keeps_original_bounds(service, monkeypatch, tmp_path):
    row = prepared(service)
    clip = service.add_clip('owner', row['id'], {'start': 20, 'end': 50})['clips'][0]
    old, extra = tmp_path / 'old.mp4', tmp_path / 'extra.mp4'
    old.write_bytes(b'clean')
    extra.write_bytes(b'clean')
    cached = [{'path': str(old), 'start': 10, 'end': 60}]
    service.update(row['id'], source_windows=cached)
    service.edit_clip('owner', row['id'], clip['id'], {'end': 80})
    def acquire(source, folder, clips, duration, saved, **kwargs):
        from supervisor.highlight_media import plan_source_windows
        assert plan_source_windows(clips, duration, saved) == [(60, 90)]
        return '', [*saved, {'path': str(extra), 'start': 60, 'end': 90}]
    monkeypatch.setattr(h, 'acquire_source_windows', acquire)
    def render(video, folder, chosen, defaults):
        assert (chosen['start'], chosen['end']) == (20, 80)
        assert chosen['source_parts'] == [{'path': str(old), 'start': 10, 'end': 50},
                                           {'path': str(extra), 'start': 0, 'end': 20}]
        assert chosen['captions'][0]['start'] == 0
        return {'export_state': 'ready'}
    monkeypatch.setattr(h, 'export_clip', render)
    service._export(row['id'], [clip['id']])
    saved = service.get('owner', row['id'])
    assert saved['state'] == 'completed'
    assert saved['clips'][0]['start'] == 20 and saved['clips'][0]['end'] == 80
    assert saved['clips'][0]['export_state'] == 'ready'
    assert 'path' not in json.dumps(service.public(saved)['source_windows'])
