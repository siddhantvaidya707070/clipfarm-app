import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env = dict(line.split('=', 1) for line in (root / '.env').read_text(encoding='utf-8').splitlines() if '=' in line and not line.startswith('#'))
credential = [{'id': 'clipfarm-local-control', 'name': 'Clipfarm local control', 'type': 'httpHeaderAuth', 'data': {'name': 'Authorization', 'value': 'Bearer ' + env['CONTROL_TOKEN']}}]
(root / 'data/n8n-credentials.json').write_text(json.dumps(credential), encoding='utf-8')
print('Prepared local n8n credential import without printing its value.')
