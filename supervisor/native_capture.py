"""Small capture-only extension. SupoClip owns tasks, editing and publication."""
import hashlib
import hmac
import json
import math
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from .store import Store, now
from .live import Recorder, closed_segments
from .ingest import DATA, public_source, enough_space, probe, trim_source
from .ranges import validate_range

KIND = 'native_capture'
TASK_FIELDS = frozenset(('font_options', 'caption_template', 'include_broll', 'processing_mode',
                        'output_format', 'streamer_webcam', 'add_subtitles', 'clip_length',
                        'workflow', 'workflow_options', 'instructions', 'continuous_parts', 'cut_long_pauses',
                        'pause_threshold_ms', 'remove_filler_words', 'filtered_words'))


def task_options(data):
    options = data.get('task_options', {})
    if not isinstance(options, dict):
        raise ValueError('task_options must be an object')
    return {'include_broll': False, 'add_subtitles': True, 'output_format': 'vertical',
            'clip_length': 'auto', 'processing_mode': 'fast',
            **{key: value for key, value in options.items() if key in TASK_FIELDS}}


def sessions(store, user_id):
    return sorted((r for r in store.list(KIND) if r.get('user_id') == user_id and not r.get('parent_id')),
                  key=lambda r: r.get('created', ''), reverse=True)


def create_session(store, user_id, data):
    mode = data.get('mode', 'record')
    if mode not in ('record', 'replay'):
        raise ValueError('Choose record or replay mode')
    start, end = validate_range(data.get('start', 0), data.get('end'))
    if mode == 'replay' and (start != 0 or end is not None):
        raise ValueError('Replay mode processes the entire video; remove start and end times')
    if mode == 'record' and (end is None or not math.isfinite(end) or end > 10800):
        raise ValueError('Choose a finite end time, at most 03:00:00 from recording start')
    if data.get('time_basis', 'recording') != 'recording':
        raise ValueError('Broadcast timestamps require a completed replay; live times start when media arrives')
    source = data.get('source', '')
    if not public_source(source):
        raise ValueError('Use a public HTTPS YouTube, Twitch or Kick stream URL')
    row = dict(id=uuid.uuid4().hex, user_id=user_id, source=source, start=start, end=end,
               mode=mode, task_options=task_options(data), time_basis='recording', task_ids=[],
               state='queued', created=now(), message='Waiting for capture worker', stop_requested=False)
    if mode == 'replay':
        row.update(state='ready', url_handoff=True, message='Submitting entire replay through normal video workflow')
    with store.transaction() as connection:
        from sqlalchemy import select
        rows = connection.execute(select(store.records.c.data).where(store.records.c.kind == KIND))
        if mode == 'record' and any(
                r[0]['state'] in ('queued', 'recording', 'ready', 'handing_off')
                and r[0].get('mode', 'record') != 'replay' and not r[0].get('parent_id') for r in rows):
            raise ValueError('Another capture is active; wait or stop it first')
        store._put(connection, KIND, row['id'], row)
    # Replay submission is independent of the long-running recorder loop. The
    # persisted ready row and stable key make network failures safe to retry.
    if mode == 'replay':
        try:
            handoff(store, row)
        except Exception as exc:
            current = store.get(KIND, row['id'])
            update(store, row['id'], state=failure_state(current, exc),
                   message='Replay handoff pending: ' + type(exc).__name__)
    return store.get(KIND, row['id'])


def stop_session(store, user_id, ident):
    with store.transaction() as connection:
        row = store._get(connection, KIND, ident)
        if not row or row['user_id'] != user_id:
            raise ValueError('Unknown capture')
        if row['state'] in ('queued', 'ready'):
            row['state'] = 'stopped'
        if row['state'] == 'recording':
            row['stop_requested'] = True
        return store._put(connection, KIND, ident, row)


def update(store, ident, **fields):
    with store.transaction() as connection:
        return store._put(connection, KIND, ident, {**store._get(connection, KIND, ident), **fields, 'updated': now()})


def recover(store):
    for row in store.list(KIND):
        if row.get('external_recorder') is True:
            # The adoption helper owns this recorder's lifecycle and recovery.
            continue
        if row['state'] == 'recording':
            update(store, row['id'], state='interrupted', message='Capture worker restarted; partial files retained. Start a new capture.')
        elif row['state'] == 'handing_off':
            update(store, row['id'], state='ready', message='Resuming finalized recording handoff')


def failure_state(row, exc):
    if row['state'] not in ('ready', 'handing_off'):
        return 'failed'
    if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 429):
        return 'failed'
    return 'ready'


