"""Campaign eligibility and deadline tracking. Submission stays disabled until verified."""
import time
from datetime import datetime


def submission_state(published, current, receipt):
    if receipt:
        return 'submitted'
    elapsed = current - published
    if elapsed > 1800:
        return 'expired'
    return 'urgent' if elapsed >= 1500 else 'pending'


def healthy(store, campaign):
    config = store.get('campaign', campaign) or {}
    if config.get('paused') or not config.get('ai_editing_allowed') or not config.get('submission_verified'):
        return False
    if config.get('browser_health_until', 0) > time.time():
        return True
    from .browser_submission import perform
    try:
        result = perform(config)
        store.put('campaign', campaign, {**config, 'browser_health_until': result['healthy_until']})
        return True
    except Exception:
        return False


def submit_pending(store):
    from .browser_submission import perform
    for clip in store.list('clip'):
        creator = store.get('creator', store.get_job(clip['job_id'])['creator'])
        campaign = creator.get('campaign')
        if not campaign or clip['state'] != 'published' or clip.get('submission_receipt'):
            continue
        published = datetime.fromisoformat(clip['published_at']).timestamp()
        if time.time() - published > 1500 or clip.get('next_submission_attempt', 0) > time.time():
            continue
        try:
            receipt = perform(store.get('campaign', campaign) or {}, 'https://www.youtube.com/shorts/' + clip['youtube_id'])
            store.update_clip(clip['id'], submission_receipt=receipt)
        except Exception as exc:
            store.update_clip(clip['id'], next_submission_attempt=time.time() + 60, submission_error=type(exc).__name__)


def monitor_deadlines(store):
    for clip in store.list('clip'):
        if not clip.get('published_at') or clip.get('submission_receipt') or clip.get('submission_state') == 'expired':
            continue
        job = store.get_job(clip['job_id'])
        creator = store.get('creator', job['creator'])
        if not creator.get('campaign'):
            continue
        start = datetime.fromisoformat(clip['published_at']).timestamp()
        state = submission_state(start, time.time(), False)
        if state == 'expired':
            campaign = store.get('campaign', creator['campaign']) or {'id': creator['campaign']}
            store.put('campaign', creator['campaign'], {**campaign, 'paused': True, 'hold_reason': 'Expired submission: ' + clip['id']})
            store.update_clip(clip['id'], submission_state='expired')
        minute = int((time.time() - start) / 60)
        mark = 'expired' if state == 'expired' else '20' if minute >= 20 else '10' if minute >= 10 else ''
        if mark and clip.get('submission_alert') != mark:
            store.notify(f"Campaign submission {state}: clip {clip['id']}; published video {clip['youtube_id']}. Check the receipt before the 30-minute deadline.")
            store.update_clip(clip['id'], submission_alert=mark)
