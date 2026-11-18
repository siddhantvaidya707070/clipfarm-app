import json
import pytest

from supervisor import highlight_media as media


def test_padded_windows_merge_and_only_missing_footage_is_planned():
    plan = getattr(media, 'plan_source_windows', lambda *a, **k: None)
    assert plan([{'start': 20, 'end': 40}, {'start': 45, 'end': 60}], 100,
                [{'start': 0, 'end': 35}]) == [(35, 70)]
    assert plan([{'start': 3, 'end': 10}, {'start': 90, 'end': 99}], 100, []) == [(0, 20), (80, 100)]


def test_sources_preserve_original_times_and_span_cached_pieces(tmp_path):
    one, two = tmp_path / 'one.mp4', tmp_path / 'two.mp4'
    one.write_bytes(b'one')
    two.write_bytes(b'two')
    row = {'source_windows': [{'path': str(one), 'start': 90, 'end': 130},
                              {'path': str(two), 'start': 130, 'end': 170}]}
    resolve = getattr(media, 'clip_sources', lambda *a: None)
    assert resolve(row, {'start': 120, 'end': 150}) == [
        {'path': str(one), 'start': 30, 'end': 40},
        {'path': str(two), 'start': 0, 'end': 20}]


def test_native_playlist_slicing_retains_init_and_sequence():
    parse = getattr(media, 'parse_source_playlist', lambda *a: None)
    playlist = '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:10,\na.ts\n#EXTINF:10,\nb.ts\n#EXTINF:10,\nc.ts\n#EXT-X-ENDLIST\n'
    parsed = parse(playlist, 'https://r.googlevideo.com/playlist.m3u8', 30)
    assert parsed is not None
    slice_playlist = getattr(media, 'slice_source_playlist', lambda *a: None)
    text, start, end = slice_playlist(parsed, 11, 19)
    assert (start, end) == (10, 20)
    assert '#EXT-X-MEDIA-SEQUENCE:8' in text
    assert 'init.mp4' in text and 'b.ts' in text
    assert 'a.ts' not in text and 'c.ts' not in text


def test_export_uses_source_local_seek_without_shifting_captions(monkeypatch, tmp_path):
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        if command[0] == 'ffprobe':
            return b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"20"}}'
        from pathlib import Path
        Path(command[-1]).write_bytes(b'export')
        return b''
    monkeypatch.setattr(media, 'run', execute)
    clip = {'id': 'a' * 32, 'start': 110, 'end': 130,
            'captions': [{'start': 1, 'end': 2, 'text': 'Hello'}],
            'source_parts': [{'path': str(tmp_path / 'source.mp4'), 'start': 10, 'end': 30}]}
    media.export_clip(tmp_path / 'source.mp4', tmp_path, clip, {'burn_captions': True})
    command = next(c for c in commands if c[0] == 'ffmpeg')
    assert command[command.index('-ss') + 1] == '10'
    assert '00:00:01,000 --> 00:00:02,000' in (tmp_path / ('a' * 32 + '.srt')).read_text()
    assert clip['start'] == 110 and clip['end'] == 130


def test_multi_piece_export_encodes_once_and_keeps_clip_duration(monkeypatch, tmp_path):
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        if command[0] == 'ffprobe':
            return b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"30"}}'
        from pathlib import Path
        Path(command[-1]).write_bytes(b'export')
        return b''
    monkeypatch.setattr(media, 'run', execute)
    media.export_clip('', tmp_path, {'id': 'b' * 32, 'start': 120, 'end': 150,
        'source_parts': [{'path': str(tmp_path / 'one.mp4'), 'start': 30, 'end': 40},
                         {'path': str(tmp_path / 'two.mp4'), 'start': 0, 'end': 20}]}, {'burn_captions': True})
    renders = [c for c in commands if c[0] == 'ffmpeg']
    assert len(renders) == 1
    command = renders[0]
    assert command.count('-i') == 2
    assert 'concat=n=2:v=1:a=1' in command[command.index('-filter_complex') + 1]
    assert command[command.index('-c:v') + 1] == 'libx264'


