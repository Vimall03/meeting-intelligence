from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class JevRoutingDecision:
    search_existing_work_probability: float
    suggest_new_task_probability: float
    cost: float | None = None


class JevRoutingService:
    def __init__(
        self,
        api_key: str,
        model: str = "typesafe/jev-1.13",
        endpoint: str = "https://openrouter.ai/api/alpha/decisions",
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def decide(
        self, recent_context: str, new_context: str
    ) -> JevRoutingDecision:
        response = await self._client.post(
            self._endpoint,
            json={
                "model": self._model,
                "state": {
                    "recent_context": recent_context,
                    "new_context": new_context,
                },
                "questions": {
                    "should_search_existing_work": {
                        "type": "noul",
                        "instructions": (
                            "Based primarily on new_context, would it be useful right now "
                            "to search for an existing work item related to what is being "
                            "discussed? Use recent_context only to resolve references and "
                            "understand context. Answer yes when the conversation indicates "
                            "that the work already exists, was previously raised, is "
                            "already being worked on, has a ticket/task/issue, has an "
                            "identifier, or someone is discussing its status or completion. "
                            "Do not answer yes merely because someone mentions a new "
                            "problem."
                        ),
                    },
                    "should_suggest_new_task": {
                        "type": "noul",
                        "instructions": (
                            "Based primarily on new_context, has the conversation just "
                            "created, requested, assigned, agreed to, or committed to "
                            "concrete new work that would be useful to capture as a new "
                            "task? Use recent_context to resolve references. Do not answer "
                            "yes if the conversation clearly indicates that this work "
                            "already exists as a tracked ticket or task."
                        ),
                    },
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers", {})
        search_probability = float(
            answers.get("should_search_existing_work", {}).get("noul", 0.0)
        )
        task_probability = float(
            answers.get("should_suggest_new_task", {}).get("noul", 0.0)
        )
        usage = payload.get("usage", {})
        return JevRoutingDecision(
            search_existing_work_probability=search_probability,
            suggest_new_task_probability=task_probability,
            cost=float(usage["cost"]) if usage.get("cost") is not None else None,
        )

    async def close(self) -> None:
        await self._client.aclose()