def backend_headers(user_id, ident):
    stamp = str(int(time.time()))
    signature = hmac.new(os.environ['BACKEND_AUTH_SECRET'].encode(), f'{user_id}:{stamp}'.encode(), hashlib.sha256).hexdigest()
    return {'x-supoclip-user-id': user_id, 'x-supoclip-ts': stamp,
            'x-supoclip-signature': signature, 'Idempotency-Key': 'capture-' + ident}


def handoff(store, row):
    with store.transaction() as connection:
        current = store._get(connection, KIND, row['id'])
        if current['state'] != 'ready':
            return
        row = store._put(connection, KIND, row['id'], {**current, 'state': 'handing_off', 'message': 'Transferring finalized recording to SupoClip'})
    base = os.getenv('SUP0CLIP_URL', 'http://backend:8000')
    headers = backend_headers(row['user_id'], row['id'])
    with httpx.Client(timeout=300) as client:
        if row.get('url_handoff'):
            source_url = row['source']
        elif not row.get('upload_url'):
            with open(row['selected_path'], 'rb') as media:
                response = client.post(base + '/upload', headers=headers, files={'video': ('capture.mp4', media, 'video/mp4')})
            response.raise_for_status()
            row = update(store, row['id'], upload_url=response.json()['video_path'])
            source_url = row['upload_url']
        else:
            source_url = row['upload_url']
        # Persist upload identity before task submission: SupoClip deduplicates
        # retries against this upload plus the stable idempotency key.
        response = client.post(base + '/tasks/', headers=headers,
                               json={**task_options(row), 'source': {'url': source_url}})
        response.raise_for_status()
        result = response.json()
        update(store, row['id'], state='submitted', task_id=result.get('task_id') or result['id'],
               message='Ready in SupoClip tasks for processing, review and publishing')
        if row.get('parent_id'):
            with store.transaction() as connection:
                parent = store._get(connection, KIND, row['parent_id'])
                task_id = result.get('task_id') or result['id']
                ids = list(parent.get('task_ids', []))
                if task_id not in ids:
                    ids.append(task_id)
                store._put(connection, KIND, parent['id'], {**parent, 'task_ids': ids, 'task_id': task_id})


def completed_youtube(source):
    """Fail open to recording when cheap metadata cannot establish a replay."""
    host = (urlparse(source).hostname or '').lower()
    if not any(host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be')):
        return False
    try:
        result = subprocess.run(['yt-dlp', '--no-playlist', '--skip-download', '--dump-single-json',
                                 '--socket-timeout', '5', '--retries', '0', source],
                                capture_output=True, text=True, timeout=15)
        return result.returncode == 0 and json.loads(result.stdout).get('live_status') in ('was_live', 'not_live')
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def submit_closed_batches(store, row, folder, running=True):
    """Submit a first minute then three-minute windows without stopping capture.

    Child IDs derive from their first segment, so worker retries reuse the same
    upload/task identity. Only closed segments are ever read while recording.
    """
    # A transient upload/task error must not strand a previously closed batch
    # until the entire stream finishes. Retry the same stable child identity.
    for pending in store.list(KIND):
        if pending.get('parent_id') == row['id'] and pending['state'] == 'ready':
            try:
                handoff(store, pending)
            except Exception as exc:
                latest = store.get(KIND, pending['id'])
                update(store, pending['id'], state=failure_state(latest, exc),
                       message='Batch handoff pending: ' + type(exc).__name__)
    current = store.get(KIND, row['id'])
    consumed = set(current.get('batched_segments', []))
    files = [p for p in closed_segments(folder, running) if p.name not in consumed]
    offset = float(current.get('batched_seconds', 0))
    batches = int(current.get('batch_count', 0))
    while files:
        count = 1 if batches == 0 else 3
        if running and len(files) < count:
            break
        chosen = files[:count]
        durations = [probe(p)['duration'] for p in chosen]
        end = offset + sum(durations)
        start = max(offset, row['start'])
        selected_end = min(end, row['end'])
        if selected_end > start:
            ident = row['id'] + '-batch-' + chosen[0].stem
            batch = store.get(KIND, ident)
            if not batch:
                manifest = Path(folder) / (ident + '.txt')
                manifest.write_text(''.join(f"file '{p.name}'\n" for p in chosen), encoding='utf-8')
                merged = Path(folder) / (ident + '.mp4')
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '1',
                                '-i', str(manifest), '-c', 'copy', '-movflags', '+faststart', str(merged)],
                               check=True, timeout=120)
                selected = str(merged)
                if start != offset or selected_end != end:
                    selected = trim_source({'id': 'native-' + ident, 'source_offset': start - offset,
                                            'source_end': selected_end - offset}, merged)
                batch = dict(id=ident, parent_id=row['id'], user_id=row['user_id'], source=row['source'],
                             state='ready', selected_path=selected, created=now(),
                             source_offset=start, source_end=selected_end, task_options=task_options(row),
                             message='Closed live batch ready for processing')
                store.put(KIND, ident, batch)
            if batch['state'] == 'ready':
                try:
                    handoff(store, batch)
                except Exception as exc:
                    latest = store.get(KIND, ident)
                    update(store, ident, state=failure_state(latest, exc), message='Batch handoff pending: ' + type(exc).__name__)
            latest = store.get(KIND, ident)
            if latest.get('task_id'):
                parent = store.get(KIND, row['id'])
                ids = list(parent.get('task_ids', []))
                if latest['task_id'] not in ids:
                    update(store, row['id'], task_ids=ids + [latest['task_id']], task_id=latest['task_id'])
        consumed.update(p.name for p in chosen)
        offset = end
        batches += 1
        update(store, row['id'], batched_segments=sorted(consumed), batched_seconds=offset, batch_count=batches)
        files = files[len(chosen):]


