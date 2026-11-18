from pathlib import Path
import pytest
from src import youtube_proxy as proxy
from src import youtube_source as source
from src.config import Config, set_config_override


@pytest.fixture(autouse=True)
def direct_config(tmp_path):
    config = Config()
    config.youtube_proxy_url = ""
    config.temp_dir = str(tmp_path)
    config.youtube_source_cache_ttl_seconds = 0
    set_config_override(config)
    yield
    set_config_override(None)


def test_direct_options_do_not_require_a_paid_proxy():
    options = proxy.download_options("job", "abcdefghijk", proxy.ByteBudget(1000))
    assert options["proxy"] == ""
    assert options["concurrent_fragment_downloads"] == 16
    with pytest.raises(proxy.DownloadBudgetExceeded):
        options["progress_hooks"][0]({"status": "downloading", "filename": "audio", "downloaded_bytes": 1001})


def test_retry_from_zero_is_counted_again():
    budget = proxy.ByteBudget(1000)
    hook = proxy._progress_hook(budget, {})
    for count in (100, 20, 90):
        hook({'status': 'downloading', 'filename': 'same.mp4', 'downloaded_bytes': count})
    assert budget.snapshot() == 190


def test_explicit_section_quality_reaches_video_selection(monkeypatch, tmp_path):
    formats = [
        {'format_id':str(height), 'height':height, 'width':height*16/9, 'vcodec':'h264', 'acodec':'none',
         'protocol':'http_dash_segments', 'fragments':[{'url':'test','duration':10}], 'ext':'mp4'}
        for height in (360,720,1080)
    ] + [{'format_id':'audio','vcodec':'none','acodec':'aac','ext':'m4a',
           'protocol':'http_dash_segments','fragments':[{'url':'test','duration':10}]}]
    monkeypatch.setattr(proxy, 'source_info', lambda *a: {'formats':formats,'duration':10})
    monkeypatch.setattr(proxy, 'source_playlist', lambda fmt,*a: fmt)
    acquired = []
    def acquire(options,url,video_id,fmt,stem,fragments=None):
        acquired.append(fmt['format_id'])
        stem.write_bytes(b'media')
        return stem
    monkeypatch.setattr(proxy, 'native_download_format', acquire)
    monkeypatch.setattr(proxy, '_mux_local_window', lambda v,a,d,**kw: d.write_bytes(b'muxed'))
    proxy.download_section_file('https://youtu.be/abcdefghijk','abcdefghijk',0,10,tmp_path/'section.mp4',max_height=720)
    assert acquired == ['720','audio']


def test_adaptive_full_only_uses_cheaper_exact_matching_quality():
    info = {'duration':3600,'formats':[
        {'format_id':'1080','height':1080,'vcodec':'h264','acodec':'none','fps':30,'filesize':100,'protocol':'https'},
        {'format_id':'720','height':720,'vcodec':'h264','acodec':'none','fps':30,'filesize':1000,'protocol':'https'},
        {'format_id':'range','height':720,'vcodec':'h264','acodec':'none','fps':30,'tbr':1600,'protocol':'m3u8_native'},
        {'format_id':'original','vcodec':'none','acodec':'aac','format_note':'original','ext':'m4a','abr':128,'filesize':100,'protocol':'https'},
    ]}
    chosen = proxy.pick_adaptive_full_formats(info,[(0,1800)],max_height=720)
    assert chosen and [fmt['format_id'] for fmt in chosen] == ['720','original']
    info['formats'][1]['filesize'] = 10**10
    assert proxy.pick_adaptive_full_formats(info,[(0,1800)],max_height=720) is None
    info['formats'][1].pop('filesize')
    assert proxy.pick_adaptive_full_formats(info,[(0,1800)],max_height=720) is None


def test_section_quality_prefers_720_over_a_lower_mp4():
    chosen = proxy.pick_video_format([
        {'format_id':'low','vcodec':'h264','height':360,'ext':'mp4','protocol':'m3u8_native'},
        {'format_id':'720','vcodec':'vp9','height':720,'ext':'webm','protocol':'m3u8_native'},
    ],720)
    assert chosen['format_id'] == '720'


def test_720_acquisition_prefers_30fps_but_keeps_resolution_when_only_60_exists():
    formats = [
        {'format_id':'720-60','vcodec':'h264','height':720,'fps':60,'protocol':'m3u8_native'},
        {'format_id':'720-30','vcodec':'h264','height':720,'fps':30,'protocol':'m3u8_native'},
        {'format_id':'360-30','vcodec':'h264','height':360,'fps':30,'protocol':'m3u8_native'},
    ]
    assert proxy.pick_video_format(formats,720)['format_id'] == '720-30'
    assert proxy.pick_video_format([formats[0],formats[2]],720)['format_id'] == '720-60'