@pytest.mark.parametrize('byte_limit', [2_000_000_000, 1])
def test_cached_windows_are_reused_and_only_missing_handle_is_downloaded(monkeypatch, tmp_path, byte_limit):
    import io
    import yt_dlp
    monkeypatch.setenv('HIGHLIGHT_DOWNLOAD_MAX_BYTES', str(byte_limit))
    old = tmp_path / 'old.mp4'
    old.write_bytes(b'clean')
    prior = [{'path': str(old), 'start': 0, 'end': 40}]
    (tmp_path / 'source-windows.json').write_text(json.dumps(prior))
    playlist = '#EXTM3U\n' + ''.join(f'#EXTINF:10,\npart-{i}.ts\n' for i in range(10)) + '#EXT-X-ENDLIST\n'
    resolved, transferred = [], []
    class Client:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, source, download=False):
            resolved.append(source)
            return {'id': 'abcdefghijk', 'formats': [{'format_id': '96', 'url': 'https://r.googlevideo.com/list.m3u8',
                'height': 1080, 'fps': 30, 'vcodec': 'avc1', 'acodec': 'none', 'ext': 'mp4', 'protocol': 'm3u8_native'},
                {'format_id': '234', 'url': 'https://r.googlevideo.com/audio.m3u8', 'vcodec': 'none',
                 'acodec': None, 'ext': 'mp4', 'protocol': 'm3u8_native', 'language': 'en', 'format_note': 'Default, high'}]}
        def urlopen(self, request):
            response = io.BytesIO(playlist.encode())
            response.url = 'https://r.googlevideo.com/list.m3u8'
            return response
        def process_info(self, info):
            from pathlib import Path
            transferred.append(info['hls_media_playlist_data'])
            for hook in self.options['progress_hooks']:
                hook({'filename': self.options['outtmpl'], 'downloaded_bytes': 8})
            Path(self.options['outtmpl'].replace('%(ext)s', 'mp4')).write_bytes(b'fragments')
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', Client)
    def execute(command, **kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b'clean-window')
        return b''
    monkeypatch.setattr(media, 'run', execute)
    if byte_limit == 1:
        with pytest.raises(ValueError, match='byte budget'):
            media.acquire_source_windows('https://youtu.be/abcdefghijk', tmp_path,
                                         [{'start': 30, 'end': 50}], 100)
        assert json.loads((tmp_path / 'source-windows.json').read_text()) == prior
        assert list(tmp_path.glob('window-*.mp4')) == []
        return
    full, pieces = media.acquire_source_windows('https://youtu.be/abcdefghijk', tmp_path,
                                               [{'start': 30, 'end': 50}], 100)
    assert full == '' and len(pieces) == 2
    assert len(resolved) == 1 and len(transferred) == 2
    assert 'part-4.ts' in transferred[0] and 'part-5.ts' in transferred[0]
    assert 'part-3.ts' not in transferred[0]
    assert media.clip_sources({'source_windows': pieces}, {'start': 30, 'end': 50}) == [
        {'path': str(old), 'start': 30, 'end': 40},
        {'path': pieces[1]['path'], 'start': 0, 'end': 10}]
    # A restart can restore the clean media map without another metadata request.
    assert media.acquire_source_windows('https://youtu.be/abcdefghijk', tmp_path,
                                        [{'start': 30, 'end': 50}], 100)[1] == pieces
    assert len(resolved) == 1


def test_unsupported_hls_features_fail_without_fetching_full_source():
    import pytest
    for content in ('#EXT-X-KEY:METHOD=AES-128,URI="key"\n', '#EXT-X-BYTERANGE:100\n', '#EXT-X-DISCONTINUITY\n'):
        with pytest.raises(ValueError):
            media.parse_source_playlist('#EXTM3U\n' + content + '#EXTINF:10,\npart.ts\n#EXT-X-ENDLIST\n',
                                        'https://r.googlevideo.com/list.m3u8', 10)