# Public adoption entry point for a recorder that is already writing segments.
process_available_batches = submit_closed_batches


def capture(store, row, stopping):
    if row.get('mode') == 'replay' or completed_youtube(row['source']):
        update(store, row['id'], state='ready', url_handoff=True,
               message='Processing entire completed video through normal video workflow; recording range does not apply')
        return
    recorder = Recorder()
    try:
        recorder.start({'id': row['id'], 'live_url': row['source']}, max_height=720, segment_seconds=60)
        update(store, row['id'], folder=str(recorder.folder), message='Recording; times begin when playable media arrives')
        deadline = time.monotonic() + row['end'] + 180
        while not stopping() and time.monotonic() < deadline:
            latest = store.get(KIND, row['id'])
            if latest.get('stop_requested'):
                recorder.stop()
                submit_closed_batches(store, row, recorder.folder, running=False)
                update(store, row['id'], state='stopped', message='Stopped; captured footage sent in closed batches')
                return
            seconds = recorder.captured_seconds()
            update(store, row['id'], recorded_seconds=seconds)
            submit_closed_batches(store, row, recorder.folder)
            if seconds >= row['end']:
                break
            if recorder.ffmpeg.poll() is not None:
                recorder.stop()
                submit_closed_batches(store, row, recorder.folder, running=False)
                raise ValueError('Stream ended before the selected end time; partial footage retained')
            if not enough_space():
                raise ValueError('Capture paused: less than 20 GB free')
            time.sleep(2)
        if stopping():
            update(store, row['id'], state='interrupted', message='Worker stopped; partial footage retained')
            return
        if recorder.captured_seconds() < row['end']:
            raise ValueError('Stream unavailable or selected range not received before timeout')
        recorder.stop()
        submit_closed_batches(store, row, recorder.folder, running=False)
        update(store, row['id'], state='completed', message='Recording completed; closed batches sent to SupoClip tasks')
    finally:
        recorder.stop()


def main():
    store = Store(os.environ['SUPERVISOR_DATABASE_URL'])
    halted = False
    def stop(*_):
        nonlocal halted
        halted = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    recover(store)
    while not halted:
        for row in sorted(store.list(KIND), key=lambda r: r.get('created', '')):
            if halted:
                break
            try:
                if row['state'] == 'queued':
                    with store.transaction() as connection:
                        current = store._get(connection, KIND, row['id'])
                        if current['state'] != 'queued':
                            continue
                        store._put(connection, KIND, row['id'], {**current, 'state': 'recording'})
                    capture(store, row, lambda: halted)
                current = store.get(KIND, row['id'])
                if current['state'] == 'ready':
                    handoff(store, current)
            except Exception as exc:
                # A finalized handoff is safe to retry; recording failures require
                # an explicit new capture rather than inventing missing footage.
                current = store.get(KIND, row['id'])
                update(store, row['id'], state=failure_state(current, exc),
                       message='Capture/handoff failed: ' + type(exc).__name__ + '. Check worker logs or retry source.')
                print(f"Capture {row['id']}: {type(exc).__name__}", flush=True)
        time.sleep(5)


if __name__ == '__main__':
    main()
