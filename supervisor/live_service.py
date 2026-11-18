"""Persistent requests; only the scheduler owns capture subprocesses."""
import uuid
from .store import now
from .ranges import validate_range
from .ingest import public_source


def list_sessions(store):
    return sorted(store.list('live_session'), key=lambda row: row['created'], reverse=True)


def get_session(store, ident):
    row = store.get('live_session', ident)
    if not row:
        raise ValueError('Unknown live session')
    if row.get('job_id'):
        job = store.get_job(row['job_id'])
        row = {**row, 'job_state': job['state'] if job else 'missing'}
    return row


def start_session(store, creator, source, start=0, end=None, time_basis='recording', **extra):
    start, end = validate_range(start, end)
    if not store.get('creator', creator):
        raise ValueError('Unknown creator')
    if not public_source(source):
        raise ValueError('Use a supported public HTTPS live URL')
    if time_basis not in ('recording', 'original'):
        raise ValueError('Time basis must be recording or original')
    ident = uuid.uuid4().hex
    row = {'id': ident, 'creator': creator, 'source': source, 'start': start, 'end': end,
           'time_basis': time_basis, 'created': now(), 'state': 'queued', 'stop_requested': False,
           'message': 'Waiting for scheduler; times are relative to recording start',
           **{k: extra[k] for k in ('render_options',) if k in extra}}
    if time_basis == 'original':
        job = store.queue(creator, source, source_offset=start, **({'source_end': end} if end is not None else {}), **extra)
        row.update(state='replay_pending', job_id=job['id'], message='Original broadcast timestamps require a completed replay; past live footage was not recorded')
        return store.put('live_session', ident, row)
    with store.transaction() as connection:
        # The transaction guard serializes concurrent API start requests.
        from sqlalchemy import select
        sessions = connection.execute(select(store.records.c.data).where(store.records.c.kind == 'live_session'))
        if any(r[0]['state'] in ('queued', 'recording', 'stopping') for r in sessions):
            raise ValueError('A live session is already queued or recording; stop it first')
        return store._put(connection, 'live_session', ident, row)


def stop_session(store, ident):
    with store.transaction() as connection:
        row = store._get(connection, 'live_session', ident)
        if not row:
            raise ValueError('Unknown live session')
        if row['state'] in ('queued', 'recording', 'stopping'):
            row.update(stop_requested=True, state='stopped' if row['state'] == 'queued' else 'stopping', updated=now())
        return store._put(connection, 'live_session', ident, row)


def recover_sessions(store):
    for row in list_sessions(store):
        if row['state'] in ('recording', 'stopping'):
            store.put('live_session', row['id'], {**row, 'state': 'interrupted', 'updated': now(),
                      'message': 'Scheduler restarted; closed MP4 jobs retained. Inspect final TS before import; start a new session to continue.'})
