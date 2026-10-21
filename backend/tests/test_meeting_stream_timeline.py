import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import asynccontextmanager
from app.core.auth import Principal, issue_ticket
from test_auth_integration import MemoryStore
import time, json

from streaming_fakes import transcription_stream, DefaultStream
from fastapi import WebSocketDisconnect

from app.services.jev_service import JevDecision
from app.services.llm_service import IntentReview, LlmResult


route = importlib.import_module("app.api.routes.meeting_stream")


class FakeWebSocket:
    def __init__(self, *, drive=None, finish_on_empty_search: bool = False) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(google_drive=drive or FakeDrive(), users=MemoryStore()))
        self.sent: list[dict] = []
        self.finished = asyncio.Event()
        self.received_audio = False
        self.finish_on_empty_search = finish_on_empty_search

    async def receive_text(self):
        user = getattr(self, 'user', 'A')
        return json.dumps({'ticket': await issue_ticket(Principal(user, 'family-' + user, int(time.time()) + 900), self.app.state.users), 'meetingId': 'test-meeting', 'audioFormat': 'pcm_s16le_16000_v2'})
    async def receive(self):
        if getattr(self, '_pending_pcm', None) is not None:
            pcm, self._pending_pcm = self._pending_pcm, None
            return {'type': 'websocket.receive', 'bytes': pcm}
        self._pending_pcm = (await self.receive_bytes()).ljust(6400, b' ')
        start = getattr(self, '_sent_samples', 0)
        self._sent_samples = start + len(self._pending_pcm) // 2
        return {'type': 'websocket.receive', 'text': json.dumps({'type': 'audio', 'startSample': start})}
    async def close(self, **_kwargs):
        self.closed = _kwargs
        self.finished.set()
    async def accept(self) -> None:
        pass

    async def receive_bytes(self) -> bytes:
        if not self.received_audio:
            self.received_audio = True
            return b"audio"
        await self.finished.wait()
        raise WebSocketDisconnect()

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        if message.get("type") == "timeline_snapshot" and message.get("revision") == 2:
            self.finished.set()
        if self.finish_on_empty_search and message.get("type") == "RELATED_FILES" and not message.get("files"):
            self.finished.set()


@asynccontextmanager
async def scoped_provider(db, principal, provider):
    if provider == 'google_drive': yield db.test_drive
    else:
        class EmptyAtlassian:
            async def status(self): return {'connected': False}
        yield getattr(db, 'test_atlassian', None) or EmptyAtlassian()

class FakeDrive:
    async def search_relevant_files(self, _terms, *, top_k: int) -> dict:
        assert top_k == 5
        return {
            "query": "launch plan",
            "files": [{
                "id": "file-1",
                "name": "Launch plan",
                "mime_type": "application/vnd.google-apps.document",
                "web_view_link": "https://drive.google.com/file-1",
                "source": "google_drive",
                "matched_terms": ["launch plan"],
                "relevance_score": 28,
            }],
            "search_latency_ms": 1,
            "ranking_latency_ms": 1,
        }


class EmptyDrive:
    async def search_relevant_files(self, _terms, *, top_k: int) -> dict:
        assert top_k == 5
        return {
            "query": "launch plan",
            "files": [],
            "search_latency_ms": 1,
            "ranking_latency_ms": 1,
        }


class FakeJev:
    def __init__(self, **_kwargs) -> None:
        pass

    async def decide(self, _recent: str, _new: str, **_kwargs) -> JevDecision:
        return JevDecision(0.9, 0.9)

    async def close(self) -> None:
        pass


class FakeLlm:
    def __init__(self, **_kwargs) -> None:
        pass

    async def review_intent(self, recent, new, **_kwargs):
        return IntentReview(True, True, search_target="launch plan", task_action="Send launch plan")

    async def process_event(self, event) -> LlmResult:
        if event.type == "SUGGEST_NEW_TASK":
            data = {"task_title": "Send launch plan", "task_description": "Send the plan to Maya."}
        else:
            data = {"primary_terms": ["launch plan"], "alternate_terms": [], "entities": []}
        return LlmResult(event_type=event.type, data=data)

    async def close(self) -> None:
        pass


class MeetingStreamTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_intent_produces_cards_only_after_semantic_confirmation(self):
        class UncertainJev(FakeJev):
            async def decide(self, recent, new, **_kwargs):
                return JevDecision(.4, .37)
        socket = FakeWebSocket()
        async def transcribe(_audio):
            return {"audio_duration": 3, "segments": [{"start": 0, "end": 3,
                "text": "Please send the launch plan to Maya."}]}
        with (
            patch.object(route, 'provider_session', scoped_provider),
            patch.object(socket.app.state.users, 'test_drive', socket.app.state.google_drive, create=True),
            patch.object(route.settings, "openrouter_api_key", "test-key"),
            patch.object(route, "JevService", UncertainJev),
            patch.object(route, "LlmService", FakeLlm),
            patch.object(route, "AmazonStreamingTranscription", transcription_stream(transcribe)),
        ):
            await asyncio.wait_for(route.meeting_stream(socket), timeout=2)
        items = [m for m in socket.sent if m["type"] == "timeline_snapshot"][-1]["items"]
        self.assertEqual({item["kind"] for item in items}, {"search_results", "task_suggestion"})

    async def test_failed_decision_is_retried_without_waiting_for_more_speech(self):
        from app.services.jev_service import InvalidJevDecision
        for first_error in (TimeoutError("private provider details"), InvalidJevDecision("Incomplete meeting decision")):
            with self.subTest(error=type(first_error).__name__):
                calls = []
                class RecoveringJev(FakeJev):
                    async def decide(self, recent, new, **_kwargs):
                        calls.append((recent, new))
                        if len(calls) < 3:
                            raise first_error
                        return JevDecision(.9, .9)
                websocket = FakeWebSocket()
                async def transcribe(_audio):
                    return {"audio_duration": 3, "segments": [{"start": 0, "end": 3,
                        "text": "Please send the launch plan to Maya."}]}
                with (
                    patch.object(route, 'provider_session', scoped_provider),
                    patch.object(websocket.app.state.users, 'test_drive', websocket.app.state.google_drive, create=True),
                    patch.object(route.settings, "openrouter_api_key", "test-key"),
                    patch.object(route, "JevService", RecoveringJev),
                    patch.object(route, "LlmService", FakeLlm),
                    patch.object(route, "AmazonStreamingTranscription", transcription_stream(transcribe)),
                ):
                    await asyncio.wait_for(route.meeting_stream(websocket), timeout=4)
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(calls[0], calls[2])
                items = [m for m in websocket.sent if m["type"] == "timeline_snapshot"][-1]["items"]
                self.assertEqual(len(items), 2)
                self.assertTrue(any(item["kind"] == "search_results" for item in items))
                self.assertTrue(any(item["kind"] == "task_suggestion" for item in items))

    async def test_decision_outage_has_a_bounded_retry_budget(self):
        calls = []
        class UnavailableJev(FakeJev):
            async def decide(self, recent, new, **_kwargs):
                calls.append(new)
                raise TimeoutError("private provider details")
        socket = FakeWebSocket()
        async def transcribe(_audio):
            return {"audio_duration": 3, "segments": [{"start": 0, "end": 3,
                "text": "Please send the launch plan to Maya."}]}
        with (
            patch.object(route, 'provider_session', scoped_provider),
            patch.object(socket.app.state.users, 'test_drive', socket.app.state.google_drive, create=True),
            patch.object(route.settings, "openrouter_api_key", "test-key"),
            patch.object(route, "JevService", UnavailableJev),
            patch.object(route, "LlmService", FakeLlm),
            patch.object(route, "AmazonStreamingTranscription", transcription_stream(transcribe)),
        ):
            runner = asyncio.create_task(route.meeting_stream(socket))
            try:
                await asyncio.sleep(2)
                self.assertEqual(len(calls), 3)
                await asyncio.sleep(.5)
                self.assertEqual(len(calls), 3)
            finally:
                socket.finished.set()
                await asyncio.wait_for(runner, timeout=1)
        self.assertTrue(any(m["type"] == "audio_ack" for m in socket.sent))
        self.assertFalse(any(m.get("items") for m in socket.sent))

    async def test_websocket_publishes_complete_ordered_timeline(self) -> None:
        websocket = FakeWebSocket()

        async def transcribe(_audio: bytes) -> dict:
            return {
                "audio_duration": 3,
                "segments": [{"start": 0, "end": 3, "text": "Please send the launch plan to Maya."}],
            }

        with (
            patch.object(route, 'provider_session', scoped_provider),
            patch.object(websocket.app.state.users, 'test_drive', websocket.app.state.google_drive, create=True),
            patch.object(websocket.app.state.users, 'test_atlassian', getattr(websocket.app.state, 'atlassian', None), create=True),
            patch.object(route.settings, "openrouter_api_key", "test-key"),
            patch.object(route, "JevService", FakeJev),
            patch.object(route, "LlmService", FakeLlm),
            patch.object(route, "AmazonStreamingTranscription", transcription_stream(transcribe)),
        ):
            await asyncio.wait_for(route.meeting_stream(websocket), timeout=4)

        snapshots = [message for message in websocket.sent if message["type"] == "timeline_snapshot"]
        self.assertEqual([snapshot["revision"] for snapshot in snapshots], [0, 1, 2])
        latest = snapshots[-1]
        self.assertEqual(len(latest["items"]), 2)
        self.assertEqual([item["sequence"] for item in latest["items"]], [2, 1])
        task = next(item for item in latest["items"] if item["kind"] == "task_suggestion")
        search = next(item for item in latest["items"] if item["kind"] == "search_results")
        self.assertEqual((task["title"], task["description"]), ("Send launch plan", "Send the plan to Maya."))
        self.assertEqual(search["source"]["label"], "Google Drive")
        self.assertEqual(search["files"][0]["name"], "Launch plan")

    async def test_websocket_does_not_publish_empty_search_card(self) -> None:
        websocket = FakeWebSocket(drive=EmptyDrive(), finish_on_empty_search=True)

        async def transcribe(_audio: bytes) -> dict:
            return {
                "audio_duration": 3,
                "segments": [{"start": 0, "end": 3, "text": "Please send the launch plan to Maya."}],
            }

        with (
            patch.object(route, 'provider_session', scoped_provider),
            patch.object(websocket.app.state.users, 'test_drive', websocket.app.state.google_drive, create=True),
            patch.object(websocket.app.state.users, 'test_atlassian', getattr(websocket.app.state, 'atlassian', None), create=True),
            patch.object(route.settings, "openrouter_api_key", "test-key"),
            patch.object(route, "JevService", FakeJev),
            patch.object(route, "LlmService", FakeLlm),
            patch.object(route, "AmazonStreamingTranscription", transcription_stream(transcribe)),
        ):
            await asyncio.wait_for(route.meeting_stream(websocket), timeout=4)

        snapshots = [message for message in websocket.sent if message["type"] == "timeline_snapshot"]
        self.assertEqual([snapshot["revision"] for snapshot in snapshots], [0, 1])
        self.assertEqual([item["kind"] for item in snapshots[-1]["items"]], ["task_suggestion"])

    async def test_transcripts_continue_after_task_and_atlassian_failures(self) -> None:
        for failure in ("status", "search", "partial"):
            with self.subTest(failure=failure):
                class FailingAtlassian:
                    async def status(self):
                        if failure == "status":
                            raise RuntimeError("status unavailable")
                        return {"connected": True}

                    async def search_relevant_files(self, terms, **kwargs):
                        if failure == "search":
                            raise RuntimeError("search unavailable")
                        return {"query": terms.query, "sources": {}, "errors": {"confluence": "not authorized"}}

                class ContinuingSocket(FakeWebSocket):
                    next_audio = False
                    def __init__(self):
                        super().__init__()
                        self.next_ready = asyncio.Event()

                    async def receive_bytes(self):
                        if not self.received_audio:
                            self.received_audio = True
                            return b"first"
                        if self.next_audio:
                            self.next_audio = False
                            return b"later"
                        # A real pending socket receive wakes when later audio
                        # arrives; it does not require periodic cancellation.
                        await self.next_ready.wait()
                        self.next_ready.clear()
                        if self.next_audio:
                            self.next_audio = False
                            return b"later"
                        raise WebSocketDisconnect()

                    async def send_json(self, message):
                        self.sent.append(message)
                        if message.get("type") == "timeline_snapshot" and message.get("revision") == 2:
                            self.next_audio = True
                            self.next_ready.set()
                        if message.get("type") == "transcription_state" and "next topic" in message["transcript"]:
                            self.finished.set()
                            self.next_ready.set()

                websocket = ContinuingSocket()
                websocket.app.state.atlassian = FailingAtlassian()
                async def transcribe(audio):
                    return {"audio_duration": 3, "segments": [{"start": 0, "end": 3, "text": "Please send the launch plan to Maya." if audio.rstrip() == b"first" else "Now the next topic is checkout."}]}

                with (
                    patch.object(route, 'provider_session', scoped_provider),
                    patch.object(websocket.app.state.users, 'test_drive', websocket.app.state.google_drive, create=True),
            patch.object(websocket.app.state.users, 'test_atlassian', getattr(websocket.app.state, 'atlassian', None), create=True),
            patch.object(route.settings, "openrouter_api_key", "test-key"),
                    patch.object(route, "JevService", FakeJev),
                    patch.object(route, "LlmService", FakeLlm),
                    patch.object(route, "AmazonStreamingTranscription", transcription_stream(transcribe)),
                ):
                    await asyncio.wait_for(route.meeting_stream(websocket), timeout=5)
                updates = [message for message in websocket.sent if message["type"] == "transcription_state"]
                self.assertEqual(len(updates), 2)
                self.assertIn("next topic", updates[-1]["transcript"])


    async def test_concurrent_meetings_never_exchange_results_or_timeline_sessions(self):
        from app.services.integrations import provider_session as real_provider
        from app.services.google_drive_service import GoogleDriveService
        from app.services.user_store import CredentialStore
        import httpx
        db = MemoryStore()
        sockets = [FakeWebSocket(), FakeWebSocket()]
        for user, socket in zip(('A', 'B'), sockets):
            socket.user = user; socket.app.state.users = db
            store = CredentialStore(db, user, 'google_drive')
            await store.write({'access_token': user + '-secret', 'refresh_token': user + '-refresh', 'expires_at': time.time() + 900})
        def drive_api(request):
            user = request.headers['Authorization'].split()[-1].split('-')[0]
            return httpx.Response(200, json={'files': [{'id': user + '-private', 'name': user + ' launch plan', 'mimeType': 'text/plain'}]})
        original = GoogleDriveService.__init__
        def init(service, config, **kwargs): original(service, config, transport=httpx.MockTransport(drive_api), **kwargs)
        async def transcribe(audio):
            return {'audio_duration': 3, 'segments': [{'start': 0, 'end': 3, 'text': 'Please send the launch plan to Maya.'}]}
        class UncertainJev(FakeJev):
            async def decide(self, recent, new, **_kwargs): return JevDecision(.4, .4)
        with self.assertLogs('meeting_intelligence', level='INFO') as captured, patch.object(route, 'provider_session', real_provider), patch.object(GoogleDriveService, '__init__', init), patch.object(route.settings, 'openrouter_api_key', 'test-key'), patch.object(route, 'JevService', UncertainJev), patch.object(route, 'LlmService', FakeLlm), patch.object(route, 'AmazonStreamingTranscription', transcription_stream(transcribe)):
            await asyncio.wait_for(asyncio.gather(*(route.meeting_stream(s) for s in sockets)), timeout=5)
        from app.core.observability import JsonFormatter
        logs = [json.loads(JsonFormatter().format(record)) for record in captured.records]
        for owner in ('A', 'B'):
            own = [entry for entry in logs if entry.get('user_id') == owner]
            self.assertEqual(len({entry['meeting_id'] for entry in own}), 1)
            self.assertEqual(len({entry['connection_id'] for entry in own}), 1)
            self.assertEqual(len({entry['request_id'] for entry in own}), 1)
            self.assertTrue(any(entry['event'] == 'meeting.transcription.accepted' and entry['sequence'] == 3200 for entry in own))
            self.assertTrue(any(entry['event'] == 'meeting.search.results' and entry['provider'] == 'google_drive' for entry in own))
            self.assertTrue(any(entry['event'] == 'meeting.operation.completed' and entry.get('stage') == 'intent_review' for entry in own))
        self.assertEqual(len({entry['connection_id'] for entry in logs}), 2)
        for secret in ('-secret', '-refresh', 'launch plan', '-private', 'Maya'):
            self.assertNotIn(secret, json.dumps(logs))
        latest = [[m for m in s.sent if m['type'] == 'timeline_snapshot'][-1] for s in sockets]
        self.assertNotEqual(latest[0]['session_id'], latest[1]['session_id'])
        for owner, other, snapshot in [('A', 'B', latest[0]), ('B', 'A', latest[1])]:
            result = json.dumps(snapshot)
            self.assertIn(owner + '-private', result); self.assertNotIn(other + '-private', result)
            self.assertNotIn('-secret', result); self.assertNotIn('-refresh', result)

    async def test_socket_rejects_renewal_for_another_user(self):
        db = MemoryStore()
        class SwitchedSocket(FakeWebSocket):
            async def receive(self):
                ticket = await issue_ticket(Principal('B', 'family-B', int(time.time()) + 900), db)
                return {'type': 'websocket.receive', 'text': json.dumps({'ticket': ticket})}
        socket = SwitchedSocket(); socket.app.state.users = db
        db.test_drive = FakeDrive()
        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''), patch.object(route, 'AmazonStreamingTranscription', DefaultStream):
            await asyncio.wait_for(route.meeting_stream(socket), timeout=2)
        self.assertEqual(socket.closed['code'], 4401)
        self.assertFalse(socket.received_audio)
        self.assertFalse(any(m.get('items') for m in socket.sent))

    async def test_logout_stops_an_existing_socket_before_processing_next_audio(self):
        db = MemoryStore()
        class RevokedSocket(FakeWebSocket):
            async def send_json(self, message):
                self.sent.append(message)
                if message['type'] == 'timeline_snapshot':
                    await db.put('REVOKED#family-A', {'expiresAt': int(time.time()) + 86400})
        socket = RevokedSocket(); socket.app.state.users = db; db.test_drive = FakeDrive()
        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''), patch.object(route, 'AmazonStreamingTranscription', DefaultStream):
            await asyncio.wait_for(route.meeting_stream(socket), timeout=2)
        self.assertEqual(socket.closed['code'], 4401)
        self.assertFalse(any(m.get('type') == 'transcription_state' for m in socket.sent))

if __name__ == '__main__':
    unittest.main()
