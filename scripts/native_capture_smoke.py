"""Local HLS fixture smoke, run in the capture image. Never publishes videos."""
import functools
import http.server
import threading
from supervisor import native_capture as native, live
from supervisor.store import Store
from supervisor.ingest import probe

server = http.server.ThreadingHTTPServer(('127.0.0.1', 9321),
    functools.partial(http.server.SimpleHTTPRequestHandler, directory='/data/live-smoke-fixture'))
threading.Thread(target=server.serve_forever, daemon=True).start()
store = Store('sqlite:////data/native-capture-smoke.db')
native.recover(store)
for old in store.list(native.KIND):
    if old['state'] in ('queued', 'ready'):
        native.update(store, old['id'], state='stopped')
row = native.create_session(store, 'clipfarm-owner', {'source': 'https://twitch.tv/fixture', 'start': 1, 'end': 21})
native.update(store, row['id'], state='recording')
original_start = live.Recorder.start
original_validation = live.public_source
def fixture_start(self, creator):
    return original_start(self, {**creator, 'live_url': 'hls://http://127.0.0.1:9321/live.m3u8'})
# Only this isolated fixture process accepts the loopback HLS source.
live.public_source = lambda _: True
live.Recorder.start = fixture_start
try:
    native.capture(store, row, lambda: False)
    result = store.get(native.KIND, row['id'])
    assert result['state'] == 'ready', result['state']
    duration = probe(result['selected_path'])['duration']
    assert abs(duration - 20) < .5
    print('Streamlink -> FFmpeg -> selected MP4:', duration, 'seconds; ready for native handoff')
finally:
    live.Recorder.start = original_start
    live.public_source = original_validation
    server.shutdown()
