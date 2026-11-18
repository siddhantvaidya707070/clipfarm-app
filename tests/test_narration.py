import pytest
from supervisor import narration


def test_budget_maximum_covers_worst_case():
    assert narration.reservation_cost('hello', 4096) > 4096 * 20


def test_usage_charged_conservatively_without_details():
    assert narration.usage_cost({'input_tokens': 10, 'output_tokens': 100}) == 2010
    assert narration.usage_cost({}) is None


def test_transcript_verbatim_check():
    assert narration.same_words('Hello, world!', 'hello world')
    assert not narration.same_words('Buy now', 'Do not buy now')


def test_ambiguous_cache_blocks_recharge(tmp_path, monkeypatch):
    class Store:
        def get(self, *args):
            return {'state': 'requested'}
    with pytest.raises(ValueError, match='ambiguous'):
        narration.synthesize(Store(), 'Hello', tmp_path)


def test_completed_cache_never_reserves(tmp_path):
    audio = tmp_path / 'cached.wav'
    audio.write_bytes(b'cached audio')
    class Store:
        def get(self, *args):
            return {'state': 'complete', 'path': str(audio)}
        def reserve(self, *args, **kwargs):
            pytest.fail('Cached audio must not incur another charge')
    assert narration.synthesize(Store(), 'Hello', tmp_path) == str(audio)
