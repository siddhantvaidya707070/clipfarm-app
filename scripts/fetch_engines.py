"""Fetch immutable upstream snapshots; never installs nested Git repositories."""
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
lock_file = root / 'engines.lock.json'
lock = json.loads(lock_file.read_text(encoding='utf-8')) if lock_file.exists() else {}
for name, repo in [('openshorts', 'mutonby/openshorts'), ('medusa', 'Edualnog/medusa-clip')]:
    dest = root / 'vendor' / name
    if dest.exists():
        print(name + ': source already present')
        continue
    request = urllib.request.Request('https://api.github.com/repos/' + repo + '/commits/HEAD', headers={'User-Agent': 'ClipfarmLocalSetup'})
    with urllib.request.urlopen(request) as response:
        commit = json.load(response)['sha']
    archive = root / 'vendor' / (name + '.tar.gz')
    urllib.request.urlretrieve('https://codeload.github.com/' + repo + '/tar.gz/' + commit, archive)
    with tarfile.open(archive) as tar:
        top = tar.getmembers()[0].name.split('/')[0]
        tar.extractall(root / 'vendor', filter='data')
    (root / 'vendor' / top).rename(dest)
    lock[name] = {'repository': 'https://github.com/' + repo, 'commit': commit, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'qualified': False}
    print(name + ': pinned source downloaded; not enabled')
lock_file.write_text(json.dumps(lock, indent=2), encoding='utf-8')
