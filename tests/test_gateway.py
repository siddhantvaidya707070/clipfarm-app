import pytest


def test_luna_judge_is_allowed_metered_and_tool_compatible(monkeypatch):
    from supervisor.openai_gateway import prepare, token_cost
    monkeypatch.setenv('OPENAI_JUDGE_MODEL', 'gpt-6-luna')
    body, reserve = prepare({'model': 'gpt-6-luna', 'max_tokens': 100, 'tools': [{'type': 'function'}]})
    assert body['reasoning_effort'] == 'none'
    assert reserve >= token_cost('gpt-6-luna', 10000, 100)
    assert token_cost('gpt-6-luna', 1000, 1000) == 600
    assert token_cost('gpt-6-luna', 300000, 1000) == 60750
    with pytest.raises(ValueError):
        prepare({'model': 'gpt-6-astra'})


def test_estimate_accounts_for_output_and_utf8():
    from supervisor.openai_gateway import prepare
    body, reserve = prepare({'model': 'gpt-5.4-mini', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert body['max_completion_tokens'] == 8192
    assert reserve >= 8192 * 4.5


def test_gateway_rejects_model_switch_and_streaming():
    from supervisor.openai_gateway import prepare
    for b in ({'model': 'other'}, {'model': 'gpt-5.4-mini', 'stream': True}):
        with pytest.raises(ValueError):
            prepare(b)


def test_gateway_never_preserves_unbounded_output():
    from supervisor.openai_gateway import prepare
    b, _ = prepare({'model': 'gpt-5.4-mini', 'max_tokens': 1000000})
    assert b['max_completion_tokens'] == 8192


def test_gateway_rejects_unmetered_tools_and_multiple_outputs():
    from supervisor.openai_gateway import prepare
    for extra in ({'tools': [{'type': 'web_search'}]}, {'n': 3}, {'service_tier': 'priority'}):
        with pytest.raises(ValueError):
            prepare({'model': 'gpt-5.4-mini', **extra})


def test_gateway_rejects_unbounded_server_side_context():
    from supervisor.openai_gateway import prepare
    for extra in ({'previous_response_id': 'resp_previous'}, {'conversation': 'conv_123'}, {'input': [{'type': 'input_file', 'file_id': 'file_large'}]}):
        with pytest.raises(ValueError):
            prepare({'model': 'gpt-5.4-mini', **extra}, endpoint='responses')
