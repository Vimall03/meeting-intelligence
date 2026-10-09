import json
import unittest

import httpx

from app.services.event_service import MeetingEvent
from app.services.llm_service import LlmService


class LlmServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_intent_review_uses_text_only_and_requires_real_boolean_answers(self):
        for data in (
            {"search_existing_work": False, "suggest_new_task": True, "task_action": "Finish the requested work", "needs_more_context": False},
            {"search_existing_work": "false", "suggest_new_task": True},
            {"search_existing_work": False},
            {"search_existing_work": False, "suggest_new_task": True, "task_action": "", "needs_more_context": False},
        ):
            with self.subTest(data=data):
                def respond(request):
                    payload = json.loads(request.content)
                    speech = json.loads(payload["messages"][1]["content"])
                    self.assertEqual(speech, {"recent_context": "unfinished work", "new_context": "by Thursday"})
                    self.assertEqual(payload["max_tokens"], 192)
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}],
                        "usage": {"cost": "private provider diagnostics"}})
                service = LlmService(api_key="test-key", transport=httpx.MockTransport(respond))
                try:
                    if all(type(data.get(key)) is bool for key in ("search_existing_work", "suggest_new_task", "needs_more_context")) and data.get("task_action"):
                        with self.assertLogs('meeting_intelligence', level='INFO') as captured:
                            result = await service.review_intent("unfinished work", "by Thursday")
                        self.assertFalse(result.search_existing_work)
                        self.assertTrue(result.suggest_new_task)
                        from app.core.observability import JsonFormatter
                        encoded = json.dumps([JsonFormatter().format(record) for record in captured.records])
                        self.assertNotIn("private provider diagnostics", encoded)
                        self.assertNotIn("unfinished work", encoded)
                    else:
                        with self.assertRaises(ValueError):
                            await service.review_intent("unfinished work", "by Thursday")
                finally:
                    await service.close()

    async def test_sends_event_specific_prompt_and_parses_json_response(self) -> None:
        event = MeetingEvent(
            type="SEARCH_EXISTING_WORK",
            score=0.82,
            triggered_at=95.8,
            recent_context="Earlier discussion about the theme bug.",
            triggering_context="Is that theme issue still open?",
        )

        async def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "google/gemma-4-26b-a4b-it")
            self.assertEqual(payload['provider'], {'sort': 'latency'})
            self.assertEqual(payload['max_tokens'], 256)
            system_message = payload["messages"][0]
            self.assertEqual(system_message["role"], "system")
            self.assertIn("existing Google Drive files", system_message["content"])
            self.assertIn("cost comparison", system_message["content"])
            user_message = json.loads(payload["messages"][1]["content"])
            self.assertEqual(
                user_message["event"],
                {
                    "type": "SEARCH_EXISTING_WORK",
                    "score": 0.82,
                    "triggered_at": 95.8,
                },
            )
            self.assertEqual(user_message["recent_context"], event.recent_context)
            self.assertEqual(user_message["triggering_context"], event.triggering_context)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "primary_terms": ["theme bug"],
                                        "alternate_terms": ["theme issue"],
                                        "entities": ["theme"],
                                    }
                                ),
                            }
                        }
                    ],
                    "usage": {"cost": 0.0002},
                },
            )

        service = LlmService(
            api_key="test-key",
            transport=httpx.MockTransport(respond),
        )
        try:
            result = await service.process_event(event)
        finally:
            await service.close()

        self.assertEqual(result.event_type, "SEARCH_EXISTING_WORK")
        self.assertEqual(result.data["primary_terms"], ["theme bug"])
        self.assertEqual(result.data["alternate_terms"], ["theme issue"])
        self.assertEqual(result.data["entities"], ["theme"])
        self.assertEqual(result.cost, 0.0002)

    async def test_uses_new_task_prompt_for_suggest_new_task_events(self) -> None:
        event = MeetingEvent(
            type="SUGGEST_NEW_TASK",
            score=0.87,
            triggered_at=12.3,
            recent_context="recent",
            triggering_context="Okay, let's create a ticket.",
        )

        async def respond(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            system_message = payload["messages"][0]["content"]
            self.assertIn("identify the concrete new work", system_message)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "task_title": "Investigate database error",
                                        "task_description": "...",
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {},
                },
            )

        service = LlmService(
            api_key="test-key",
            transport=httpx.MockTransport(respond),
        )
        try:
            result = await service.process_event(event)
        finally:
            await service.close()

        self.assertEqual(result.data["task_title"], "Investigate database error")
        self.assertEqual(result.data["task_description"], "...")
        self.assertIsNone(result.cost)

    async def test_normalizes_incomplete_task_result(self) -> None:
        event = MeetingEvent(
            type="SUGGEST_NEW_TASK",
            score=0.8,
            triggered_at=1,
            recent_context="context",
            triggering_context="Send it tomorrow.",
        )
        service = LlmService(
            api_key="test-key",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": json.dumps({"task_title": " Send it "})}}]},
                )
            ),
        )
        try:
            result = await service.process_event(event)
        finally:
            await service.close()
        self.assertEqual(result.data, {"task_title": "Send it", "task_description": ""})

    async def test_retries_administrative_title_then_accepts_underlying_work(self) -> None:
        calls = []
        def respond(request):
            calls.append(json.loads(request.content))
            title = "Create Jira ticket for database error" if len(calls) == 1 else "Investigate database error"
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"task_title": title, "task_description": "Investigate the reported database error."})}}], "usage": {"cost": .001}})
        service = LlmService("test", transport=httpx.MockTransport(respond))
        try:
            result = await service.process_event(MeetingEvent(type="SUGGEST_NEW_TASK", score=.9, triggered_at=0, recent_context="The database error has no ticket.", triggering_context="Create one for this."))
        finally:
            await service.close()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.data["task_title"], "Investigate database error")
        self.assertEqual(result.cost, .002)

    async def test_repeated_bad_draft_is_not_shown_as_a_task(self) -> None:
        service = LlmService("test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"task_title": "Create ticket", "task_description": "Create a Jira ticket."})}}]})))
        try:
            result = await service.process_event(MeetingEvent(type="SUGGEST_NEW_TASK", score=.9, triggered_at=0, recent_context="", triggering_context="Create a ticket."))
        finally:
            await service.close()
        self.assertEqual(result.data, {"task_title": "", "task_description": ""})


if __name__ == "__main__":
    unittest.main()
