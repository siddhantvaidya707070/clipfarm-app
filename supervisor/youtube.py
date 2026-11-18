import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import httpx

REDIRECT = 'http://localhost:8787/oauth/callback'
SCOPES = 'https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/yt-analytics.readonly'


def secret_path(name):
    return Path(os.getenv('SECRETS_DIR', '/secrets')) / name


def client_config():
    path = secret_path('google_client.json')
    if not path.exists():
        raise ValueError('Place your Google OAuth client JSON in clipfarm/secrets/google_client.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    return data.get('web', data.get('installed', {}))


def validate_destination(expected, actual):
    if not expected or not actual or expected != actual:
        raise ValueError('Authenticated YouTube channel does not match the configured destination')


def authorization_url(store, channel):
    if channel not in ('tjr', 'gaming'):
        raise ValueError('Unknown channel')
    config = client_config()
    state = secrets.token_urlsafe(32)
    store.put('oauth', state, {'channel': channel, 'expires': time.time() + 600, 'used': False})
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({'client_id': config['client_id'], 'redirect_uri': REDIRECT, 'response_type': 'code', 'scope': SCOPES, 'access_type': 'offline', 'prompt': 'consent', 'state': state})


async def complete_oauth(store, code, state):
    pending = store.get('oauth', state)
    if not pending or pending['used'] or time.time() > pending['expires']:
        raise ValueError('Invalid or expired OAuth state')
    config = client_config()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post('https://oauth2.googleapis.com/token', data={'client_id': config['client_id'], 'client_secret': config['client_secret'], 'code': code, 'redirect_uri': REDIRECT, 'grant_type': 'authorization_code'})
        response.raise_for_status()
        token = response.json()
        check = await client.get('https://www.googleapis.com/youtube/v3/channels', params={'part': 'id,snippet', 'mine': 'true'}, headers={'Authorization': 'Bearer ' + token['access_token']})
        check.raise_for_status()
    items = check.json().get('items', [])
    if len(items) != 1 or not token.get('refresh_token'):
        raise ValueError('Select exactly one YouTube channel and grant offline access')
    channel = pending['channel']
    row = store.get('channel', channel)
    if row.get('youtube_id'):
        validate_destination(row['youtube_id'], items[0]['id'])
    path = secret_path('youtube_' + channel + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(token), encoding='utf-8')
    temporary.replace(path)
    store.put('channel', channel, {**row, 'youtube_id': items[0]['id'], 'youtube_name': items[0]['snippet']['title'], 'paused': True})
    store.put('oauth', state, {**pending, 'used': True})
    return channel


def access_token(channel):
    path = secret_path('youtube_' + channel + '.json')
    if not path.exists():
        raise ValueError('Connect this YouTube channel first')
    data = json.loads(path.read_text(encoding='utf-8'))
    config = client_config()
    response = httpx.post('https://oauth2.googleapis.com/token', data={'client_id': config['client_id'], 'client_secret': config['client_secret'], 'refresh_token': data['refresh_token'], 'grant_type': 'refresh_token'}, timeout=30)
    response.raise_for_status()
    return response.json()['access_token']


def next_slot(current, already):
    eastern = ZoneInfo('America/New_York')
    used = [datetime.fromisoformat(x).astimezone(timezone.utc) for x in already]
    local = current.astimezone(eastern)
    for day in range(31):
        date = (local + timedelta(days=day)).date()
        if sum(x.astimezone(eastern).date() == date for x in used) >= 2:
            continue
        for hour in (18, 23):
            candidate = datetime(date.year, date.month, date.day, hour, tzinfo=eastern).astimezone(timezone.utc)
            if candidate > current and all(abs((candidate - x).total_seconds()) >= 14400 for x in used):
                return candidate
    raise ValueError('No free publication slot')


def release_capacity(current, published):
    eastern = ZoneInfo('America/New_York')
    rows = [datetime.fromisoformat(x).astimezone(timezone.utc) for x in published]
    if sum(x.astimezone(eastern).date() == current.astimezone(eastern).date() for x in rows) >= 2:
        return False
    return all((current - x).total_seconds() >= 14400 for x in rows)


def upload_private(store, clip_id, channel_id):
    clip = store.get('clip', clip_id)
    channel = store.get('channel', channel_id)
    token = access_token(channel_id)
    headers = {'Authorization': 'Bearer ' + token}
    with httpx.Client(timeout=180) as client:
        actual = client.get('https://www.googleapis.com/youtube/v3/channels', params={'part': 'id', 'mine': 'true'}, headers=headers)
        actual.raise_for_status()
        validate_destination(channel['youtube_id'], actual.json()['items'][0]['id'])
        path = Path(clip['path'])
        size = path.stat().st_size
        if not clip.get('upload_session'):
            if clip['state'] in ('upload_unknown', 'uploading'):
                raise ValueError('Upload is ambiguous: reconcile before initiating another upload')
            response = client.post('https://www.googleapis.com/upload/youtube/v3/videos', params={'uploadType': 'resumable', 'part': 'snippet,status', 'notifySubscribers': 'false'}, headers={**headers, 'X-Upload-Content-Length': str(size), 'X-Upload-Content-Type': 'video/mp4'}, json={'snippet': {'title': clip['title'][:100], 'description': clip['description'], 'tags': ['clipfarm-' + clip_id], 'categoryId': '20' if channel_id == 'gaming' else '24', 'defaultLanguage': 'en'}, 'status': {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False}})
            response.raise_for_status()
            session = response.headers['location']
            store.update_clip(clip_id, state='uploading', upload_session=session, upload_size=size)
            offset = 0
        else:
            session = clip['upload_session']
            response = client.put(session, headers={**headers, 'Content-Length': '0', 'Content-Range': f'bytes */{size}'})
            if response.status_code in (200, 201):
                return _finish_upload(store, clip_id, response.json())
            if response.status_code != 308:
                store.update_clip(clip_id, state='upload_unknown')
                raise ValueError('Upload session expired or failed; reconcile before retry')
            offset = int(response.headers.get('Range', 'bytes=0--1').rsplit('-', 1)[-1]) + 1 if 'Range' in response.headers else 0
        try:
            with path.open('rb') as video:
                video.seek(offset)
                while offset < size:
                    chunk = video.read(8 * 1024 * 1024)
                    response = client.put(session, headers={**headers, 'Content-Type': 'video/mp4', 'Content-Range': f'bytes {offset}-{offset + len(chunk) - 1}/{size}'}, content=chunk)
                    if response.status_code in (200, 201):
                        return _finish_upload(store, clip_id, response.json())
                    if response.status_code != 308:
                        response.raise_for_status()
                        raise ValueError('Unexpected resumable upload response')
                    offset += len(chunk)
                    store.update_clip(clip_id, upload_offset=offset)
        except Exception:
            store.update_clip(clip_id, state='upload_unknown')
            raise
    raise ValueError('No completed upload response; reconcile the session')


def _finish_upload(store, clip_id, result):
    if not result.get('id'):
        raise ValueError('YouTube did not return a video ID')
    store.update_clip(clip_id, state='private_uploaded', youtube_id=result['id'], upload_session=None)
    return result['id']


def release(store, clip, channel_id):
    token = access_token(channel_id)
    headers = {'Authorization': 'Bearer ' + token}
    with httpx.Client(timeout=30) as client:
        result = client.get('https://www.googleapis.com/youtube/v3/videos', params={'part': 'status,processingDetails,snippet', 'id': clip['youtube_id']}, headers=headers)
        result.raise_for_status()
        items = result.json().get('items', [])
        if items and items[0]['status'].get('privacyStatus') == 'public':
            store.update_clip(clip['id'], state='published', published_at=items[0]['snippet']['publishedAt'])
            return True
        if not items or items[0]['status'].get('uploadStatus') != 'processed':
            return False
        # Persist the release attempt before the external side effect.
        store.update_clip(clip['id'], state='release_pending', release_started_at=datetime.now(timezone.utc).isoformat())
        result = client.put('https://www.googleapis.com/youtube/v3/videos', params={'part': 'status'}, headers=headers, json={'id': clip['youtube_id'], 'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}})
        result.raise_for_status()
        store.update_clip(clip['id'], state='published', published_at=datetime.now(timezone.utc).isoformat())
        return True


def reconcile_release(store, clip, channel_id):
    token = access_token(channel_id)
    response = httpx.get('https://www.googleapis.com/youtube/v3/videos', params={'part': 'status,snippet', 'id': clip['youtube_id']}, headers={'Authorization': 'Bearer ' + token}, timeout=30)
    response.raise_for_status()
    items = response.json().get('items', [])
    if items and items[0]['status'].get('privacyStatus') == 'public':
        store.update_clip(clip['id'], state='published', published_at=items[0]['snippet']['publishedAt'])
        return True
    return False


def reconcile_upload(store, clip, channel_id):
    if clip.get('upload_session'):
        return upload_private(store, clip['id'], channel_id)
    raise ValueError('No resumable session. Inspect YouTube Studio and record the existing video ID before retrying.')
