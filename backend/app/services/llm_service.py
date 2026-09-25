from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.services.event_service import EventType, MeetingEvent

_SYSTEM_PROMPTS: dict[EventType, str] = {
    "SEARCH_EXISTING_WORK": (
        "You are given a live meeting context.\n\n"
        "Your job is to produce high-recall, precise search terms for relevant "
        "existing Google Drive files or tracked work items. Search can be "
        "proactive: the speaker does not need to ask to search or open a file.\n\n"
        "Use triggering_context as the primary signal.\n"
        'Use recent_context only to resolve references such as "that issue", '
        '"it", "the theme bug", or a previously mentioned project or deliverable. '
        "Search when the discussion makes a specific existing document, analysis, "
        "plan, report, proposal, spreadsheet, deck, or work item useful now, even "
        "without an explicit retrieval command.\n\n"
        "Preserve distinctive wording and named entities from the meeting. For "
        "example, for 'let's pull up the cost comparison document', keep 'cost "
        "comparison' as a primary phrase; useful alternates could be 'cost "
        "comparison analysis' or 'pricing comparison'. Do not use generic words "
        "like 'document' or 'file' as the only search term, invent a title, or "
        "broaden into unrelated topics.\n\n"
        "Do not invent ticket IDs or details.\n"
        "Return structured JSON only, with exactly these keys: "
        '"primary_terms" (array of strings), '
        '"alternate_terms" (array of strings), '
        '"entities" (array of strings). '
        "Use 1-3 concise, strongest phrase-level primary terms; use a few useful "
        "synonyms or close variants as alternates; and put concrete names, "
        "systems, environments, or components in entities. Keep the user's "
        "original topic phrase as the first primary term.\n\n"
        "If previous_search_terms is present, a search for this topic already "
        "ran with those terms. Put any newly mentioned, more specific retrieval "
        "clue (a likely document or file title, an identifier, a named entity, "
        "or an explicit phrase the speaker used for it) first in primary_terms, "
        "using the speaker's wording. Only repeat previous_search_terms if "
        "nothing more specific was said."
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
        if event.previous_search_terms:
            user_payload["previous_search_terms"] = list(event.previous_search_terms)
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
        data = json.loads(content)
        if event.type == "SEARCH_EXISTING_WORK":
            data = _normalize_search_terms(data)
        usage = payload.get("usage", {})
        return LlmResult(
            event_type=event.type,
            data=data,
            cost=float(usage["cost"]) if usage.get("cost") is not None else None,
        )

    async def close(self) -> None:
        await self._client.aclose()


def _normalize_search_terms(data: object) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        return {"primary_terms": [], "alternate_terms": [], "entities": []}

    normalized: dict[str, list[str]] = {}
    for key in ("primary_terms", "alternate_terms", "entities"):
        value = data.get(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []
        terms: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            term = " ".join(item.split()).strip()
            key_term = term.casefold()
            if term and key_term not in seen and len(term) <= 120:
                seen.add(key_term)
                terms.append(term)
        normalized[key] = terms[:6]
    return normalized
