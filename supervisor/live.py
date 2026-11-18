"""Capture a single supported stream and enqueue only closed segments."""
import os
import math
import subprocess
import time
import uuid
from pathlib import Path
from .ingest import DATA, public_source, enough_space, probe


def closed_segments(folder, running):
    files = sorted(f for f in Path(folder).glob('*.ts') if f.stem.isdigit())
    return files[:-1] if running else files


class Recorder:
    def __init__(self):
        self.source = self.ffmpeg = None
        self.folder = None
        self.creator = None
        self.done = set()
        self.offset = 0.0
        self.previous = None
        self.session = None

    def start(self, creator, max_height=None, segment_seconds=300):
        if not public_source(creator['live_url']) or not enough_space():
            raise ValueError('Invalid live source or low disk space')
        self.folder = DATA / 'inbox' / 'live' / uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.creator = creator
        self.started = time.time()
        self.log = (self.folder / 'capture.log').open('ab')
        # An explicit selector list never falls back to unrestricted 1080p/4K.
        quality = 'best' if max_height is None else ','.join(
            name for height in (720, 480, 360, 240, 144) if height <= max_height
            for name in (f'{height}p60', f'{height}p'))
        self.source = subprocess.Popen(['streamlink', '--retry-streams', '15', '--retry-max', '2', '--stdout', creator['live_url'], quality], stdout=subprocess.PIPE, stderr=self.log)
        self.ffmpeg = subprocess.Popen(['ffmpeg', '-v', 'warning', '-i', 'pipe:0', '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-f', 'segment', '-segment_time', str(segment_seconds), '-reset_timestamps', '1', str(self.folder / '%05d.ts')], stdin=self.source.stdout, stdout=subprocess.DEVNULL, stderr=self.log)
        self.source.stdout.close()

    def captured_seconds(self):
        """Measured media timeline, excluding launch and stream resolution delays."""
        duration = self.offset
        if not self.folder:
            return duration
        for file in Path(self.folder).glob('*.ts'):
            if not file.stem.isdigit() or file.name in self.done:
                continue
            try:
                seconds = float(probe(file)['duration'])
                if math.isfinite(seconds) and seconds > 0:
                    duration += seconds
            except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                # The current segment may not yet contain enough packets to probe.
                continue
        return duration

    def drain(self, store):
        if not self.folder:
            return
        closed = closed_segments(self.folder, bool(self.ffmpeg and self.ffmpeg.poll() is None))
        for file in closed:
            if file.name in self.done or file.name == 'tail.ts':
                continue
            duration = probe(file)['duration']
            output = file.with_suffix('.mp4')
            source_offset = self.offset
            if self.previous:
                tail = self.folder / 'tail.ts'
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-sseof', '-60', '-i', str(self.previous), '-c', 'copy', str(tail)], check=True, timeout=60)
                source_offset = max(0, self.offset - probe(tail)['duration'])
                media = 'concat:' + str(tail) + '|' + str(file)
            else:
                media = str(file)
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-fflags', '+genpts', '-i', media, '-c', 'copy', '-movflags', '+faststart', str(output)], check=True, timeout=120)
            start, end = source_offset, None
            if self.session:
                start = max(source_offset, self.session['start'])
                end = min(self.offset + duration, self.session['end']) if self.session['end'] is not None else self.offset + duration
            if end is None or end > start:
                extra = {'source_offset': start, 'media_source_offset': source_offset,
                         'captured_at': self.started, 'original_source': self.creator['live_url']}
                if end is not None:
                    extra['source_end'] = end
                if self.session and self.session.get('render_options'):
                    extra['render_options'] = self.session['render_options']
                store.queue(self.creator['id'], str(output), **extra)
            self.done.add(file.name)
            self.previous = file
            self.offset += duration

    def stop(self):
        for process in (self.source, self.ffmpeg):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        if hasattr(self, 'log'):
            self.log.close()
        self.source = self.ffmpeg = None


def recover_segments(store):
    # Restarted recordings are never assumed complete. Surface their files for review.
    root = DATA / 'inbox' / 'live'
    for folder in root.glob('*') if root.exists() else []:
        marker = folder / 'interrupted-noted'
        if folder.is_dir() and not marker.exists() and list(folder.glob('*.ts')):
            store.notify(f'Interrupted live capture recovered at {folder}. Closed MP4 segments remain queued; inspect the final TS segment before importing.')
            marker.touch()


class LiveController:
    """A scheduler-owned single recorder; API writes requests only."""
    def __init__(self, store):
        self.store = store
        self.recorder = Recorder()
        self.session_id = None

    def _update(self, **fields):
        if self.session_id:
            # Preserve concurrently requested stop flags under the write guard.
            with self.store.transaction() as connection:
                row = self.store._get(connection, 'live_session', self.session_id)
                self.store._put(connection, 'live_session', self.session_id, {**row, **fields})

    def tick(self):
        store, recorder = self.store, self.recorder
        try:
            if recorder.ffmpeg:
                session = store.get('live_session', self.session_id) if self.session_id else None
                creator = store.get('creator', recorder.creator['id'])
                captured = recorder.captured_seconds()
                stop = bool(session and (session.get('stop_requested') or
                            session.get('end') is not None and captured >= session['end']))
                if not session and (not creator.get('enabled') or creator.get('live_url') != recorder.creator.get('live_url')):
                    stop = True
                ended = recorder.ffmpeg.poll() is not None
                if stop or ended or not enough_space():
                    recorder.stop()
                    recorder.drain(store)
                    no_footage = recorder.offset <= 0
                    self._update(state='failed' if no_footage else ('stopped' if stop else 'completed'),
                                 recorded_seconds=recorder.offset,
                                 message='No footage captured; stream ended or recording stopped before media arrived. Retry the live source or use a completed replay.' if no_footage else
                                         'Recording closed; captured segments queued for clipping')
                    store.put('live_retry', creator['id'], {'next': time.time() + 900})
                    self.recorder = Recorder()
                    self.session_id = None
                else:
                    recorder.drain(store)
                    self._update(recorded_seconds=captured, elapsed_seconds=time.time() - recorder.started)
                return
            sessions = sorted((s for s in store.list('live_session') if s['state'] == 'queued'), key=lambda s: s['created'])
            if sessions:
                session = sessions[0]
                # Claim while holding the same transaction guard used by stop requests.
                with store.transaction() as connection:
                    latest = store._get(connection, 'live_session', session['id'])
                    if latest['state'] != 'queued' or latest.get('stop_requested'):
                        return
                    store._put(connection, 'live_session', session['id'], {**latest, 'state': 'recording'})
                self.session_id = session['id']
                recorder.session = session
                creator = {**store.get('creator', session['creator']), 'live_url': session['source']}
                recorder.start(creator)
                self._update(started_at=recorder.started, folder=str(recorder.folder), message='Recording now; selection uses seconds since recording start')
                return
            creators = [c for c in store.list('creator') if c.get('enabled') and c.get('live_url') and
                        (store.get('live_retry', c['id']) or {}).get('next', 0) < time.time()]
            if creators and enough_space():
                recorder.start(creators[0])
        except Exception as exc:
            no_footage = recorder.offset <= 0 and recorder.captured_seconds() <= 0
            self._update(state='failed', message=('No footage captured. ' if no_footage else 'Capture failed: ') + str(exc)[:300])
            if recorder.creator:
                store.put('live_retry', recorder.creator['id'], {'next': time.time() + 900})
            recorder.stop()
            self.recorder = Recorder()
            self.session_id = None
