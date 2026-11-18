"""Copy only the configured OpenAI credential into the private service secret."""
from pathlib import Path


def sync_openai(env_file, destination):
    env_file, destination = Path(env_file), Path(destination)
    if not env_file.is_file():
        return 'unchanged'
    value = ''
    for line in env_file.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, separator, candidate = line.partition('=')
        if separator and name.strip() == 'OPENAI_API_KEY':
            candidate = candidate.strip()
            if candidate[:1] in ('"', "'") and candidate[-1:] == candidate[:1]:
                candidate = candidate[1:-1]
            else:
                candidate = candidate.split(' #', 1)[0].strip()
            if candidate and '\x00' not in candidate:
                value = candidate
    if not value:
        return 'unchanged'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(value, encoding='utf-8')
    temporary.replace(destination)
    return 'configured'


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    result = sync_openai(root.parent / '.env.local', root / 'secrets/openai_key')
    print('OpenAI server credential ' + result + '; no secret values displayed.')
