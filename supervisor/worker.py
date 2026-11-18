import asyncio
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import httpx
from .store import Store, now, render_variant
from .engines import Supoclip
from .ingest import acquire, normalize_render, DATA, enough_space, public_source, ReplayPending
from .editorial import metadata
from .media_effects import apply_effects, DISCLOSURE
from .openai_gateway import read_secret
from .telegram import poll
from .campaigns import healthy, monitor_deadlines, submit_pending
from .youtube import upload_private, next_slot, release, reconcile_upload, reconcile_release, release_capacity
from .live import LiveController, recover_segments
from .live_service import recover_sessions
from .maintenance import cleanup
from .analytics import collect, weekly

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('clipfarm')


def discover(store):
    for creator in store.list('creator'):
        if not creator.get('enabled') or not creator.get('feed'):
            continue
        feed = creator['feed']
        if not public_source(feed):
            continue
        marker = store.get('feed', creator['id']) or {'last': 0}
        if time.time() - marker['last'] < 300:
            continue
        try:
            response = httpx.get(feed, timeout=20)
            response.raise_for_status()
            entries = ET.fromstring(response.text).findall('{http://www.w3.org/2005/Atom}entry')
            for entry in entries[:5]:
                link = entry.find('{http://www.w3.org/2005/Atom}link')
                if link is not None and public_source(link.get('href', '')):
                    store.queue(creator['id'], link.attrib['href'])
            store.put('feed', creator['id'], {'last': time.time()})
        except Exception as exc:
            log.warning('Feed %s failed: %s', creator['id'], type(exc).__name__)


def _process_one(store, engine):
    jobs = sorted(store.list('job'), key=lambda j: j['created'])
    active = [j for j in jobs if j['state'] == 'processing']
    if active:
        job = active[0]
        if job.get('next_attempt', 0) > time.time():
            return
        result = engine.status(job['engine_id'])
        state = result.get('status')
        if state in ('completed', 'complete'):
            creator = store.get('creator', job['creator'])
            count = 0
            for artifact in engine.clips(job['engine_id'], DATA / 'media' / job['id'] / 'clips', job.get('source_offset', 0)):
                already = next((c for c in store.list('clip') if c['job_id'] == job['id'] and c.get('engine_clip_id') == artifact.get('engine_clip_id')), None)
                if already:
                    count += 1
                    continue
                artifact['path'], info = normalize_render(artifact['path'])
                artifact['probe'] = info
                # Neighboring live chunks may describe the same moment.
                prior = [c for c in store.list('clip') if store.get_job(c['job_id']).get('original_source') == job.get('original_source') and job.get('original_source') and store.get_job(c['job_id']).get('captured_at') == job.get('captured_at') and render_variant(store.get_job(c['job_id']).get('render_options')) == render_variant(job.get('render_options'))]
                if any(max(0, min(c['end'], artifact['end']) - max(c['start'], artifact['start'])) / max(1, artifact['end'] - artifact['start']) > .5 for c in prior):
                    continue
                artifact = apply_effects(store, artifact, job.get('render_options'), creator.get('campaign', ''))
                try:
                    artifact = metadata(store, artifact, creator, job.get('original_source', job['source']))
                except Exception as exc:
                    artifact['review_reasons'] = ['Editorial analysis unavailable: ' + type(exc).__name__]
                    artifact['description'] = ('#TJR\n' if creator.get('campaign') == 'tjr' else '') + 'Source: ' + job.get('original_source', job['source'])
                if artifact.get('ai_narration'):
                    artifact['title'] = 'AI narration | ' + artifact.get('title', 'Stream highlight').removeprefix('AI narration | ')
                    artifact['title'] = artifact['title'][:100]
                    if DISCLOSURE not in artifact.get('description', ''):
                        artifact['description'] = artifact.get('description', '') + '\n\n' + DISCLOSURE
                clip = store.add_clip(job['id'], artifact)
                channel = store.get('channel', creator['channel'])
                checks = clip.get('ai_checks', {})
                if channel.get('autopublish') and store.autopublish_ready() and not clip.get('validation_errors') and all(checks.get(k) for k in ('context', 'captions', 'visuals', 'metadata')):
                    # Automated decisions never count toward the initial human pilot.
                    store.update_clip(clip['id'], state='approved', checks=checks, auto_reviewed=now())
                store.notify(f"Clip ready for review: {clip['id']} — {clip['title']}. Open local controls on your PC.")
                count += 1
            store.update_job(job['id'], state='review' if count else 'held', error='' if count else 'No usable nonduplicate clips returned')
        elif state in ('error', 'failed', 'cancelled'):
            store.update_job(job['id'], state='held', error='Engine failed; inspect task ' + job['engine_id'])
            store.notify('Engine task requires review: ' + job['id'])
        return
    queued = [j for j in jobs if j['state'] in ('queued', 'retry') and j.get('next_attempt', 0) <= time.time()]
    if not queued or not read_secret('openai_key') or not enough_space():
        return
    job = queued[0]
    try:
        source = acquire(job)
        store.update_job(job['id'], state='submitting', local_source=source)
        engine_id = engine.submit(source, store.get('creator', job['creator']), job['id'])
        store.update_job(job['id'], engine_id=engine_id, state='processing')
    except ReplayPending:
        deadline = job.get('replay_deadline', time.time() + 86400)
        expired = time.time() >= deadline
        store.update_job(job['id'], state='held' if expired else 'retry',
                         replay_deadline=deadline, next_attempt=time.time() + 900,
                         error='Replay unavailable after 24 hours; inspect source' if expired else 'Waiting for completed replay; rechecking in 15 minutes')
        if expired:
            store.notify('Replay requires review: ' + job['id'])
    except Exception as exc:
        attempts = job.get('attempts', 0) + 1
        current = store.get_job(job['id'])
        ambiguous = current['state'] == 'submitting'
        store.update_job(job['id'], state='held' if ambiguous or attempts >= 3 else 'retry', attempts=attempts, next_attempt=time.time() + min(900, 60 * 2 ** attempts), error=str(exc)[:500])
        if ambiguous or attempts >= 3:
            store.notify('Job held: ' + job['id'] + '. Inspect local controls for the error.')


