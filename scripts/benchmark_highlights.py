"""Run real transcription/scoring/caption exports against a cached hour of media.

Run inside the supervisor image with its normal environment. This is a processing
benchmark, not a YouTube acquisition/chat replay test. No API responses or secrets
are printed. Full uploads cost the configured provider's normal transcription rate.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervisor.highlights import HighlightService, ground_clip_type
from supervisor.highlight_media import atomic_json, run
from supervisor.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, help='Already acquired local video, never a URL')
    parser.add_argument('--seconds', type=int, default=3600)
    parser.add_argument('--root', default='/data/highlight-benchmark')
    parser.add_argument('--height', type=int, choices=(720, 1080), default=720)
    options = parser.parse_args()
    source = Path(options.source).resolve()
    if not source.is_file() or not 15 <= options.seconds <= 10800:
        raise SystemExit('Choose a local video and 15 to 10800 seconds')
    root = Path(options.root)
    root.mkdir(parents=True, exist_ok=True)
    store = Store('sqlite:///' + str((root / 'benchmark.db').resolve()))
    service = HighlightService(store, root)
    cache = service.root / 'cache' / 'BENCHMARK01'
    cache.mkdir(parents=True, exist_ok=True)
    video = cache / 'source.mp4'
    if not video.exists():
        print('Preparing cached test interval (excluded from processing timing)', flush=True)
        run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-t', str(options.seconds),
             '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-movflags', '+faststart', str(video)], timeout=300)
    actual = float(json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                   '-of', 'json', str(video)]))['format']['duration'])
    if abs(actual - options.seconds) > 2:
        raise SystemExit('Cached benchmark interval differs from requested duration')
    atomic_json(cache / 'info.json', {'id': 'BENCHMARK01', 'title': 'Cached source: first-hour processing benchmark',
                                    'duration': actual, 'is_live': False})
    atomic_json(cache / 'chat.json', [])
    row = service.create('benchmark', {'source': 'https://youtu.be/BENCHMARK01',
                                      'output_format': 'original', 'burn_captions': True, 'export_height': options.height}, schedule=False)
    service.stops[row['id']] = threading.Event()
    print('Running real gpt-transcribe, peak detection, metadata and burned-caption exports', flush=True)
    started = time.perf_counter()
    try:
        service._process(row['id'])
        row = service.get('benchmark', row['id'])
        for clip in row['clips']:
            ground_clip_type(clip)
        row = service.update(row['id'], clips=row['clips'])
        checks = []
        for clip in row['clips']:
            path = service.folder(row['id']) / (clip['id'] + '.mp4')
            valid = False
            error = ''
            if path.exists():
                try:
                    result = json.loads(run(['ffprobe', '-v', 'error', '-show_entries',
                                             'format=duration:stream=codec_type', '-of', 'json', str(path)]))
                    duration = float(result['format']['duration'])
                    valid = {'video', 'audio'}.issubset({s['codec_type'] for s in result['streams']}) and abs(duration - (clip['end'] - clip['start'])) < .25
                except Exception as exc:
                    error = type(exc).__name__
            srt = path.with_suffix('.srt')
            checks.append({'id': clip['id'], 'mp4_valid': valid, 'srt_exists': srt.exists(),
                           'caption_count': len(clip.get('captions', [])), 'error': error})
        report = {'source': str(source), 'test_seconds': actual, 'model': os.getenv('OPENAI_TRANSCRIPTION_MODEL', 'gpt-transcribe'),
                  'state': row['state'], 'message': row['message'], 'timings': row['timings'],
                  'clips': len(row['clips']), 'export_height': options.height, 'types': dict(Counter(c['type'] for c in row['clips'])),
                  'all_mp4_valid': bool(checks) and all(c['mp4_valid'] for c in checks),
                  'all_srt_present': bool(checks) and all(c['srt_exists'] for c in checks),
                  'metadata_generated': sum(bool(c.get('metadata_generated')) for c in row['clips']),
                  'timing_quality': row.get('timing_quality'), 'validation': checks,
                  'manifest': str(service.folder(row['id']) / 'manifest.json'),
                  'notes': ['Uses real, previously downloaded source media; source acquisition excluded.',
                            'Chat deliberately unavailable: exercises audio/transcript fallback.',
                            'GPT-Transcribe word timings are estimates; caption editing remains necessary.']}
        atomic_json(root / 'timing-report.json', report)
        print(json.dumps({k: v for k, v in report.items() if k != 'validation'}, indent=2), flush=True)
        if row['state'] != 'completed' or not report['all_mp4_valid']:
            raise SystemExit(1)
    finally:
        service.close()


if __name__ == '__main__':
    main()
