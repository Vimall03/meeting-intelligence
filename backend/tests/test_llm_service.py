import json
import unittest

import httpx

from app.services.event_service import MeetingEvent
from app.services.llm_service import LlmService


class LlmServiceTests(unittest.IsolatedAsyncioTestCase):
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
                                        "task_title": "Create ticket",
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

        self.assertEqual(result.data["task_title"], "Create ticket")
        self.assertIsNone(result.cost)


if __name__ == "__main__":
    unittest.main()
