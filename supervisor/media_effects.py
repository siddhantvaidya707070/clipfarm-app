"""Opt-in local effects, licensed music beds and explicitly labelled narration."""
import hashlib
import importlib.util
import json
import math
import subprocess
import wave
from pathlib import Path
from .ingest import DATA, probe
from .openai_gateway import read_secret
from .narration import synthesize

EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}
DISCLOSURE = 'AI narration: a generic synthetic voice reads the original clip transcript; this is not the creator’s voice.'


def music_file(name):
    if not isinstance(name, str) or not name or name.startswith('.') or '/' in name or '\\' in name or ':' in name or Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError('Music must be a safe local audio filename')
    root = (DATA / 'music').resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError('Music file is missing or outside DATA/music')
    return path


def normalize_effects(options, campaign=''):
    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - {'preset', 'music', 'music_rights_confirmed', 'narration'}:
        raise ValueError('Unknown media effects option')
    result = {'preset': options.get('preset', 'clean'), 'music': options.get('music', ''),
              'music_rights_confirmed': options.get('music_rights_confirmed', False), 'narration': options.get('narration', False)}
    if result['preset'] not in ('clean', 'punchy') or any(type(result[k]) is not bool for k in ('music_rights_confirmed', 'narration')):
        raise ValueError('Invalid effects preset or boolean option')
    if not isinstance(result['music'], str):
        raise ValueError('Music must be a local filename')
    if result['music']:
        if not result['music_rights_confirmed']:
            raise ValueError('Confirm music usage rights before adding a music bed')
        music_file(result['music'])
    if result['narration'] and str(campaign).lower() == 'tjr':
        raise ValueError('TJR narration is disabled pending explicit AI editing clarification')
    return result


def media_options():
    music = []
    folder = DATA / 'music'
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            try:
                music_file(path.name)
                music.append({'name': path.name})
            except ValueError:
                pass
    available = bool(read_secret('openai_key')) and importlib.util.find_spec('websockets') is not None
    return {'music': music, 'narration_available': available,
            'narration_note': 'Optional generic AI voice reads the actual transcript; requires account access to gpt-realtime-2.1-mini. Shared $1/day and $10/month limits. TJR disabled pending clarification.'}


def render_command(source, output, duration, options, music_path=None, narration_path=None, narration_duration=None):
    command = ['ffmpeg', '-y', '-v', 'error', '-i', str(source)]
    filters = []
    speech = '0:a'
    next_input = 1
    if narration_path:
        if not narration_duration or not math.isfinite(narration_duration):
            raise ValueError('Invalid narration duration')
        speed = max(1., narration_duration / max(.1, duration - .1))
        if speed > 1.25:
            raise ValueError('Narration cannot fit this clip within the 1.25x speed limit; choose a longer clip')
        command += ['-i', str(narration_path)]
        filters += [f'[{next_input}:a]atempo={speed:.8f},apad[narrator]', '[0:a]volume=-32dB[originalbed]',
                    '[narrator][originalbed]amix=inputs=2:duration=first:normalize=0[speech]']
        speech = 'speech'
        next_input += 1
    if music_path:
        command += ['-stream_loop', '-1', '-i', str(music_path)]
        filters += [f'[{speech}]asplit=2[dialogue][duckkey]', f'[{next_input}:a]volume=-22dB[musicbed]',
                    '[musicbed][duckkey]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=300[ducked]',
                    '[dialogue][ducked]amix=inputs=2:duration=first:normalize=0[mix]']
        speech = 'mix'
    if narration_path:
        # Persistent burned-in label survives metadata removal and reposting.
        filters += ["[0:v]drawtext=text='AI NARRATION':fontsize=38:fontcolor=white:box=1:boxcolor=black@0.75:x=24:y=24[labelled]"]
        video = 'labelled'
    else:
        video = '0:v'
    if options['preset'] == 'punchy':
        filters += [f'[{video}]eq=contrast=1.04:saturation=1.06,unsharp=5:5:0.25[punchy]']
        video = 'punchy'
    if filters:
        command += ['-filter_complex', ';'.join(filters)]
    command += ['-map', f'[{video}]' if video != '0:v' else '0:v:0', '-map', f'[{speech}]' if speech != '0:a' else '0:a:0',
                '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-c:a', 'aac', '-movflags', '+faststart', str(output)]
    return command


def apply_effects(store, artifact, options, campaign=''):
    options = normalize_effects(options, campaign)
    if options['preset'] == 'clean' and not options['music'] and not options['narration']:
        return artifact
    source = Path(artifact['path']).resolve()
    info = artifact.get('probe') or probe(source)
    duration = float(info['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid clip duration')
    music = music_file(options['music']) if options['music'] else None
    fingerprint = {'source': str(source), 'size': source.stat().st_size, 'mtime': source.stat().st_mtime_ns,
                   'options': options, 'transcript': artifact.get('transcript', ''), 'version': 1}
    if music:
        fingerprint['music_stat'] = [music.stat().st_size, music.stat().st_mtime_ns]
    ident = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:24]
    output = source.with_name(source.stem + '-fx-' + ident + '.mp4')
    if not output.exists():
        narration_path, narration_duration = None, None
        if options['narration']:
            narration_path = synthesize(store, artifact.get('transcript', ''), source.parent)
            with wave.open(narration_path, 'rb') as audio:
                narration_duration = audio.getnframes() / audio.getframerate()
        temporary = output.with_suffix('.partial.mp4')
        subprocess.run(render_command(source, temporary, duration, options, music, narration_path, narration_duration),
                       check=True, capture_output=True, timeout=1800)
        probe(temporary)
        temporary.replace(output)
    result = {**artifact, 'path': str(output), 'filename': output.name, 'probe': probe(output), 'effects': options}
    if options['narration']:
        result.update(ai_narration=True, narration_disclosure=DISCLOSURE,
                      title='AI narration | ' + artifact.get('title', 'Stream highlight'),
                      description=(artifact.get('description', '') + '\n\n' + DISCLOSURE).strip())
    return result
