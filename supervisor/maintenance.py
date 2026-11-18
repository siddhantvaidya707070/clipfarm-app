import time
from pathlib import Path


def removable(path, clips, age_days):
    matches = [c for c in clips if Path(c.get('path', '')).resolve() == Path(path).resolve()]
    if not matches:
        return age_days >= 7
    return age_days >= 30 and all(c['state'] == 'published' and (not c.get('campaign') or c.get('submission_receipt')) for c in matches)


def cleanup(store, root):
    clips = store.list('clip')
    jobs = store.list('job')
    protected = [Path(j['local_source']).parent.resolve() for j in jobs if j.get('local_source') and j['state'] in ('queued', 'submitting', 'processing', 'retry', 'held')]
    removed = 0
    media = Path(root) / 'media'
    for path in media.rglob('*') if media.exists() else []:
        if not path.is_file() or any(p == path.resolve().parent or p in path.resolve().parents for p in protected):
            continue
        age = (time.time() - path.stat().st_mtime) / 86400
        if removable(path, clips, age):
            path.unlink()
            removed += 1
    return removed