def test_720_hls_60fps_beats_unsliceable_720_https_30fps():
    formats = [
        {'format_id':'720-https-30','vcodec':'h264','height':720,'fps':30,'protocol':'https',
         'fragments':[{'url':'byte-range-without-time'}]},
        {'format_id':'720-hls-60','vcodec':'h264','height':720,'fps':60,'protocol':'m3u8_native'},
        {'format_id':'360-hls-30','vcodec':'h264','height':360,'fps':30,'protocol':'m3u8_native'},
    ]
    assert proxy.pick_video_format(formats,720)['format_id'] == '720-hls-60'


def test_native_request_respects_the_shorter_configured_socket_timeout(monkeypatch):
    import time
    import yt_dlp
    from yt_dlp.networking import Request
    timeouts = []
    class Client:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def urlopen(self, request):
            timeouts.append(request.extensions['timeout'])
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', Client)
    with proxy._proxy_client({'_youtube_deadline':time.monotonic()+60,'socket_timeout':6}) as client:
        client.urlopen(Request('https://example.test/media'))
    assert timeouts == [6]


def test_configured_720_ceiling_also_caps_an_explicit_1080_profile():
    config = Config()
    config.youtube_max_height = 720
    set_config_override(config)
    assert proxy.capped_height(1080) == 720
    assert source.requested_height('1080') == 720


def test_adaptive_full_rejects_progressive_audio_in_the_wrong_language():
    info = {'duration':3600,'language':'ja','formats':[
        {'format_id':'720','height':720,'vcodec':'h264','acodec':'none','filesize':1000,'protocol':'https'},
        {'format_id':'range','height':720,'vcodec':'h264','acodec':'none','tbr':1600,'protocol':'m3u8_native'},
        {'format_id':'original','vcodec':'none','acodec':'aac','language':'ja','format_note':'original','abr':128,'protocol':'m3u8_native'},
        {'format_id':'english','vcodec':'none','acodec':'aac','language':'en','ext':'m4a','abr':128,'filesize':100,'protocol':'https'},
    ]}
    assert proxy.pick_adaptive_full_formats(info,[(0,1800)],max_height=720) is None


def test_direct_audio_cache_populates_without_proxy(monkeypatch):
    def acquire(url, video_id, stem, **kwargs):
        result = Path(str(stem) + ".m4a")
        result.write_bytes(b"audio")
        return result
    monkeypatch.setattr(source, "download_audio", acquire)
    monkeypatch.setattr(source, "_restore_remote_cache", lambda *a: None)
    monkeypatch.setattr(source, "_persist_named", lambda *a: None)
    monkeypatch.setattr(source, "_persist_manifest", lambda *a: None)
    path = source.download_audio_for_job("https://youtu.be/abcdefghijk", "abcdefghijk")
    assert path.read_bytes() == b"audio"


def test_direct_extraction_is_shared_within_job(monkeypatch):
    calls = []
    monkeypatch.setattr(proxy, "extract_video_info", lambda url, opts: calls.append(url) or {"formats": [], "id": "abcdefghijk"})
    budget = proxy.ByteBudget(1000)
    first = proxy.source_info("https://youtu.be/abcdefghijk", {"proxy": ""}, budget)
    second = proxy.source_info("https://youtu.be/abcdefghijk", {"proxy": ""}, budget)
    assert first == second
    assert len(calls) == 1


def test_materialize_retains_compact_full_source_with_original_timestamps(monkeypatch):
    monkeypatch.setattr(source, '_storage', lambda: None)
    monkeypatch.setattr(source, '_persist_named', lambda *a: None)
    monkeypatch.setattr(source, '_persist_manifest', lambda *a: None)
    monkeypatch.setattr(source, 'download_section_file', lambda *a, **kw: pytest.fail('Cheaper full source must replace window transfers'))
    acquired = []
    def compact(url, video_id, ranges, dest, **kwargs):
        acquired.append(kwargs['max_height'])
        dest.write_bytes(b'compact full audiovisual source')
        return dest
    monkeypatch.setattr(source, 'download_adaptive_full', compact)
    url = 'https://youtu.be/abcdefghijk'
    budget = proxy.ByteBudget(10000)
    budget.metadata[url] = (0, {})
    result = source.materialize_clip_source(url, 'abcdefghijk', [{'start_time':100,'end_time':120}],
                                           budget=budget, duration=3600, quality='720')
    assert acquired == [720]
    assert result.name == 'full-720.mp4'
    assert not source.timeline_path(result).exists()
    assert source.cached_full_media('abcdefghijk', '720') == result


def test_adaptive_pair_counts_transfers_once_without_charging_the_mux(monkeypatch, tmp_path):
    formats = ({'format_id':'720','filesize':1000}, {'format_id':'original','filesize':100})
    monkeypatch.setattr(proxy, 'source_info', lambda *a: {'duration':3600})
    monkeypatch.setattr(proxy, 'pick_adaptive_full_formats', lambda *a, **kw: formats)
    def acquire(options,url,video_id,fmt,stem,fragments=None):
        stem.write_bytes(b'a' * fmt['filesize'])
        options['progress_hooks'][0]({'status':'downloading','filename':str(stem),'downloaded_bytes':fmt['filesize']})
        return stem
    monkeypatch.setattr(proxy, 'native_download_format', acquire)
    monkeypatch.setattr(proxy, '_mux_local_window', lambda v,a,d,**kw: d.write_bytes(v.read_bytes()+a.read_bytes()))
    budget = proxy.ByteBudget(1200)
    result = proxy.download_adaptive_full('url','abcdefghijk',[(0,1800)],tmp_path/'full.mp4',budget=budget,max_height=720)
    assert result.read_bytes() == b'a' * 1100
    assert budget.snapshot() == 1100