def test_adaptive_full_source_requires_cheaper_exact_size_and_matching_quality():
    choose = getattr(media, 'select_cheaper_full_source', lambda *args: None)
    selected = [{'height': 1080, 'width': 1920, 'fps': 60, 'vcodec': 'vp9'},
                {'language': 'en', 'abr': 128}]
    video = {'format_id': '303', 'height': 1080, 'width': 1920, 'fps': 60, 'vcodec': 'vp9',
             'acodec': 'none', 'protocol': 'https', 'filesize': 80_000_000}
    audio = {'format_id': '140', 'vcodec': 'none', 'acodec': 'aac', 'protocol': 'https',
             'filesize': 10_000_000, 'language': 'en', 'abr': 128}
    info = {'formats': [video, audio], 'language': 'en'}
    assert choose(info, selected, 150_000_000, 200_000_000) == [video, audio]
    assert choose(info, selected, 50_000_000, 200_000_000) is None
    assert choose(info, selected, 150_000_000, 85_000_000) is None
    for altered in ({'fps': 30}, {'height': 720}, {'filesize': None, 'filesize_approx': 80_000_000}):
        assert choose({'formats': [{**video, **altered}, audio]}, selected, 150_000_000, 200_000_000) is None


def test_adaptive_acquisition_uses_exact_formats_from_shared_metadata(monkeypatch, tmp_path):
    import io
    from pathlib import Path
    import yt_dlp
    metadata_calls, full_calls = [], []
    formats = [
        {'format_id': 'hlsv', 'protocol': 'm3u8_native', 'vcodec': 'vp9', 'acodec': 'none', 'height': 1080,
         'width': 1920, 'fps': 60, 'tbr': 10000, 'url': 'https://r.googlevideo.com/video.m3u8'},
        {'format_id': 'hlsa', 'protocol': 'm3u8_native', 'vcodec': 'none', 'acodec': None,
         'language': 'en', 'abr': 128, 'tbr': 128, 'url': 'https://r.googlevideo.com/audio.m3u8'},
        {'format_id': '303', 'protocol': 'https', 'vcodec': 'vp9', 'acodec': 'none', 'height': 1080,
         'width': 1920, 'fps': 60, 'filesize': 10000000, 'url': 'https://r.googlevideo.com/video'},
        {'format_id': '140', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'aac', 'language': 'en',
         'abr': 128, 'filesize': 1000000, 'url': 'https://r.googlevideo.com/audio'}]
    class Client:
        def __init__(self, options):
            self.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, source, download=False):
            metadata_calls.append(source)
            return {'id': 'abcdefghijk', 'duration': 100, 'formats': formats}
        def urlopen(self, request):
            response = io.BytesIO(('#EXTM3U\n' + ''.join(f'#EXTINF:10,\npart-{i}.ts\n' for i in range(10)) + '#EXT-X-ENDLIST\n').encode())
            response.url = 'https://r.googlevideo.com/list.m3u8'
            return response
        def process_ie_result(self, info, download=True):
            full_calls.append(self.options['format'])
            assert info['formats'] == formats
            Path(self.options['outtmpl'].replace('%(ext)s', 'mp4')).write_bytes(b'clean-full')
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', Client)
    full, windows = media.acquire_source_windows('https://youtu.be/abcdefghijk', tmp_path,
                                                 [{'start': 10, 'end': 90}], 100)
    assert Path(full).read_bytes() == b'clean-full'
    assert windows == [] and len(metadata_calls) == 1
    assert full_calls == ['303+140']
    decision = json.loads((tmp_path / 'source-acquisition.json').read_text())
    assert decision['mode'] == 'full' and decision['full_bytes'] == 11000000
