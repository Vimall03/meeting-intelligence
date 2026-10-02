import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import WebSocketDisconnect

from app.services.jev_service import JevDecision
from app.services.llm_service import LlmResult


route = importlib.import_module("app.api.routes.meeting_stream")


class FakeWebSocket:
    def __init__(self, *, drive=None, finish_on_empty_search: bool = False) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(google_drive=drive or FakeDrive()))
        self.sent: list[dict] = []
        self.finished = asyncio.Event()
        self.received_audio = False
        self.finish_on_empty_search = finish_on_empty_search

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
                    patch.object(route.settings, "openrouter_api_key", "test-key"),
                    patch.object(route, "JevService", FakeJev),
                    patch.object(route, "LlmService", FakeLlm),
                    patch.object(route, "transcribe_snapshot", transcribe),
                ):
                    await asyncio.wait_for(route.meeting_stream(websocket), timeout=5)
                updates = [message for message in websocket.sent if message["type"] == "transcription_state"]
                self.assertEqual(len(updates), 2)
                self.assertIn("next topic", updates[-1]["transcript"])


if __name__ == "__main__":
    unittest.main()
