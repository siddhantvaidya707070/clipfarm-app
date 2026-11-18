"""Create local secrets without printing them or overwriting existing configuration."""
import hashlib
import json
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for directory in ('secrets', 'data', 'data/inbox', 'data/media', 'data/benchmarks', 'data/engine-temp', 'data/engine-clips', 'data/models'):
    (root / directory).mkdir(parents=True, exist_ok=True)
env = root / '.env'
if not env.exists():
    values = {k: secrets.token_hex(32) for k in ('POSTGRES_PASSWORD', 'SUPERVISOR_DB_PASSWORD', 'CONTROL_TOKEN', 'GATEWAY_TOKEN', 'BACKEND_AUTH_SECRET', 'APP_SETTINGS_ENCRYPTION_KEY', 'BETTER_AUTH_SECRET')}
    content = (root / '.env.example').read_text(encoding='utf-8')
    for key, value in values.items():
        content = content.replace(key + '=generated-by-bootstrap', key + '=' + value)
    content = content.replace('clipfarm:generated-by-bootstrap@', 'clipfarm:' + values['SUPERVISOR_DB_PASSWORD'] + '@')
    env.write_text(content, encoding='utf-8')
settings = dict(line.split('=', 1) for line in env.read_text(encoding='utf-8').splitlines() if '=' in line and not line.startswith('#'))
sql = root / 'data/init-supervisor.sql'
if not sql.exists():
    sql.write_text("CREATE USER clipfarm WITH PASSWORD '" + settings['SUPERVISOR_DB_PASSWORD'] + "';\nCREATE DATABASE clipfarm OWNER clipfarm;\nINSERT INTO users(id,name,email) VALUES ('clipfarm-owner','Clipfarm service','owner@clipfarm.local') ON CONFLICT DO NOTHING;\n", encoding='utf-8')
for name in ('openai_key', 'telegram_token'):
    p = root / 'secrets' / name
    if not p.exists():
        p.write_text('', encoding='utf-8')
commit = root / 'vendor/supoclip-commit.json'
if commit.exists():
    lock_path = root / 'engines.lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8')) if lock_path.exists() else {'qualified': False}
    lock['supoclip'] = {'repository': 'https://github.com/FujiwaraChoki/supoclip', 'commit': json.loads(commit.read_text(encoding='utf-8'))['sha'], 'archive_sha256': hashlib.sha256((root / 'vendor/supoclip.tar.gz').read_bytes()).hexdigest()}
    (root / 'engines.lock.json').write_text(json.dumps(lock, indent=2), encoding='utf-8')
print('Local configuration initialized. Secret values were not printed.')