def test_adaptive_audio_failure_happens_before_full_video_transfer(monkeypatch, tmp_path):
    formats = ({'format_id':'720','filesize':1000}, {'format_id':'original','filesize':100})
    monkeypatch.setattr(proxy, 'source_info', lambda *a: {'duration':3600})
    monkeypatch.setattr(proxy, 'pick_adaptive_full_formats', lambda *a, **kw: formats)
    acquired = []
    def acquire(options,url,video_id,fmt,stem,fragments=None):
        acquired.append(fmt['format_id'])
        if fmt['format_id'] == 'original':
            raise proxy.YouTubeDownloadError('Audio forbidden')
        stem.write_bytes(b'v' * 1000)
        return stem
    monkeypatch.setattr(proxy, 'native_download_format', acquire)
    with pytest.raises(proxy.YouTubeDownloadError, match='Audio forbidden'):
        proxy.download_adaptive_full('url','abcdefghijk',[(0,1800)],tmp_path/'full.mp4',budget=proxy.ByteBudget(2000),max_height=720)
    assert acquired == ['original']


def test_original_caption_sidecar_is_used_without_cached_audio(tmp_path):
    directory = source.cache_dir_for('abcdefghijk')
    directory.mkdir(parents=True)
    captions = directory / 'captions.json'
    captions.write_text('{"events":[]}')
    captions.with_suffix('.transcript_cache.json').write_text('{"words":[]}')
    render = tmp_path / source.WORK_DIRNAME / 'job' / 'render.mp4'
    assert source.transcript_source_for('abcdefghijk', render) == captions


def test_editor_range_reacquisition_maps_caption_words_without_audio(monkeypatch, tmp_path):
    import json
    directory = source.cache_dir_for('abcdefghijk')
    directory.mkdir(parents=True)
    piece = directory / 'range.mp4'
    piece.write_bytes(b'clean source footage')
    manifest = source._empty_manifest('abcdefghijk')
    manifest['ranges'] = [{'file':piece.name,'start':90,'end':130}]
    source._save_manifest(directory, manifest)
    captions = directory / 'captions.json'
    captions.write_text('{"events":[]}')
    captions.with_suffix('.transcript_cache.json').write_text(json.dumps({'words':[
        {'text':'Original','start':100000,'end':100500}]}))
    monkeypatch.setattr(source, '_storage', lambda: None)
    def assemble(slices, dest):
        dest.write_bytes(b'clean render footage')
        return source.build_time_map([(90,130)])
    monkeypatch.setattr(source, 'assemble_render_source', assemble)
    result = source.materialize_clip_source('url','abcdefghijk',[{'start_time':100,'end_time':120}],
                                           task_id='editor',duration=3600,quality='720')
    words = json.loads(result.with_suffix('.transcript_cache.json').read_text())['words']
    assert words == [{'text':'Original','start':10000,'end':10500}]


def test_clean_fragments_are_remuxed_without_encoding_handles(monkeypatch, tmp_path):
    pieces = [source.CachedPiece(path=tmp_path / 'first.mp4', start=0, end=10), source.CachedPiece(path=tmp_path / 'second.mp4', start=30, end=40)]
    for piece in pieces:
        piece.path.write_bytes(b'video')
    monkeypatch.setattr(source, '_compatible_fragment_sources', lambda pieces: True)
    monkeypatch.setattr(source, '_trim_local', lambda *a: pytest.fail('Clean handles must not be encoded again'))
    captured = []
    monkeypatch.setattr(source, '_concat_local', lambda files, dest, **kw: captured.extend(files))
    mapping = source.assemble_render_source([(pieces[0], 2, 8), (pieces[1], 32, 38)], tmp_path/'render.mp4')
    assert captured == [p.path for p in pieces]
    assert mapping.to_local(33) == pytest.approx(13)


def test_different_audio_codecs_cannot_use_concat_demuxer(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import json
    pieces = [source.CachedPiece(0, 10, tmp_path/'a.mp4'), source.CachedPiece(20, 30, tmp_path/'b.mp4')]
    def probe(command, **kwargs):
        audio = 'aac' if command[-1].endswith('a.mp4') else 'opus'
        return SimpleNamespace(stdout=json.dumps({'format': {'duration':10}, 'streams': [
            {'codec_type':'video','codec_name':'h264','width':1920,'height':1080},
            {'codec_type':'audio','codec_name':audio,'sample_rate':'48000','channels':2},
        ]}))
    monkeypatch.setattr(source.subprocess, 'run', probe)
    assert not source._compatible_fragment_sources(pieces)