def process_one(store, engine):
    try:
        _process_one(store, engine)
    except Exception as exc:
        active = sorted([j for j in store.list('job') if j['state'] == 'processing'], key=lambda j: j['created'])
        if not active:
            raise
        job = active[0]
        count = job.get('processing_failures', 0) + 1
        store.update_job(job['id'], processing_failures=count, state='held' if count >= 3 else 'processing', next_attempt=time.time() + min(900, 60 * 2 ** count), error='Engine reconciliation failed: ' + type(exc).__name__)
        if count >= 3:
            store.notify('Engine output held after three reconciliation failures: ' + job['id'] + '. Later jobs can continue.')


def publish_jobs(store):
    for clip in store.list('clip'):
        job = store.get_job(clip['job_id'])
        creator = store.get('creator', job['creator'])
        channel = store.get('channel', creator['channel'])
        if clip['state'] == 'release_pending':
            # Reconcile public state even if the owner paused after an ambiguous response.
            try:
                if reconcile_release(store, clip, creator['channel']):
                    store.update_clip(clip['id'], publication_error='')
                    continue
            except Exception:
                continue
        manual = bool(clip.get('manual_publish'))
        if channel['paused'] and not manual:
            continue
        if clip.get('next_publish_attempt', 0) > time.time():
            continue
        if clip['state'] == 'approved' and channel.get('autopublish') and store.autopublish_ready() and store.publishable(clip['id']):
            if not store.request_auto_publication(clip['id']):
                continue
            clip = store.get('clip', clip['id'])
        if clip['state'] not in ('publish_queued', 'uploading', 'upload_unknown', 'private_uploaded', 'release_pending'):
            continue
        # Never start/release a campaign without a currently verified submission path.
        if creator.get('campaign') and not healthy(store, creator['campaign']):
            if manual:
                store.update_clip(clip['id'], publication_error='Campaign submission path is unavailable; publication held')
            continue
        if not (creator.get('permission') or manual and clip.get('permission_confirmed')) or not all(clip.get('checks', {}).get(k) for k in ('context', 'captions', 'visuals', 'metadata')):
            continue
        try:
            if clip['state'] in ('publish_queued', 'uploading'):
                upload_private(store, clip['id'], creator['channel'])
                store.update_clip(clip['id'], publication_error='', publish_attempts=0, next_publish_attempt=0)
            elif clip['state'] == 'upload_unknown':
                reconcile_upload(store, clip, creator['channel'])
                store.update_clip(clip['id'], publication_error='', publish_attempts=0, next_publish_attempt=0)
            else:
                if manual:
                    if release(store, clip, creator['channel']):
                        store.update_clip(clip['id'], publication_error='')
                        store.notify('Published: https://www.youtube.com/shorts/' + clip['youtube_id'])
                    continue
                if not clip.get('publish_at'):
                    used = [c['publish_at'] for c in store.list('clip') if c.get('publish_at') and store.get('creator', store.get_job(c['job_id'])['creator'])['channel'] == creator['channel']]
                    store.update_clip(clip['id'], publish_at=next_slot(datetime.now(timezone.utc), used).isoformat())
                elif datetime.fromisoformat(clip['publish_at']) <= datetime.now(timezone.utc):
                    current = datetime.now(timezone.utc)
                    published = [c['published_at'] for c in store.list('clip') if c.get('published_at') and store.get('creator', store.get_job(c['job_id'])['creator'])['channel'] == creator['channel']]
                    if not release_capacity(current, published):
                        booked = published + [c['publish_at'] for c in store.list('clip') if c.get('publish_at') and c['id'] != clip['id'] and store.get('creator', store.get_job(c['job_id'])['creator'])['channel'] == creator['channel']]
                        store.update_clip(clip['id'], publish_at=next_slot(current, booked).isoformat())
                        continue
                    if release(store, clip, creator['channel']):
                        store.notify('Published: https://www.youtube.com/shorts/' + clip['youtube_id'])
        except Exception as exc:
            current = store.get('clip', clip['id'])
            attempts = current.get('publish_attempts', 0) + 1
            if manual:
                store.update_clip(clip['id'], publish_attempts=attempts,
                    next_publish_attempt=time.time() + min(900, 30 * 2 ** min(attempts, 5)),
                    publication_error=type(exc).__name__ + ': reconnect YouTube if credentials expired; saved upload will be reconciled')
            row = store.get('channel', creator['channel'])
            store.put('channel', creator['channel'], {**row, 'paused': True})
            store.notify('YouTube integration paused after ' + type(exc).__name__ + '; inspect clip ' + clip['id'] + ' before resuming.')


