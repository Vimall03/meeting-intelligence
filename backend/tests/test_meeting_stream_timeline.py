import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import asynccontextmanager
from app.core.auth import Principal, issue_ticket
from test_auth_integration import MemoryStore
import time, json

from fastapi import WebSocketDisconnect

from app.services.jev_service import JevDecision
from app.services.llm_service import LlmResult


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
        return json.dumps({'ticket': await issue_ticket(Principal(user, 'family-' + user, int(time.time()) + 900), self.app.state.users)})
    async def receive(self):
        return {'type': 'websocket.receive', 'bytes': await self.receive_bytes()}
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

    async def decide(self, _recent: str, _new: str) -> JevDecision:
        return JevDecision(0.9, 0.9)

    async def close(self) -> None:
        pass


class FakeLlm:
    def __init__(self, **_kwargs) -> None:
        pass

    async def process_event(self, event) -> LlmResult:
        if event.type == "SUGGEST_NEW_TASK":
            data = {"task_title": "Send launch plan", "task_description": "Send the plan to Maya."}
        else:
            data = {"primary_terms": ["launch plan"], "alternate_terms": [], "entities": []}
        return LlmResult(event_type=event.type, data=data)

    async def close(self) -> None:
        pass


class MeetingStreamTimelineTests(unittest.IsolatedAsyncioTestCase):
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
            patch.object(route, "transcribe_snapshot", transcribe),
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
            patch.object(route, "transcribe_snapshot", transcribe),
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
                    async def receive_bytes(self):
                        if not self.received_audio:
                            self.received_audio = True
                            return b"first"
                        if self.next_audio:
                            self.next_audio = False
                            return b"later"
                        await self.finished.wait()
                        raise WebSocketDisconnect()

                    async def send_json(self, message):
                        self.sent.append(message)
                        if message.get("type") == "timeline_snapshot" and message.get("revision") == 2:
                            self.next_audio = True
                        if message.get("type") == "transcription_state" and "next topic" in message["transcript"]:
                            self.finished.set()

                websocket = ContinuingSocket()
                websocket.app.state.atlassian = FailingAtlassian()
                async def transcribe(audio):
                    return {"audio_duration": 3, "segments": [{"start": 0, "end": 3, "text": "Please send the launch plan to Maya." if audio == b"first" else "Now the next topic is checkout."}]}

                with (
                    patch.object(route, 'provider_session', scoped_provider),
                    patch.object(websocket.app.state.users, 'test_drive', websocket.app.state.google_drive, create=True),
            patch.object(websocket.app.state.users, 'test_atlassian', getattr(websocket.app.state, 'atlassian', None), create=True),
            patch.object(route.settings, "openrouter_api_key", "test-key"),
                    patch.object(route, "JevService", FakeJev),
                    patch.object(route, "LlmService", FakeLlm),
                    patch.object(route, "transcribe_snapshot", transcribe),
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
        with patch.object(route, 'provider_session', real_provider), patch.object(GoogleDriveService, '__init__', init), patch.object(route.settings, 'openrouter_api_key', 'test-key'), patch.object(route, 'JevService', FakeJev), patch.object(route, 'LlmService', FakeLlm), patch.object(route, 'transcribe_snapshot', transcribe):
            await asyncio.wait_for(asyncio.gather(*(route.meeting_stream(s) for s in sockets)), timeout=5)
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
        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''):
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
        with patch.object(route, 'provider_session', scoped_provider), patch.object(route.settings, 'openrouter_api_key', ''):
            await asyncio.wait_for(route.meeting_stream(socket), timeout=2)
        self.assertEqual(socket.closed['code'], 4401)
        self.assertFalse(any(m.get('type') == 'transcription_state' for m in socket.sent))

if __name__ == '__main__':
    unittest.main()
