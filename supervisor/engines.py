import hashlib
import hmac
import os
import time
from pathlib import Path
import httpx


def seconds(value):
    if isinstance(value, str) and ':' in value:
        total = 0
        for part in value.split(':'):
            total = total * 60 + float(part)
        return total
    return float(value)


def normalize_clip(row, offset=0):
    name = row.get('filename') or Path(row.get('file_path', '')).name
    if not name or Path(name).name != name or '/' in name or '\\' in name or name.startswith('.'):
        raise ValueError('Unsafe engine output path')
    start = seconds(row.get('start_time', row.get('start', 0))) + offset
    end = seconds(row.get('end_time', row.get('end', 0))) + offset
    if not 0 <= start < end:
        raise ValueError('Invalid engine clip bounds')
    return {'start': start, 'end': end, 'filename': name, 'engine_clip_id': row['id'], 'transcript': row.get('text', row.get('transcript', '')), 'title': row.get('hook_title') or row.get('title') or 'Stream highlight', 'description': '', 'engine': 'supoclip'}


class Supoclip:
    def __init__(self):
        self.base = os.getenv('SUP0CLIP_URL', 'http://backend:8000')

    def headers(self, job_key=None):
        stamp = str(int(time.time()))
        signature = hmac.new(os.environ['BACKEND_AUTH_SECRET'].encode(), ('clipfarm-owner:' + stamp).encode(), hashlib.sha256).hexdigest()
        headers = {'x-supoclip-user-id': 'clipfarm-owner', 'x-supoclip-ts': stamp, 'x-supoclip-signature': signature}
        if job_key:
            headers['Idempotency-Key'] = job_key
        return headers

    def submit(self, path, profile, job_key):
        with httpx.Client(timeout=180) as client:
            with open(path, 'rb') as file:
                upload = client.post(self.base + '/upload', headers=self.headers(job_key), files={'video': (Path(path).name, file, 'video/mp4')})
            upload.raise_for_status()
            result = client.post(self.base + '/tasks/', headers=self.headers(job_key), json={'source': {'url': upload.json()['video_path']}, 'include_broll': False, 'add_subtitles': True, 'output_format': 'vertical', 'clip_length': 'auto', 'processing_mode': 'balanced'})
            result.raise_for_status()
            data = result.json()
            return data.get('task_id') or data['id']

    def status(self, ident):
        with httpx.Client(timeout=30) as client:
            response = client.get(self.base + '/tasks/' + ident, headers=self.headers())
            response.raise_for_status()
            return response.json()

    def clips(self, ident, folder, offset=0):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=180) as client:
            response = client.get(self.base + '/tasks/' + ident + '/clips', headers=self.headers())
            response.raise_for_status()
            for row in response.json()['clips']:
                result = normalize_clip(row, offset)
                path = folder / result['filename']
                temporary = path.with_suffix('.partial')
                if not path.exists():
                    with client.stream('GET', f"{self.base}/tasks/{ident}/clips/{row['id']}/file", headers=self.headers()) as media:
                        media.raise_for_status()
                        with temporary.open('wb') as out:
                            for chunk in media.iter_bytes():
                                out.write(chunk)
                    temporary.replace(path)
                yield {**result, 'path': str(path)}

    def resume(self, ident):
        with httpx.Client(timeout=30) as client:
            response = client.post(self.base + '/tasks/' + ident + '/resume', headers=self.headers())
            response.raise_for_status()
            return response.json()
