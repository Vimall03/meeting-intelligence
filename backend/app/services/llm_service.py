from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.services.event_service import EventType, MeetingEvent

_SYSTEM_PROMPTS: dict[EventType, str] = {
    "SEARCH_EXISTING_WORK": (
        "You are given a live meeting context.\n\n"
        "Your job is to extract only the information needed to search for an "
        "existing work item.\n\n"
        "Use triggering_context as the primary signal.\n"
        'Use recent_context only to resolve references such as "that issue", '
        '"it", or "the theme bug".\n\n'
        "Do not invent ticket IDs or details.\n"
        "Return structured JSON only, with exactly these keys: "
        '"search_query" (string), "referenced_entities" (array of strings).'
    ),
    "SUGGEST_NEW_TASK": (
        "You are given a live meeting context.\n\n"
        "Your job is to identify the concrete new work that emerged from the "
        "conversation.\n\n"
        "Use triggering_context as the primary signal.\n"
        "Use recent_context only to resolve references and understand the "
        "task.\n\n"
        "Do not invent details.\n"
        "Return structured JSON only, with exactly these keys: "
        '"task_title" (string), "task_description" (string).'
    ),
}


@dataclass(frozen=True, slots=True)
class LlmResult:
    event_type: EventType
    data: dict
    cost: float | None = None


class LlmService:
    """Calls Gemma (via OpenRouter) with an event-specific prompt and context."""

    def __init__(
        self,
        api_key: str,
        model: str = "google/gemma-4-26b-a4b-it",
        endpoint: str = "https://openrouter.ai/api/v1/chat/completions",
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def process_event(self, event: MeetingEvent) -> LlmResult:
        user_payload = {
            "event": {
                "type": event.type,
                "score": event.score,
                "triggered_at": event.triggered_at,
            },
            "recent_context": event.recent_context,
            "triggering_context": event.triggering_context,
        }
        response = await self._client.post(
            self._endpoint,
            json={
                "model": self._model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPTS[event.type]},
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        return LlmResult(
            event_type=event.type,
            data=json.loads(content),
            cost=float(usage["cost"]) if usage.get("cost") is not None else None,
        )

    async def close(self) -> None:
        await self._client.aclose()
