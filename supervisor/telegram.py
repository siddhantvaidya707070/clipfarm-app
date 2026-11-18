import os
import time
import httpx
from .openai_gateway import read_secret


def poll(store):
    token = read_secret('telegram_token')
    owner = os.getenv('TELEGRAM_OWNER_ID', '')
    if not token or not owner:
        return
    with httpx.Client(timeout=15) as client:
        cursor = (store.get('setting', 'telegram') or {}).get('offset', 0)
        result = client.get(f'https://api.telegram.org/bot{token}/getUpdates', params={'offset': cursor, 'timeout': 0})
        result.raise_for_status()
        for entry in result.json().get('result', []):
            message = entry.get('message', {})
            if str(message.get('from', {}).get('id')) == owner and str(message.get('chat', {}).get('id')) == owner:
                words = message.get('text', '').split()
                if words and words[0] == '/pause':
                    for row in store.list('channel'):
                        store.put('channel', row['id'], {**row, 'paused': True})
                    store.notify('Both channels paused.')
                elif len(words) == 2 and words[0] == '/skip':
                    try:
                        store.review(words[1], False)
                    except ValueError:
                        store.notify('That clip cannot be skipped in its current state.')
                elif words and words[0] == '/status':
                    store.notify(f"Jobs: {len(store.list('job'))}. Reviewed: {store.review_count()}. Usage: {store.usage()}. Local review: http://localhost:8787")
                elif words and words[0] == '/approve':
                    store.notify('Approve in local controls after checking context, captions, framing and metadata. Remote approval requires the preview and all checks.')
            cursor = entry['update_id'] + 1
            store.put('setting', 'telegram', {'offset': cursor})
        for item in [n for n in store.list('notification') if not n['sent']][:5]:
            if item['sent']:
                continue
            response = client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id': owner, 'text': item['text'][:4000]})
            if response.status_code == 200 and response.json().get('ok'):
                store.put('notification', item['id'], {**item, 'sent': True})
