import hashlib
import json
import threading
import uuid
from urllib.parse import urlparse, parse_qs
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, Text, JSON, select, update, insert


def now():
    return datetime.now(timezone.utc).isoformat()


def render_variant(options):
    values = {'preset': 'clean', 'music': '', 'narration': False, **(options or {})}
    return json.dumps({k: values[k] for k in ('preset', 'music', 'narration')}, sort_keys=True)


def source_identity(source):
    parsed = urlparse(source)
    host = (parsed.hostname or '').lower().removeprefix('www.')
    if host == 'youtu.be':
        return 'youtube:' + parsed.path.strip('/').split('/')[0]
    if host in ('youtube.com', 'm.youtube.com'):
        ident = parse_qs(parsed.query).get('v', [''])[0]
        if not ident and parsed.path.startswith(('/shorts/', '/live/', '/embed/')):
            ident = parsed.path.strip('/').split('/')[1]
        if ident:
            return 'youtube:' + ident
    if host.endswith('twitch.tv') and '/videos/' in parsed.path:
        return 'twitch:video:' + parsed.path.rsplit('/', 1)[-1]
    return source.split('#', 1)[0]


class Store:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True, **({'connect_args': {'check_same_thread': False, 'timeout': 30}} if url.startswith('sqlite') else {}))
        self.lock = threading.RLock()
        m = MetaData()
        self.records = Table('records', m, Column('kind', String, primary_key=True), Column('id', String, primary_key=True), Column('data', JSON, nullable=False))
        self.budget = Table('budget', m, Column('id', String, primary_key=True), Column('created', String), Column('reserved', Integer), Column('actual', Integer))
        self.guard = Table('transaction_guard', m, Column('id', Integer, primary_key=True))
        m.create_all(self.engine)
        with self.engine.begin() as c:
            if c.execute(select(self.guard)).first() is None:
                c.execute(insert(self.guard).values(id=1))
        self.seed()

    @contextmanager
    def transaction(self):
        with self.lock, self.engine.begin() as c:
            # Serializes all write decisions across API and scheduler processes.
            c.execute(update(self.guard).where(self.guard.c.id == 1).values(id=1))
            yield c

    def _get(self, c, kind, ident):
        row = c.execute(select(self.records.c.data).where(self.records.c.kind == kind, self.records.c.id == ident)).first()
        return dict(row[0]) if row else None

    def get(self, kind, ident):
        with self.engine.connect() as c:
            return self._get(c, kind, ident)

    def _put(self, c, kind, ident, data):
        if self._get(c, kind, ident) is None:
            c.execute(insert(self.records).values(kind=kind, id=ident, data=data))
        else:
            c.execute(update(self.records).where(self.records.c.kind == kind, self.records.c.id == ident).values(data=data))
        return data

    def put(self, kind, ident, data):
        with self.transaction() as c:
            return self._put(c, kind, ident, data)

    def list(self, kind):
        with self.engine.connect() as c:
            return [dict(r[0]) for r in c.execute(select(self.records.c.data).where(self.records.c.kind == kind))]

    def seed(self):
        with self.transaction() as c:
            for channel in ('tjr', 'gaming'):
                if not self._get(c, 'channel', channel):
                    self._put(c, 'channel', channel, {'id': channel, 'paused': True, 'youtube_id': '', 'autopublish': False})
            for ident, name, channel in [('tjr', 'TJR', 'tjr'), ('gaming', 'Gaming sources', 'gaming'), ('kai-cenat', 'Kai Cenat', 'gaming'), ('lacy', 'Lacy', 'gaming'), ('marlon', 'Marlon', 'gaming'), ('clix', 'Clix', 'gaming'), ('ishowspeed', 'IShowSpeed', 'gaming')]:
                if not self._get(c, 'creator', ident):
                    self._put(c, 'creator', ident, {'id': ident, 'name': name, 'channel': channel, 'permission': '', 'feed': '', 'live_url': '', 'enabled': False, 'campaign': 'tjr' if channel == 'tjr' else ''})

    def queue(self, creator, source, **extra):
        with self.transaction() as c:
            owner = self._get(c, 'creator', creator)
            if not owner:
                raise ValueError('Unknown creator')
            identity = source_identity(extra.get('original_source', source))
            if extra.get('captured_at'):
                identity += ':capture:' + str(extra['captured_at'])
                job_identity = identity + ':offset:' + str(extra.get('source_offset', 0))
            else:
                job_identity = identity
            if 'source_end' in extra:
                job_identity += ':range:' + str(extra.get('source_offset', 0)) + ':' + str(extra['source_end'])
            elif extra.get('source_offset', 0) and not extra.get('captured_at'):
                job_identity += ':start:' + str(extra['source_offset'])
            job_identity += ':render:' + render_variant(extra.get('render_options'))
            ident = hashlib.sha256((owner['channel'] + '\n' + job_identity).encode()).hexdigest()[:32]
            old = self._get(c, 'job', ident)
            if old:
                return old
            if not self._get(c, 'creator', creator):
                raise ValueError('Unknown creator')
            return self._put(c, 'job', ident, {'id': ident, 'creator': creator, 'source': source, 'source_identity': identity, 'state': 'queued', 'created': now(), 'engine_id': None, 'attempts': 0, **extra})

    def get_job(self, ident):
        return self.get('job', ident)

    def update_job(self, ident, **fields):
        with self.transaction() as c:
            row = self._get(c, 'job', ident)
            if not row:
                raise ValueError('Unknown job')
            return self._put(c, 'job', ident, {**row, **fields, 'updated': now()})

    def add_clip(self, job_id, artifact):
        ident = hashlib.sha256(f"{job_id}:{artifact['start']:.2f}:{artifact['end']:.2f}".encode()).hexdigest()[:32]
        with self.transaction() as c:
            job = self._get(c, 'job', job_id)
            creator = self._get(c, 'creator', job['creator'])
            for row in c.execute(select(self.records.c.data).where(self.records.c.kind == 'clip')):
                existing = row[0]
                other_job = self._get(c, 'job', existing['job_id'])
                other_creator = self._get(c, 'creator', other_job['creator'])
                overlap = max(0, min(existing['end'], artifact['end']) - max(existing['start'], artifact['start']))
                if job['source_identity'] == other_job.get('source_identity') and creator['channel'] == other_creator['channel'] and render_variant(job.get('render_options')) == render_variant(other_job.get('render_options')) and overlap / max(1, artifact['end'] - artifact['start']) > .5:
                    return dict(existing)
            return self._get(c, 'clip', ident) or self._put(c, 'clip', ident, {'id': ident, 'job_id': job_id, 'state': 'review', 'created': now(), **artifact})

    def update_clip(self, ident, **fields):
        with self.transaction() as c:
            row = self._get(c, 'clip', ident)
            if not row:
                raise ValueError('Unknown clip')
            return self._put(c, 'clip', ident, {**row, **fields, 'updated': now()})

    def review(self, ident, approve, checks=None):
        with self.transaction() as c:
            row = self._get(c, 'clip', ident)
            if not row or row['state'] not in ('review', 'approved', 'skipped'):
                raise ValueError('Clip is not reviewable')
            return self._put(c, 'clip', ident, {**row, 'state': 'approved' if approve else 'skipped', 'reviewed': now(), 'checks': checks or row.get('checks', {})})

    def review_count(self, channel=None):
        return sum(bool(x.get('reviewed')) and x['state'] != 'skipped' and (channel is None or self.get('creator', self.get_job(x['job_id'])['creator'])['channel'] == channel) for x in self.list('clip'))

    @staticmethod
    def _manual_reason(clip, creator, channel, campaign):
        from .rules import validate
        if not clip or clip.get('integration_test'):
            return 'Connection tests cannot be published'
        if not channel.get('youtube_id'):
            return 'Connect the destination YouTube channel first'
        errors = validate(clip, creator.get('campaign', ''))
        if errors:
            return 'Render checks: ' + ', '.join(errors)
        if creator.get('campaign') and (campaign.get('paused') or not campaign.get('ai_editing_allowed') or not campaign.get('submission_verified')):
            return 'Campaign eligibility and submission setup must be verified first'
        return ''

    def manual_block_reason(self, ident):
        clip = self.get('clip', ident)
        if not clip:
            return 'Unknown clip'
        creator = self.get('creator', self.get_job(clip['job_id'])['creator'])
        reason = self._manual_reason(clip, creator, self.get('channel', creator['channel']), self.get('campaign', creator.get('campaign', '')) or {})
        with self.engine.connect() as c:
            return reason or self._duplicate_reason(c, clip)

    def _duplicate_reason(self, connection, clip):
        job = self._get(connection, 'job', clip['job_id'])
        creator = self._get(connection, 'creator', job['creator'])
        active = ('publish_queued', 'uploading', 'upload_unknown', 'private_uploaded', 'release_pending', 'published')
        for row in connection.execute(select(self.records.c.data).where(self.records.c.kind == 'clip')):
            other = row[0]
            if other['id'] == clip['id'] or other['state'] not in active:
                continue
            other_job = self._get(connection, 'job', other['job_id'])
            other_creator = self._get(connection, 'creator', other_job['creator'])
            overlap = max(0, min(other['end'], clip['end']) - max(other['start'], clip['start']))
            if job['source_identity'] == other_job.get('source_identity') and creator['channel'] == other_creator['channel'] and overlap / max(1, clip['end'] - clip['start']) > .5:
                return 'Another version of this moment is already uploading or published'
        return ''

    def request_manual_publication(self, ident):
        with self.transaction() as c:
            clip = self._get(c, 'clip', ident)
            if not clip:
                raise ValueError('Unknown clip')
            if clip['state'] in ('publish_queued', 'uploading', 'upload_unknown', 'private_uploaded', 'release_pending', 'published'):
                return clip
            if clip['state'] not in ('review', 'approved', 'publish_error'):
                raise ValueError('This clip is not ready for publication')
            creator = self._get(c, 'creator', self._get(c, 'job', clip['job_id'])['creator'])
            reason = self._manual_reason(clip, creator, self._get(c, 'channel', creator['channel']), self._get(c, 'campaign', creator.get('campaign', '')) or {})
            reason = reason or self._duplicate_reason(c, clip)
            if reason:
                raise ValueError(reason)
            return self._put(c, 'clip', ident, {**clip, 'state': 'publish_queued', 'manual_publish': True,
                'permission_confirmed': now(), 'reviewed': clip.get('reviewed') or now(),
                'checks': {k: True for k in ('context', 'captions', 'visuals', 'metadata')},
                'publication_error': '', 'next_publish_attempt': 0})

    def request_auto_publication(self, ident):
        with self.transaction() as c:
            clip = self._get(c, 'clip', ident)
            if not clip or clip['state'] != 'approved' or not self.publishable(ident) or self._duplicate_reason(c, clip):
                return None
            return self._put(c, 'clip', ident, {**clip, 'state': 'publish_queued'})

    def autopublish_ready(self):
        return bool((self.get('setting', 'qualification') or {}).get('passed')) and self.review_count('tjr') >= 5 and self.review_count('gaming') >= 5

    def publishable(self, ident):
        clip = self.get('clip', ident)
        if not clip or clip['state'] != 'approved':
            return False
        creator = self.get('creator', self.get_job(clip['job_id'])['creator'])
        channel = self.get('channel', creator['channel'])
        checks = clip.get('checks', {})
        if not creator.get('permission') or channel['paused'] or not channel.get('youtube_id'):
            return False
        if not all(checks.get(k) for k in ('context', 'captions', 'visuals', 'metadata')):
            return False
        if creator.get('campaign'):
            campaign = self.get('campaign', creator['campaign']) or {}
            if not campaign.get('ai_editing_allowed') or not campaign.get('submission_verified'):
                return False
        return True

    def reserve(self, maximum, daily=1000000, monthly=10000000):
        if not isinstance(maximum, int) or maximum <= 0:
            raise ValueError('Invalid reservation')
        stamp = now()
        with self.transaction() as c:
            rows = c.execute(select(self.budget)).mappings().all()
            used = lambda prefix: sum(r['actual'] if r['actual'] is not None else r['reserved'] for r in rows if r['created'].startswith(prefix))
            if used(stamp[:10]) + maximum > daily or used(stamp[:7]) + maximum > monthly:
                raise ValueError('OpenAI budget ceiling reached')
            ident = str(uuid.uuid4())
            c.execute(insert(self.budget).values(id=ident, created=stamp, reserved=maximum, actual=None))
            return ident

    def settle(self, ident, actual):
        if actual is None:
            return  # Connection loss may already have incurred a charge.
        with self.transaction() as c:
            c.execute(update(self.budget).where(self.budget.c.id == ident).values(actual=max(0, int(actual))))

    def usage(self):
        with self.engine.connect() as c:
            rows = c.execute(select(self.budget)).mappings().all()
            return {'reservations': len(rows), 'committed_usd': sum(r['actual'] if r['actual'] is not None else r['reserved'] for r in rows) / 1000000}

    def notify(self, text):
        ident = str(uuid.uuid4())
        self.put('notification', ident, {'id': ident, 'text': text, 'created': now(), 'sent': False})
