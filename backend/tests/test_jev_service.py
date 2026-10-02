import json
import unittest

import httpx

from app.services.jev_service import JevService


class JevServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_context_and_parses_noul_probabilities(self) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "typesafe/jev-1.13")
            self.assertEqual(payload["state"]["recent_context"], "Earlier context")
            self.assertEqual(payload["state"]["new_context"], "New task")
            self.assertEqual(
                set(payload["questions"]),
                {"should_search_existing_work", "should_suggest_new_task"},
            )
            self.assertEqual(payload["questions"]["should_search_existing_work"]["type"], "noul")
            self.assertEqual(payload["questions"]["should_suggest_new_task"]["type"], "noul")
            search_instructions = payload["questions"]["should_search_existing_work"]["instructions"]
            self.assertIn("explicit OR implicit retrieval intent", search_instructions)
            self.assertIn("let's pull up the cost comparison document", search_instructions)
            self.assertIn("answer yes proactively", search_instructions)
            self.assertIn("strong yes even when the exact document name is unknown", search_instructions)
            self.assertIn("Answer no for generic conversation", search_instructions)
            return httpx.Response(
                200,
                json={
                    "answers": {
                        "should_search_existing_work": {"type": "noul", "noul": 0.06},
                        "should_suggest_new_task": {"type": "noul", "noul": 0.91},
                    },
                    "usage": {"cost": 0.00001},
                },
            )

        service = JevService(
            api_key="test-key",
            transport=httpx.MockTransport(respond),
        )
        try:
            decision = await service.decide("Earlier context", "New task")
        finally:
            await service.close()

        self.assertEqual(decision.search_existing_work_probability, 0.06)
        self.assertEqual(decision.suggest_new_task_probability, 0.91)
        self.assertEqual(decision.cost, 0.00001)

    async def test_defaults_missing_answers_to_zero(self) -> None:
        service = JevService(
            api_key="test-key",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"answers": {}})
            ),
        )
        try:
            decision = await service.decide("Context", "New context")
        finally:
            await service.close()

        self.assertEqual(decision.search_existing_work_probability, 0.0)
        self.assertEqual(decision.suggest_new_task_probability, 0.0)
        self.assertIsNone(decision.cost)


if __name__ == "__main__":
    unittest.main()
