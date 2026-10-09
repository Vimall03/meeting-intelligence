"""Encrypted checkpoints and fenced ownership for reconnectable meetings."""
import asyncio
import json
import re
import secrets
import time
import hashlib
from dataclasses import asdict

from app.core.auth import ensure_active
from app.services.user_store import StoreConflict


class MeetingBusy(Exception):
    pass


class MeetingResumeExpired(Exception):
    pass


class MeetingCheckpoints:
    """Skip identical writes while renewing the fenced lease well before expiry.

    Changed state is still durable before any acknowledgement or result is sent.
    This object belongs to one authenticated socket, never a shared user cache.
    """
    def __init__(self, session, *, clock=time.monotonic):
        self.session, self.clock = session, clock
        self.saved_digest = None
        self.saved_at = 0.0

    @staticmethod
    def digest(state):
        events = {k: v for k, v in state.get('events', {}).items() if k != 'offset'}
        # Wall/monotonic conversion jitter isn't a meeting-state change.
        stable = {**state, 'events': events}
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).digest()

    async def save(self, state):
        digest = self.digest(state)
        if digest == self.saved_digest and self.clock() - self.saved_at < 5:
            return False
        await self.session.save(state)
        # save may trim an oversized timeline; compare the persisted state.
        self.saved_digest, self.saved_at = self.digest(state), self.clock()
        return True


class MeetingSessions:
    LEASE_SECONDS = 20
    RETENTION_SECONDS = 86400

    def __init__(self, db, principal, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id):
            raise ValueError('Invalid meeting ID')
        self.db, self.principal, self.session_id = db, principal, session_id
        self.key = f'USER#{principal.sub}#MEETING#{session_id}'
        self.owner = secrets.token_hex(16)
        self.version = None

    async def acquire(self):
        await ensure_active(self.principal, self.db)
        row = await self.db.get(self.key)
        now = int(time.time())
        if row and row.get('expiresAt', 0) <= now:
            row = {**row, 'cipher': None, 'leaseUntil': 0, 'family': self.principal.family}
        if row and (row['family'] != self.principal.family or row.get('leaseUntil', 0) > now):
            raise MeetingBusy()
        version = row['version'] if row else None
        record = {**(row or {}), 'family': self.principal.family, 'owner': self.owner,
            'leaseUntil': now + self.LEASE_SECONDS, 'expiresAt': now + self.RETENTION_SECONDS,
            'version': (version or 0) + 1}
        try:
            await self.db.put(self.key, record, version=version, absent=row is None, family=self.principal.family)
        except StoreConflict:
            raise MeetingBusy() from None
        self.version = record['version']
        return await self.db.decrypt(self.principal.sub, 'meeting-state', row['cipher']) if row and row.get('cipher') else {}

    async def save(self, state):
        row = await self.db.get(self.key)
        now = int(time.time())
        if not row or row.get('owner') != self.owner or row['version'] != self.version or row['leaseUntil'] <= now:
            raise MeetingBusy()
        # Keep each encrypted item well below DynamoDB's 400 KB item limit.
        # Timeline revisions/IDs stay monotonic when old cards are trimmed.
        while len(json.dumps(state).encode()) > 180_000 and state['timeline']['items']:
            state['timeline']['items'].pop()
        if len(json.dumps(state).encode()) > 180_000:
            raise ValueError('Meeting checkpoint too large')
        cipher = await self.db.encrypt(self.principal.sub, 'meeting-state', state)
        await self.db.put(self.key, {**row, 'cipher': cipher, 'version': self.version + 1,
            'leaseUntil': now + self.LEASE_SECONDS, 'expiresAt': now + self.RETENTION_SECONDS},
            version=self.version, family=self.principal.family)
        self.version += 1

    async def release(self):
        row = await self.db.get(self.key)
        if row and row.get('owner') == self.owner and row['version'] == self.version:
            try:
                await self.db.put(self.key, {**row, 'leaseUntil': 0, 'version': self.version + 1}, version=self.version)
            except StoreConflict:
                pass


def context_state(context):
    return {'segments': [asdict(s) for s in context.get_segments()],
        'pending': [asdict(s) for s in context._unevaluated_segments],
        'candidates': [asdict(s) for s in context._intent_candidates],
        'background': [asdict(s) for s in context._background_segments]}


def restore_context(context, state):
    context.add_segments(state.get('segments', []))
    from app.services.meeting_context_service import TranscriptSegment
    context._unevaluated_segments = [TranscriptSegment(**s) for s in state.get('pending', [])]
    context._intent_candidates = [TranscriptSegment(**s) for s in state.get('candidates', [])]
    context._background_segments = [TranscriptSegment(**s) for s in state.get('background', state.get('segments', []))]


def event_state(service):
    offset = time.time() - time.monotonic()
    return {'offset': offset,
        'tasks': service._recent_task_triggers,
        'topics': [(at, list(topic)) for at, topic in service._recent_search_topics],
        # Keep the constructor payload readable by the previous release.
        # New context lives beside it so rollback can resume the same work.
        'pending': {k: {name: value for name, value in asdict(v).items() if name != 'background_context'}
                    for k, v in service._pending.items()},
        'backgrounds': {k: v.background_context for k, v in service._pending.items()},
        'search': asdict(service._search_session) if service._search_session else None}


def restore_events(service, state):
    from app.services.event_service import _PendingEvent, _SearchSession
    delta = state.get('offset', 0) - (time.time() - time.monotonic())
    service._recent_task_triggers = [(at + delta, text) for at, text in state.get('tasks', [])]
    service._recent_search_topics = [(at + delta, frozenset(topic)) for at, topic in state.get('topics', [])]
    for k, value in state.get('pending', {}).items():
        value = {**value, 'started_at': value['started_at'] + delta, 'last_update_at': value['last_update_at'] + delta}
        value['background_context'] = state.get('backgrounds', {}).get(k, value.get('background_context', ''))
        service._pending[k] = _PendingEvent(**value)
    search = state.get('search')
    if search:
        service._search_session = _SearchSession(**{**search, 'last_search_at': search['last_search_at'] + delta,
            'last_activity_at': search['last_activity_at'] + delta})


class MeetingConnections:
    def __init__(self):
        self.sockets = set()
        self.draining = False

    async def drain(self):
        self.draining = True
        await asyncio.gather(*(ws.close(code=1012, reason='MI-CON-004') for ws in list(self.sockets)), return_exceptions=True)