def maintenance(store):
    stamp = datetime.now(timezone.utc).date().isoformat()
    marker = store.get('setting', 'daily') or {}
    if marker.get('date') == stamp:
        return
    cleanup(store, DATA)
    store.notify(f"Daily clipfarm report: {len(store.list('job'))} jobs, {len(store.list('clip'))} clips, {store.review_count()} reviewed; OpenAI committed {store.usage()['committed_usd']:.3f} USD. Autopublish ready: {store.autopublish_ready()}.")
    store.put('setting', 'daily', {'date': stamp})
    weekly(store)


def main():
    store = Store(os.environ['SUPERVISOR_DATABASE_URL'])
    engine = Supoclip()
    recover_segments(store)
    recover_sessions(store)
    live_controller = LiveController(store)
    # Submitting is intentionally not retried automatically: engine acceptance may be unknown.
    for job in store.list('job'):
        if job['state'] == 'submitting':
            store.update_job(job['id'], state='held', error='Interrupted submit: reconcile engine task before retrying')
    live_tick = live_controller.tick
    # One in-flight operation per subsystem. Acquiring/transcribing a video must
    # never prevent a campaign receipt, a kill switch, or an owner notification.
    actions = [('discovery', lambda: discover(store)), ('processing', lambda: process_one(store, engine)), ('publishing', lambda: publish_jobs(store)), ('submissions', lambda: submit_pending(store)), ('deadlines', lambda: monitor_deadlines(store)), ('telegram', lambda: poll(store)), ('analytics', lambda: collect(store)), ('maintenance', lambda: maintenance(store)), ('live', live_tick)]
    pool = ThreadPoolExecutor(max_workers=len(actions))
    futures = {}
    started = now()
    while True:
        request = store.get('setting', 'tick-requested') or {}
        heartbeat = store.get('setting', 'heartbeat') or {}
        store.put('setting', 'heartbeat', {'started': started, 'last_seen': now(), 'last_n8n_tick': now() if request.get('requested') else heartbeat.get('last_n8n_tick'), 'in_flight': [k for k, v in futures.items() if not v.done()]})
        if request.get('requested'):
            store.put('setting', 'tick-requested', {'requested': False})
        for name, action in actions:
            previous = futures.get(name)
            if previous is not None and not previous.done():
                continue
            if previous is not None and previous.exception() is not None:
                log.warning('%s failed: %s', name, type(previous.exception()).__name__)
            futures[name] = pool.submit(action)
        time.sleep(15)


if __name__ == '__main__':
    main()
