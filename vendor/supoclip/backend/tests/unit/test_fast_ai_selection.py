"""One-request full-transcript AI selection with grounded output validation."""
from types import SimpleNamespace
import pytest


def dialogue():
    words = []
    for index in range(240):
        words.append({'start': index * 500, 'end': index * 500 + 350,
                      'text': ('Payoff.' if index % 16 == 15 else f'word{index}')})
    return {'words': words}


def test_ai_context_contains_entire_source_and_exact_timed_block_references():
    from src.services.fast_ai_selection import build_transcript_blocks, build_selection_prompt
    blocks = build_transcript_blocks(dialogue(), 120)
    prompt = build_selection_prompt(blocks, 120, 18, 'auto', '')
    assert 'word0' in prompt and 'word238' in prompt
    assert blocks[0]['first_word'] == 0 and blocks[-1]['last_word'] == 239
    assert blocks[0]['start'] == 0 and blocks[-1]['end'] == 119.85
    assert 'Do not pad' in prompt


def test_ai_ranges_are_grounded_non_overlapping_and_bad_ranges_cannot_become_media():
    from src.services.fast_ai_selection import build_transcript_blocks, validate_ai_selection
    blocks = build_transcript_blocks(dialogue(), 120)
    raw = {'clips': [[0, 90, 'word0 word1', 'Complete setup and payoff'],
                     [1, 80, 'word16 word17', 'Overlapping duplicate'],
                     [999, 100, 'Fake', 'Out of source'],
                     [4, 20, 'word64', 'Too weak']]}
    result = validate_ai_selection(raw, blocks, dialogue(), 120, target=18, clip_length='auto')
    assert len(result) == 1
    assert result[0]['source_ranges'] == [(0, 23.85)]
    assert result[0]['text'].startswith('word0 word1') and result[0]['text'].endswith('Payoff.')
    assert result[0]['selection_provider'] == 'ai'
    assert 'AI selected' in result[0]['reasoning']


def test_ai_validator_never_pads_sparse_speech_to_requested_count():
    from src.services.fast_ai_selection import build_transcript_blocks, validate_ai_selection
    sparse = {'words': [{'text': 'hello', 'start': 0, 'end': 200},
                        {'text': 'there.', 'start': 25000, 'end': 25200}]}
    blocks = build_transcript_blocks(sparse, 120)
    assert validate_ai_selection({'clips': [[0, 99, 'hello', 'Sparse speech']]},
                                 blocks, sparse, 120, target=18, clip_length='auto') == []


@pytest.mark.asyncio
async def test_ai_selection_uses_one_bounded_compact_request(monkeypatch):
    from src.services import fast_ai_selection as ai
    import httpx
    captured = []
    async def handle(request):
        import json
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content':
            '{"clips":[[0,90,"word0 word1","Complete setup and payoff"]]}'}}]})
    transport = httpx.MockTransport(handle)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=transport, **kwargs))
    config = SimpleNamespace(llm='openai:gpt-6-luna', openai_api_key='test-key')
    result = await ai.select_ai_highlights(dialogue(), 120, config, target=18)
    assert len(captured) == 1 and captured[0]['model'] == 'gpt-6-luna'
    assert captured[0]['max_tokens'] <= 2000
    assert len(result['segments']) == 1
    assert result['selection_provider'] == 'ai'


def test_paraphrased_ai_title_is_replaced_with_an_actual_source_quote():
    from src.services.fast_ai_selection import build_transcript_blocks, validate_ai_selection
    source = dialogue()
    selected = validate_ai_selection({'clips': [[0, 90, 'Invented murder discovery', 'Unsupported claim']]},
        build_transcript_blocks(source, 120), source, 120, target=18, clip_length='auto')
    assert len(selected) == 1
    assert selected[0]['hook_title'] == 'word0 word1 word2 word3 word4 word5 word6'
    assert 'murder' not in selected[0]['reasoning']
    assert selected[0]['virality_score'] == selected[0]['hook_score'] == 0


@pytest.mark.asyncio
async def test_http_failure_is_not_retried_as_another_ai_request(monkeypatch):
    from src.services import fast_ai_selection as ai
    import httpx
    calls = []
    async def handle(request):
        calls.append(1)
        return httpx.Response(503)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(ai.AISelectionUnavailable):
        await ai.select_ai_highlights(dialogue(), 120,
            SimpleNamespace(llm='openai:gpt-6-luna', openai_api_key='test-key'), target=18)
    assert calls == [1]


@pytest.mark.asyncio
async def test_explicit_ai_rejection_does_not_trigger_quota_filler(monkeypatch):
    from src.services import fast_ai_selection as ai, fast_highlights
    from src.services.video_service import VideoService
    async def empty(*args, **kwargs):
        return {'segments': [], 'selection_provider': 'ai', 'model': 'test', 'seconds': .01}
    monkeypatch.setattr(ai, 'select_ai_highlights', empty)
    monkeypatch.setattr(fast_highlights, 'select_fast_highlights', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('No filler fallback after a valid AI rejection')))
    async def checkpoint(patch):
        pass
    with pytest.raises(ValueError, match='No qualified standalone'):
        await VideoService._select_fast_highlight_segments(None, 120, 'auto',
            SimpleNamespace(fast_ai_selection_enabled=True), {}, checkpoint, transcript_data_override=dialogue())


@pytest.mark.asyncio
async def test_ai_unavailable_uses_only_disclosed_signal_qualified_fallback(monkeypatch):
    from src.services import fast_ai_selection as ai
    from src.services.video_service import VideoService
    async def unavailable(*args, **kwargs):
        raise ai.AISelectionUnavailable('AI selection unavailable (TimeoutError)')
    monkeypatch.setattr(ai, 'select_ai_highlights', unavailable)
    source = dialogue()
    for word in source['words']:
        word['text'] = 'Amazing!'
    saved = {}
    async def checkpoint(patch):
        saved.update(patch)
    analysis, selected, _, _ = await VideoService._select_fast_highlight_segments(None, 120, 'auto',
        SimpleNamespace(fast_ai_selection_enabled=True), {}, checkpoint, transcript_data_override=source)
    assert selected and all(s['low_confidence'] and s['selection_provider'] == 'local_fallback' for s in selected)
    assert all('AI selection unavailable' in s['reasoning'] for s in selected)
    assert analysis.selection_fallback is True
    assert saved['fast_selection']['fallback'] is True
