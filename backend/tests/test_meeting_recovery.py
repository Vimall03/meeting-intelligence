import asyncio
import json
import time
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import WebSocketDisconnect
from app.core.auth import Principal, issue_ticket
from app.services.meeting_sessions import MeetingSessions, MeetingCheckpoints, MeetingBusy, MeetingConnections
from app.services.meeting_sessions import context_state, restore_context, event_state, restore_events
from app.services.meeting_context_service import MeetingContextService
from app.services.event_service import EventService
from app.services.jev_service import JevDecision
from test_auth_integration import MemoryStore
from test_meeting_stream_timeline import route, scoped_provider, FakeDrive, FakeJev, FakeLlm


class RecoverySocket:
    def __init__(self, db, frames=(), end_ack=None, end_revision=None, end_llm=False):
        self.app = SimpleNamespace(state=SimpleNamespace(users=db))
        self.frames = list(frames)
        self.sent = []
        self.finished = asyncio.Event()
        self.end_ack, self.end_revision, self.end_llm = end_ack, end_revision, end_llm

    async def accept(self): pass

    async def receive_text(self):
        principal = Principal('A', 'family-A', int(time.time()) + 900)
        return json.dumps({'ticket': await issue_ticket(principal, self.app.state.users), 'meetingId': 'meeting-1'})

    async def receive(self):
        if self.frames:
            return self.frames.pop(0)
        await self.finished.wait()
        raise WebSocketDisconnect()

    async def close(self, **kwargs):
        self.closed = kwargs
        self.finished.set()

    async def send_json(self, message):
        self.sent.append(message)
        if (message['type'] == 'audio_ack' and message['sequence'] == self.end_ack
            or message['type'] == 'timeline_snapshot' and message['revision'] == self.end_revision
            or self.end_llm and message['type'] == 'llm_result' and message['decision'] == 'SEARCH_EXISTING_WORK'):
            self.finished.set()


def window(sequence, text):
    return [{'type': 'websocket.receive', 'text': json.dumps({'type': 'audio', 'sequence': sequence})},
            {'type': 'websocket.receive', 'bytes': text.encode()}]


class MeetingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_context_preserves_legacy_shape_and_restores_new_background(self):
        from app.services.event_service import EventService
        from app.services.jev_service import JevDecision
        from app.services.meeting_sessions import event_state, restore_events
        service = EventService(search_threshold=.75, task_threshold=.75, dedupe_window_seconds=90)
        service.start_pending(JevDecision(0, .9), 'Recent reference', 'Please get that', time.monotonic(),
            background_context='Earlier concrete work')
        state = event_state(service)
        self.assertNotIn('background_context', state['pending']['SUGGEST_NEW_TASK'])
        restored = EventService(search_threshold=.75, task_threshold=.75, dedupe_window_seconds=90)
        restore_events(restored, state)
        self.assertEqual(restored._pending['SUGGEST_NEW_TASK'].background_context, 'Earlier concrete work')

    def setUp(self):
        self.db = MemoryStore()
        self.a = Principal('A', 'family-A', int(time.time()) + 900)
        self.state = {'ack': 1, 'timeline': {'session_id': 'stable', 'revision': 0, 'items': []}, 'context': {}, 'jobs': {}}

    async def test_encrypted_checkpoint_survives_new_owner_but_not_other_user(self):
        first = MeetingSessions(self.db, self.a, 'meeting')
        await first.acquire(); await first.save(self.state); await first.release()
        second = MeetingSessions(self.db, self.a, 'meeting')
        self.assertEqual(await second.acquire(), self.state)
        self.assertNotIn('stable', json.dumps(self.db.rows))
        b = Principal('B', 'family-B', self.a.expires)
        self.assertEqual(await MeetingSessions(self.db, b, 'meeting').acquire(), {})

    async def test_duplicate_owner_and_late_writer_are_fenced(self):
        first = MeetingSessions(self.db, self.a, 'meeting'); await first.acquire()
        with self.assertRaises(MeetingBusy): await MeetingSessions(self.db, self.a, 'meeting').acquire()
        self.db.rows[first.key]['leaseUntil'] = 0
        second = MeetingSessions(self.db, self.a, 'meeting'); await second.acquire(); await second.save(self.state)
        with self.assertRaises(MeetingBusy): await first.save(self.state)
        await first.release()
        self.assertEqual(self.db.rows[first.key]['owner'], second.owner)

    async def test_unchanged_checkpoint_coalesces_but_changed_ack_and_lease_renewal_are_durable(self):
        now = [10.0]
        session = MeetingSessions(self.db, self.a, 'coalesced')
        await session.acquire()
        checkpoints = MeetingCheckpoints(session, clock=lambda: now[0])
        self.assertTrue(await checkpoints.save(self.state))
        version = session.version
        self.state['events'] = {'offset': 123.000001}
        self.assertFalse(await checkpoints.save(self.state))
        self.assertEqual(session.version, version)
        self.state['ack'] = 2
        self.assertTrue(await checkpoints.save(self.state))
        now[0] += 5
        self.assertTrue(await checkpoints.save(self.state))
        saved = await self.db.decrypt('A', 'meeting-state', self.db.rows[session.key]['cipher'])
        self.assertEqual(saved['ack'], 2)
        self.assertEqual(session.version, version + 2)

    async def test_coalesced_checkpoints_still_fence_expired_owner_and_do_not_hide_failures(self):
        now = [10.0]
        first = MeetingSessions(self.db, self.a, 'coalesced')
        await first.acquire()
        checkpoints = MeetingCheckpoints(first, clock=lambda: now[0])
        await checkpoints.save(self.state)
        self.db.rows[first.key]['leaseUntil'] = 0
        second = MeetingSessions(self.db, self.a, 'coalesced')
        await second.acquire()
        now[0] += 5
        for _ in range(2):
            with self.assertRaises(MeetingBusy):
                await checkpoints.save(self.state)

    async def test_work_completion_wakes_socket_without_waiting_for_audio_or_poll_timeout(self):
        self.db.test_drive = FakeDrive()
        accepted = []
        completed = []
        class TimedSocket(RecoverySocket):
            async def send_json(self, message):
                if message['type'] == 'transcription_state':
                    accepted.append(time.perf_counter())
                await super().send_json(message)
        async def transcribe(audio):
            await asyncio.sleep(.01)
            completed.append(time.perf_counter())
            return {'audio_duration': 3, 'segments': [{'start': 0, 'end': 3, 'text': 'complete words'}]}
        ws = TimedSocket(self.db, window(1, 'audio'), end_ack=1)
        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''), patch.object(route, 'transcribe_snapshot', transcribe):
            await asyncio.wait_for(route.meeting_stream(ws), 2)
        self.assertLess(accepted[0] - completed[0], .15)

    async def test_revoked_session_cannot_resume_or_write(self):
        first = MeetingSessions(self.db, self.a, 'meeting'); await first.acquire()
        await self.db.put('REVOKED#family-A', {'expiresAt': int(time.time()) + 86400})
        from fastapi import HTTPException
        from app.services.user_store import StoreConflict
        with self.assertRaises(HTTPException): await MeetingSessions(self.db, self.a, 'meeting').acquire()
        with self.assertRaises(StoreConflict): await first.save(self.state)

    async def test_other_login_session_cannot_take_over_meeting(self):
        first = MeetingSessions(self.db, self.a, 'meeting'); await first.acquire(); await first.release()
        other = Principal('A', 'different-family', self.a.expires)
        with self.assertRaises(MeetingBusy): await MeetingSessions(self.db, other, 'meeting').acquire()

    async def test_context_pending_events_and_dedupe_restore(self):
        context = MeetingContextService()
        context.add_segments([{'start': 0, 'end': 3, 'text': 'Please send the launch plan.'}])
        new = MeetingContextService(); restore_context(new, context_state(context))
        self.assertEqual(new.prepare_jev_context(), context.prepare_jev_context())
        events = EventService(.75, .75, 90)
        events.start_pending(JevDecision(.9, .9), context.as_text(), context.as_text(), time.monotonic())
        restored = EventService(.75, .75, 90); restore_events(restored, event_state(events))
        self.assertEqual(restored.pending_types(), events.pending_types())

    async def test_shutdown_drains_sockets_without_auth_error(self):
        registry = MeetingConnections(); ws = RecoverySocket(self.db)
        registry.sockets.add(ws)
        await registry.drain()
        self.assertTrue(registry.draining)
        self.assertEqual(ws.closed['code'], 1012)

    async def test_replayed_audio_is_deduped_and_same_timeline_context_resumes(self):
        self.db.test_drive = FakeDrive()
        calls = []
        async def transcribe(audio):
            calls.append(audio)
            return {'audio_duration': 3, 'segments': [{'start': 0, 'end': 3, 'text': audio.decode()}]}
        first = RecoverySocket(self.db, window(1, 'first words'), end_ack=1)
        second = RecoverySocket(self.db, window(1, 'first words') + window(2, 'second words'), end_ack=2)
        with self.assertLogs('meeting_intelligence', level='INFO') as captured, patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''), patch.object(route, 'transcribe_snapshot', transcribe):
            await asyncio.wait_for(route.meeting_stream(first), 3)
            await asyncio.wait_for(route.meeting_stream(second), 3)
        connected = [record.metadata for record in captured.records if record.getMessage() == 'meeting.connected']
        self.assertEqual([entry['resumed'] for entry in connected], [False, True])
        self.assertEqual({entry['meeting_id'] for entry in connected}, {'meeting-1'})
        self.assertEqual({entry['user_id'] for entry in connected}, {'A'})
        self.assertEqual(len({entry['connection_id'] for entry in connected}), 2)
        self.assertEqual(calls, [b'first words', b'second words'])
        self.assertEqual(next(m for m in second.sent if m['type'] == 'auth_ready')['ack'], 1)
        one = next(m for m in first.sent if m['type'] == 'timeline_snapshot')
        two = next(m for m in second.sent if m['type'] == 'timeline_snapshot')
        self.assertEqual(one['session_id'], two['session_id'])
        saved = await MeetingSessions(self.db, self.a, 'meeting-1').acquire()
        self.assertEqual(saved['ack'], 2)
        self.assertEqual([s['text'] for s in saved['context']['segments']], ['first words', 'second words'])

    async def test_search_interrupted_by_disconnect_resumes_on_next_process(self):
        class WaitingDrive:
            async def search_relevant_files(self, *args, **kwargs):
                await asyncio.Event().wait()
        self.db.test_drive = WaitingDrive()
        first = RecoverySocket(self.db, window(1, 'Please send the launch plan to Maya.'), end_llm=True)
        second = RecoverySocket(self.db, end_revision=2)
        async def transcribe(audio):
            return {'audio_duration': 3, 'segments': [{'start': 0, 'end': 3, 'text': audio.decode()}]}
        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', 'test'), patch.object(route, 'JevService', FakeJev), patch.object(route, 'LlmService', FakeLlm), patch.object(route, 'transcribe_snapshot', transcribe):
            await asyncio.wait_for(route.meeting_stream(first), 4)
            self.db.test_drive = FakeDrive()
            await asyncio.wait_for(route.meeting_stream(second), 4)
        self.assertTrue(any(m['type'] == 'timeline_snapshot' and any(i['kind'] == 'search_results' for i in m['items']) for m in second.sent))

    async def test_real_uvicorn_restart_closes_1012_and_resumes_saved_audio_progress(self):
        import uvicorn
        import websockets
        from fastapi import FastAPI
        self.db.test_drive = FakeDrive()

        async def start_server():
            app = FastAPI()
            app.state.users = self.db
            app.state.meetings = MeetingConnections()
            app.include_router(route.router)
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.setblocking(False)
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off', timeout_graceful_shutdown=3))
            # Avoid changing the test runner's process signal handlers.
            from contextlib import nullcontext
            server.capture_signals = nullcontext
            task = asyncio.create_task(server.serve(sockets=[listener]))
            for _ in range(100):
                if server.started: break
                if task.done(): await task
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            return server, task, listener, f'ws://127.0.0.1:{listener.getsockname()[1]}/ws/meeting'

        async def transcribe(audio):
            return {'audio_duration': 3, 'segments': [{'start': 0, 'end': 3, 'text': 'retained words'}]}

        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''), patch.object(route, 'transcribe_snapshot', transcribe):
            server, task, listener, url = await start_server()
            try:
                async with websockets.connect(url) as client:
                    ticket = await issue_ticket(self.a, self.db)
                    await client.send(json.dumps({'ticket': ticket, 'meetingId': 'meeting-live'}))
                    while json.loads(await asyncio.wait_for(client.recv(), 2))['type'] != 'auth_ready': pass
                    await client.send(json.dumps({'type': 'audio', 'sequence': 1})); await client.send(b'audio')
                    while json.loads(await asyncio.wait_for(client.recv(), 2))['type'] != 'audio_ack': pass
                    server.should_exit = True
                    with self.assertRaises(websockets.exceptions.ConnectionClosed) as closed:
                        while True: await asyncio.wait_for(client.recv(), 3)
                    self.assertEqual(closed.exception.rcvd.code, 1012)
                await asyncio.wait_for(task, 5)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
                listener.close()
            server, task, listener, url = await start_server()
            try:
                async with websockets.connect(url) as client:
                    ticket = await issue_ticket(self.a, self.db)
                    await client.send(json.dumps({'ticket': ticket, 'meetingId': 'meeting-live', 'lastAck': 1}))
                    ready = json.loads(await asyncio.wait_for(client.recv(), 2))
                    self.assertEqual((ready['type'], ready['ack']), ('auth_ready', 1))
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
                listener.close()
