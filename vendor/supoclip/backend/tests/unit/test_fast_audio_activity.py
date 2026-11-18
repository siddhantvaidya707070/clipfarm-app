from types import SimpleNamespace
import numpy as np


def test_silent_selection_is_removed_and_audible_speech_kept(monkeypatch, tmp_path):
    from src.services import fast_audio_activity as mod
    samples = np.concatenate([np.zeros(10000), np.full(10000, .02)]).astype('<f4')
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=samples.tobytes()))
    silent = {'source_ranges': [(0, 8)], 'text': 'Unreliable caption text'}
    active = {'source_ranges': [(12, 19)], 'text': 'Audible reaction'}
    kept, metrics = mod.filter_silent_highlights(tmp_path/'source.mp4', [silent, active])
    assert kept == [active]
    assert metrics[0]['decision'] == 'silent'
    assert metrics[1]['decision'] == 'keep'


def test_original_ranges_are_mapped_before_audio_check(monkeypatch, tmp_path):
    from src.services import fast_audio_activity as mod
    samples = np.concatenate([np.zeros(10000), np.full(10000, .015)]).astype('<f4')
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=samples.tobytes()))
    mapper = SimpleNamespace(to_local_ranges=lambda ranges: [(10, 18)])
    segment = {'source_ranges': [(1000, 1008)], 'text': 'Actual source words'}
    kept, metrics = mod.filter_silent_highlights(tmp_path/'source.mp4', [segment], mapper)
    assert kept == [segment] and metrics[0]['active_fraction'] > .99


def test_decode_failure_is_reported_without_discarding_every_candidate(monkeypatch, tmp_path):
    from src.services import fast_audio_activity as mod
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b''))
    segment = {'source_ranges': [(0, 15)]}
    kept, metrics = mod.filter_silent_highlights(tmp_path/'source.mp4', [segment])
    assert kept == [segment] and metrics[0]['decision'] == 'unverified'
