from datetime import datetime, timezone
import time
import httpx
from .youtube import access_token


def due_windows(age_seconds, done):
    return [hour for hour in (24, 72, 168) if age_seconds >= hour * 3600 and hour not in done]


def collect(store):
    for clip in store.list('clip'):
        if clip['state'] != 'published' or not clip.get('published_at') or clip.get('analytics_retry', 0) > time.time():
            continue
        published = datetime.fromisoformat(clip['published_at'])
        windows = due_windows((datetime.now(timezone.utc) - published).total_seconds(), clip.get('analytics_done', []))
        if not windows:
            continue
        creator = store.get('creator', store.get_job(clip['job_id'])['creator'])
        try:
            token = access_token(creator['channel'])
            with httpx.Client(timeout=30) as client:
                result = client.get('https://www.googleapis.com/youtube/v3/videos', params={'part': 'statistics', 'id': clip['youtube_id']}, headers={'Authorization': 'Bearer ' + token})
                result.raise_for_status()
                stats = result.json().get('items', [])
                if not stats:
                    raise ValueError('Published video is unavailable')
                params = {'ids': 'channel==MINE', 'startDate': published.date().isoformat(), 'endDate': datetime.now(timezone.utc).date().isoformat(), 'metrics': 'views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage', 'filters': 'video==' + clip['youtube_id']}
                report = client.get('https://youtubeanalytics.googleapis.com/v2/reports', params=params, headers={'Authorization': 'Bearer ' + token})
                analytics = report.json() if report.status_code == 200 else {'available': False, 'http_status': report.status_code}
            for hour in windows:
                store.put('analytics', clip['id'] + ':' + str(hour), {'clip_id': clip['id'], 'window_hours': hour, 'collected_at': datetime.now(timezone.utc).isoformat(), 'statistics': stats[0]['statistics'], 'analytics': analytics})
            store.update_clip(clip['id'], analytics_done=sorted(set(clip.get('analytics_done', []) + windows)))
        except Exception:
            store.update_clip(clip['id'], analytics_retry=time.time() + 3600)


def weekly(store):
    week = datetime.now(timezone.utc).strftime('%G-%V')
    if (store.get('setting', 'weekly') or {}).get('week') == week:
        return
    records = store.list('analytics')
    ranked = sorted(records, key=lambda r: int(r.get('statistics', {}).get('viewCount', 0)), reverse=True)
    recommendations = [{'clip': r['clip_id'], 'views': r['statistics'].get('viewCount'), 'window_hours': r['window_hours']} for r in ranked[:5]]
    store.put('setting', 'weekly', {'week': week, 'top_observations': recommendations, 'policy': 'No automatic source expansion or increased posting frequency. Review retention and distinct source performance before changing limits.'})
    store.notify('Weekly performance observations saved in local status. No automatic volume increase.')
