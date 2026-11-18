"""Timed public-caption fast path: no audio/video stand-ins."""
import json
from pathlib import Path
import pytest


def test_json3_preserves_provider_starts_deduplicates_and_reports_inferred_ends():
    from src.services.fast_captions import parse_json3_transcript
    raw = {'events': [
        {'tStartMs': 1000, 'dDurationMs': 3000, 'segs': [{'utf8': 'Hello'}, {'utf8': ' world!', 'tOffsetMs': 500}]},
        {'tStartMs': 1500, 'dDurationMs': 2500, 'segs': [{'utf8': 'world!'}]},
        {'tStartMs': 3000, 'dDurationMs': 1000, 'segs': [{'utf8': 'Again.'}]}]}
    data = parse_json3_transcript(raw, duration=5, language='en')
    assert [(w['text'], w['start'], w['end']) for w in data['words']] == [
        ('Hello', 1000, 1500), ('world!', 1500, 3000), ('Again.', 3000, 4000)]
    assert data['provider'] == 'youtube_captions'
    assert data['timing_quality'] == 'provider_word_starts_inferred_ends'
    assert data['version'] == 2


def test_original_english_caption_choice_does_not_use_translated_english():
    from src.services.fast_captions import choose_original_caption
    caption = {'ext': 'json3', 'url': 'https://www.youtube.com/api/timedtext?lang=en'}
    assert choose_original_caption({'language': 'en', 'automatic_captions': {'en-orig': [caption]}}) == ('en', caption, True)
    assert choose_original_caption({'language': 'de', 'automatic_captions': {'en': [caption]}}) is None
    assert choose_original_caption({'automatic_captions': {'en': [caption]}}) is None


def test_json3_phrase_timing_is_labelled_without_inventing_word_offsets():
    from src.services.fast_captions import parse_json3_transcript
    data = parse_json3_transcript({'events': [{'tStartMs': 1200, 'dDurationMs': 2000,
        'segs': [{'utf8': 'Several words in one cue.'}]}]}, duration=5, language='en')
    assert len(data['words']) == 1
    assert data['words'][0]['text'] == 'Several words in one cue.'
    assert data['words'][0]['start'] == 1200
    assert data['timing_quality'] == 'provider_phrase_timing'


@pytest.mark.asyncio
@pytest.mark.parametrize('captions_available', [True, False])
async def test_caption_first_pipeline_skips_audio_when_available_and_retains_transcription_fallback(monkeypatch, tmp_path, captions_available):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.services import fast_captions
    from src.services import fast_ai_selection
    from src.services import video_service as module
    from src.services.video_service import VideoService
    config = SimpleNamespace(max_video_duration=10800, max_clips=20, fast_mode_max_clips=20,
        fast_highlights_enabled=True, fast_prefer_youtube_captions=True, fast_ai_selection_enabled=True,
        youtube_download_max_bytes=2000000000)
    monkeypatch.setattr(module, 'get_config', lambda: config)
    async def selected(*args, **kwargs):
        return {'segments': [{'start_time': '00:00', 'end_time': '00:24', 'source_ranges': [(0, 24)],
                             'text': 'grounded', 'fast_highlight': True, 'selection_provider': 'ai'}],
                'selection_provider': 'ai', 'model': 'test', 'seconds': .01}
    monkeypatch.setattr(fast_ai_selection, 'select_ai_highlights', selected)
    data = {'version': 2, 'provider': 'youtube_captions' if captions_available else 'openai', 'timing_quality': 'provider_word_starts_inferred_ends',
            'words': [{'text': 'grounded', 'start': i * 500, 'end': i * 500 + 350} for i in range(7200)]}
    caption_path = tmp_path / 'captions.json'
    caption_path.write_text('{"events": []}')
    caption_path.with_suffix('.transcript_cache.json').write_text(json.dumps(data))
    video = tmp_path / 'render.mp4'
    video.write_bytes(b'real video placeholder')
    video.with_suffix('.transcript_cache.json').write_text(json.dumps(data))
    monkeypatch.setattr(fast_captions, 'fetch_fast_youtube_captions', lambda *a, **kw: (caption_path, data, 'Grounded caption transcript') if captions_available else None)
    monkeypatch.setattr(module, 'async_get_youtube_video_info', AsyncMock(return_value={'duration': 3600}))
    monkeypatch.setattr(module, 'cached_full_media', lambda *a: None)
    monkeypatch.setattr(module, 'download_analysis_source', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('No full audio needed')) if captions_available else (video, True))
    monkeypatch.setattr(VideoService, '_get_file_duration', lambda *a: (_ for _ in ()).throw(AssertionError('Never ffprobe a caption JSON')) if captions_available else 3600)
    monkeypatch.setattr(VideoService, 'generate_transcript', AsyncMock(
        side_effect=AssertionError('No paid transcription needed') if captions_available else None,
        return_value='Grounded paid fallback transcript'))
    monkeypatch.setattr(VideoService, 'prepare_source', AsyncMock(return_value=video))
    monkeypatch.setattr(module, 'MediaStorage', lambda: SimpleNamespace(save_source_metadata=lambda *a: None))
    monkeypatch.setattr(module, 'persist_cache_sidecar', lambda *a: None)
    received = {}
    def materialize(*args, **kwargs):
        received.update(kwargs)
        return video
    monkeypatch.setattr(module, 'materialize_clip_source', materialize)
    saved = {}
    async def checkpoint(patch):
        saved.update(patch)
    result = await VideoService.process_video_complete('https://youtube.com/watch?v=abcdefghijk', 'youtube',
        processing_mode='fast', checkpoint_callback=checkpoint)
    assert len(result['segments_to_render']) == 1
    assert received['audio_path'] == (caption_path if captions_available else video)
    assert received['duration'] == 3600
    assert saved['transcript_data']['provider'] == ('youtube_captions' if captions_available else 'openai')
    assert result['transcription_provider'] == ('youtube_captions' if captions_available else None)
    assert result['timing_quality'] == ('provider_word_starts_inferred_ends' if captions_available else None)
    assert VideoService.prepare_source.await_count == (1 if captions_available else 2)
    assert VideoService.generate_transcript.await_count == (0 if captions_available else 1)
