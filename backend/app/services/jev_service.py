from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class JevDecision:
    search_existing_work_probability: float
    suggest_new_task_probability: float
    cost: float | None = None


class JevService:
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
    ) -> JevDecision:
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
                            "Should the meeting assistant search Google Drive now for an "
                            "existing file or work item that would help with this discussion? "
                            "Use new_context as the current signal. Use recent_context to "
                            "resolve references and identify the specific project, decision, "
                            "or artifact being discussed; recent context alone is not a reason "
                            "to search.\n\n"
                            "Answer yes for either explicit OR implicit retrieval intent. "
                            "Explicit examples: 'let's pull up the cost comparison document', "
                            "'open the rollout deck', or 'find the migration plan'. Do not "
                            "require the speaker to say 'search' or name Google Drive.\n\n"
                            "Also answer yes proactively, without a retrieval command, when "
                            "the conversation points to a specific existing deliverable or "
                            "workstream whose file is likely useful now: for example, asking "
                            "how last month's pricing analysis compared costs, discussing the "
                            "decision in the launch plan, or returning to a named project and "
                            "its prior proposal. Search should make relevant context ready "
                            "before someone has to ask for the file.\n\n"
                            "Existing material includes Docs, Sheets, Slides, PDFs, reports, "
                            "proposals, plans, analyses, designs, budgets, and tracked "
                            "tickets/tasks/issues. A new task can be suggested independently "
                            "when new work is agreed.\n\n"
                            "Answer no for generic conversation or brainstorming with no "
                            "specific existing artifact/work item likely to help, a merely "
                            "hypothetical reference, or a request only to create a new file "
                            "with no useful existing source. Do not answer yes just because "
                            "a problem or a common noun is mentioned."
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
                            "already exists as a tracked ticket or task. Do not classify "
                            "requests to find, open, retrieve, search for, pull up, or "
                            "show an existing file, ticket, or document as new work; "
                            "those are retrieval actions, not new tasks."
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
        return JevDecision(
            search_existing_work_probability=search_probability,
            suggest_new_task_probability=task_probability,
            cost=float(usage["cost"]) if usage.get("cost") is not None else None,
        )

    async def close(self) -> None:
        await self._client.aclose()
