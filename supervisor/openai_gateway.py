import json
import math
import os
from pathlib import Path
import httpx


def read_secret(name):
    path = Path(os.getenv('SECRETS_DIR', '/secrets')) / name
    return path.read_text(encoding='utf-8').strip() if path.exists() else ''


def token_cost(model, incoming, outgoing):
    # Microdollars/token; cached-input discounts are conservatively ignored.
    # Luna standard rates: developers.openai.com/api/docs/models/gpt-6-luna
    if model == 'gpt-6-luna':
        input_rate, output_rate = (.2, .75) if incoming > 272000 else (.1, .5)
    elif model == 'gpt-5.4-mini':
        input_rate, output_rate = .75, 4.5
    else:
        raise ValueError('No budget rates configured for this model')
    return math.ceil(incoming * input_rate + outgoing * output_rate)


def prepare(body, endpoint='chat/completions'):
    body = dict(body)
    allowed = {os.getenv('OPENAI_MODEL', 'gpt-5.4-mini'), os.getenv('OPENAI_JUDGE_MODEL', '')} - {''}
    if body.get('model') not in allowed:
        raise ValueError('Only the configured model is allowed')
    token_cost(body['model'], 0, 0)
    if body['model'] == 'gpt-6-luna' and endpoint == 'chat/completions':
        # SupoClip's Pydantic agent uses function-tool structured output. Luna
        # requires reasoning_effort=none for tools on this endpoint.
        body['reasoning_effort'] = 'none'
    if body.get('stream'):
        raise ValueError('Streaming is disabled to preserve usage accounting')
    if body.get('n', 1) != 1 or body.get('service_tier', 'default') not in ('auto', 'default'):
        raise ValueError('Only one output at the standard service tier is metered')
    if any(tool.get('type') != 'function' for tool in body.get('tools', [])):
        raise ValueError('Built-in billable tools are disabled')
    forbidden = {'previous_response_id', 'conversation', 'prompt', 'file_id', 'file_url', 'file_data', 'input_audio', 'audio', 'background'}
    def check_bounded(value):
        if isinstance(value, dict):
            if forbidden.intersection(value):
                raise ValueError('Server-side context, files and audio are not supported by this text/image budget')
            if value.get('type') in ('input_file', 'input_audio', 'file'):
                raise ValueError('Only inline text and image input is metered')
            for key, item in value.items():
                if key == 'image_url' and isinstance(item, str) and not item.startswith('data:image/'):
                    raise ValueError('Images must use inline data URLs')
                check_bounded(item)
        elif isinstance(value, list):
            for item in value:
                check_bounded(item)
    check_bounded(body)
    if endpoint == 'responses':
        body['store'] = False
    encoded_input = json.dumps(body, ensure_ascii=False)
    if '"image_url"' in encoded_input:
        for message in body.get('messages', []):
            content = message.get('content', [])
            if isinstance(content, list):
                for part in content:
                    if part.get('type') == 'image_url':
                        image = part.get('image_url', {})
                        url = image.get('url', '') if isinstance(image, dict) else image
                        if not url.startswith('data:image/'):
                            raise ValueError('Images must use inline data URLs for bounded metering')
    body['service_tier'] = 'default'
    limit = body.pop('max_tokens', body.pop('max_completion_tokens', body.pop('max_output_tokens', 8192)))
    limit = min(max(1, int(limit)), 8192)
    body['max_output_tokens' if endpoint == 'responses' else 'max_completion_tokens'] = limit
    body.pop('temperature', None)
    body.pop('top_p', None)
    encoded = json.dumps(body, ensure_ascii=False).encode()
    if len(encoded) > 8000000:
        raise ValueError('Model request exceeds 8 MB')
    # Bytes upper-bound text tokenization; image data URLs are counted in full.
    # Large conservative overhead covers schemas, messages and image processing.
    reserve = token_cost(body['model'], len(encoded) * 2 + 10000, limit)
    return body, reserve


async def forward(store, body, endpoint):
    key = os.getenv('OPENAI_API_KEY', '').strip() or read_secret('openai_key')
    if not key:
        raise ValueError('Add your OpenAI API key in local settings first')
    body, maximum = prepare(body, endpoint)
    reservation = store.reserve(maximum, daily=int(float(os.getenv('OPENAI_DAILY_USD', '1')) * 1000000), monthly=int(float(os.getenv('OPENAI_MONTHLY_USD', '10')) * 1000000))
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post('https://api.openai.com/v1/' + endpoint, json=body, headers={'Authorization': 'Bearer ' + key})
    if response.status_code >= 400:
        # Known rejected requests incur no model usage; ambiguous 5xx retains reserve.
        if 400 <= response.status_code < 500:
            store.settle(reservation, 0)
        return response.status_code, {'error': {'message': f'OpenAI returned HTTP {response.status_code}; check account/model access.', 'type': 'upstream_error'}}
    data = response.json()
    usage = data.get('usage', {})
    incoming = usage.get('prompt_tokens', usage.get('input_tokens'))
    outgoing = usage.get('completion_tokens', usage.get('output_tokens'))
    if incoming is not None and outgoing is not None:
        store.settle(reservation, token_cost(body['model'], incoming, outgoing))
    return response.status_code, data
