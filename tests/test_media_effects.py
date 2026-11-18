import pytest
from supervisor import media_effects as fx


def test_defaults_preserve_speech():
    assert fx.normalize_effects(None) == {'preset': 'clean', 'music': '', 'music_rights_confirmed': False, 'narration': False}


@pytest.mark.parametrize('name', ['../a.mp3', 'a\\b.mp3', '/a.mp3', 'a.exe', '.a.mp3'])
def test_music_paths_rejected(name):
    with pytest.raises(ValueError):
        fx.normalize_effects({'music': name, 'music_rights_confirmed': True})


def test_music_rights_and_tjr_narration():
    with pytest.raises(ValueError):
        fx.normalize_effects({'music': 'a.mp3'})
    with pytest.raises(ValueError):
        fx.normalize_effects({'narration': True}, 'tjr')
    with pytest.raises(ValueError):
        fx.normalize_effects({'narration': 'false'})
    with pytest.raises(ValueError):
        fx.normalize_effects([])


def test_clean_is_identity():
    artifact = {'path': 'does-not-need-reading.mp4'}
    assert fx.apply_effects(None, artifact, {}) is artifact


def test_music_filter_preserves_dialogue():
    command = fx.render_command('in.mp4', 'out.mp4', 15, {'preset': 'punchy', 'music': 'a.mp3'}, music_path='a.mp3')
    filters = command[command.index('-filter_complex') + 1]
    assert 'volume=-22dB' in filters
    assert 'sidechaincompress' in filters
    assert 'amix' in filters
    assert '-c:v' in command


def test_narration_never_truncates():
    with pytest.raises(ValueError, match='fit'):
        fx.render_command('in.mp4', 'out.mp4', 10, {'preset': 'clean', 'music': ''}, narration_path='speech.wav', narration_duration=14)


def test_licensed_local_music(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, 'DATA', tmp_path)
    (tmp_path / 'music').mkdir()
    (tmp_path / 'music' / 'licensed.wav').write_bytes(b'local file')
    options = fx.normalize_effects({'music': 'licensed.wav', 'music_rights_confirmed': True})
    assert options['music'] == 'licensed.wav'
    assert fx.media_options()['music'] == [{'name': 'licensed.wav'}]


def test_narration_label_and_original_bed():
    command = fx.render_command('in.mp4', 'out.mp4', 10, {'preset': 'clean', 'music': ''}, narration_path='speech.wav', narration_duration=11)
    filters = command[command.index('-filter_complex') + 1]
    assert 'AI NARRATION' in filters
    assert 'volume=-32dB' in filters
    assert 'atempo=1.11111111' in filters
