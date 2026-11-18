"""One private-only native publication; reuses the same test post on reruns."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx

title = 'SupoClip native private connection test'
with httpx.Client(base_url='http://localhost:3107', timeout=60) as client:
    login = client.post('/api/auth/sign-in/email', json=json.loads(Path('secrets/native_login.json').read_text()), headers={'Origin': 'http://localhost:3107'})
    login.raise_for_status()
    connections = client.get('/api/social/connections')
    connections.raise_for_status()
    data = connections.json()
    accounts = data if isinstance(data, list) else data['connections']
    account = next(a for a in accounts if a['external_account_id'] == 'UCMmSzrmqhppNmjREQhEKVDQ')
    response = client.get('/api/social/posts')
    response.raise_for_status()
    posts = response.json()['posts']
    post = next((p for p in posts if p.get('title') == title), None)
    if not post:
        response = client.post('/api/social/posts', json={
            'task_id': '2c54c15c-f125-418b-9db2-31441e87ef5e',
            'clip_id': 'bd7d0e7d-3f9c-4679-a58a-587f83909503',
            'social_account_id': account['id'], 'title': title,
            'caption': 'Private connection verification. Not for public release.',
            'hashtags': [], 'privacy_level': 'private',
            'scheduled_for': (datetime.now(timezone.utc) + timedelta(seconds=50)).isoformat(),
        }, headers={'Origin': 'http://localhost:3107'})
        response.raise_for_status()
        post = response.json()['post']
    assert post['privacy_level'] == 'private'
    Path('data/native-private-test.json').write_text(json.dumps({'post_id': post['id']}))
    print('Native private test:', post['id'], post['status'], flush=True)
    previous = post['status']
    for _ in range(48):
        response = client.get('/api/social/posts/' + post['id'])
        response.raise_for_status()
        payload = response.json()
        post = payload.get('post', payload)
        if post['status'] != previous:
            print('Native post:', post['status'], flush=True)
            previous = post['status']
        if post['status'] == 'failed':
            raise RuntimeError(post.get('error_message') or 'Native private upload failed')
        if post['status'] == 'published':
            assert post['external_post_id']
            print('Native private publication passed:', post['external_post_id'])
            break
        time.sleep(5)
    else:
        raise RuntimeError('Native private publication still pending; test record preserved')
