from __future__ import annotations

from dataclasses import dataclass

import httpx
from app.core.observability import provider_request


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
        response = await provider_request(
            self._client, 'POST', self._endpoint,
            provider='openrouter', stage='jev', model=self._model,
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
                            "Should the meeting assistant search Google Drive, Confluence, and Jira now for an "
                            "existing file or work item that would help with this discussion? "
                            "Use new_context as the current signal. Use recent_context to "
                            "resolve references and identify the specific project, decision, "
                            "or artifact being discussed; recent context alone is not a reason "
                            "to search.\n\n"
                            "Answer yes for either explicit OR implicit retrieval intent. "
                            "Explicit examples: 'let's pull up the cost comparison document', "
                            "'open the rollout deck', or 'find the migration plan'. Do not "
                            "require the speaker to say 'search' or name Google Drive.\n\n"
                            "If the speaker explicitly asks to find, retrieve, locate, open, "
                            "check, or pull up an existing document, file, or task, this is a "
                            "strong yes even when the exact document name is unknown or "
                            "forgotten. Strong yes examples: 'find that document', 'pull up "
                            "the previous file', 'I can't remember the exact document', "
                            "'someone find it for me', 'someone from the team could find it "
                            "for me right now', and 'we need to check something from our "
                            "previous work'. new_context may be only the last fragment of "
                            "such a request (e.g. 'able to recall the exact document if' or "
                            "'could find it for me right now'); use recent_context to "
                            "reconnect it to the document being asked for, and keep the "
                            "answer a strong yes.\n\n"
                            "Also answer yes proactively, without a retrieval command, when "
                            "the conversation points to a specific existing deliverable or "
                            "workstream whose file is likely useful now: for example, asking "
                            "how last month's pricing analysis compared costs, discussing the "
                            "decision in the launch plan, or returning to a named project and "
                            "its prior proposal. Search should make relevant context ready "
                            "before someone has to ask for the file.\n\n"
                            "Existing material includes Docs, Sheets, Slides, PDFs, reports, "
                            "proposals, plans, analyses, designs, budgets, and tracked "
                            "tickets/tasks/issues. A reference to a specific existing "
                            "issue, customer spreadsheet, previously discussed page, "
                            "or view is a strong retrieval signal "
                            "when that item is relevant to the current discussion, even "
                            "without a request to open it. A new task can be suggested "
                            "independently when new work is agreed.\n\n"
                            "A specific ticket ID such as BTS-12 identifies useful existing "
                            "work: 'do not file another one, BTS-12 covers it' should retrieve "
                            "BTS-12 while declining a new task.\n"
                            "Natural meeting examples that warrant retrieval: 'Are we still "
                            "blocked on the checkout timeout?' (existing Jira issue); 'What "
                            "did we decide about rollback in the runbook?' (Confluence); "
                            "'Those costs do not match the budget sheet' (Drive); 'I cannot "
                            "find where we tracked the database error' (Jira). A later "
                            "fragment naming 'the database error' completes that retrieval "
                            "intent even without repeating 'find'. Distinguish a missing "
                            "tracking entry from missing application logs.\n\n"
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
                            "task? Use recent_context to resolve references and speech "
                            "split across transcript windows. Treat a clear request or "
                            "commitment as actionable even when phrased softly, without "
                            "a named owner or deadline. This includes a request to "
                            "follow up with customers, a commitment to run a survey, "
                            "a request to complete it, a request to arrange meetings, "
                            "or a commitment to ask a colleague to send something. "
                            "A reminder to do concrete unfinished work can be a task. "
                            "A later window that clarifies an emerging request is still "
                            "a positive signal; downstream logic handles deduplication. "
                            "Realistic actionable examples: 'The export drops the last row; "
                            "could you take a look after this call?', 'We should add retries "
                            "before launch', 'I will send the updated rollout notes to support', "
                            "and 'Nobody owns the database error yet; let us get it tracked'. "
                            "The work is the investigation, fix, or deliverable, never the act "
                            "of creating a Jira ticket. A bare 'create a ticket for this' is "
                            "actionable only when the underlying work is clear from context. "
                            "Explicitly requesting a ticket for a known problem is a strong "
                            "yes: recent_context 'I have been trying to track the database "
                            "error but cannot find an existing log for it' + new_context "
                            "'we need to create a ticket for this' = yes. Failure to find "
                            "a tracking record is NOT evidence that a ticket already exists. "
                            "This decision offers a draft; only the user's explicit Create "
                            "click submits it to their selected Jira project. Negations and "
                            "corrections override earlier intent: 'do not file it', 'already "
                            "fixed', 'we have BTS-12 for that' are not new tasks. "
                            "Do not answer yes for a goal or status update alone, casual "
                            "discussion, a hypothetical suggestion, or completed work. "
                            "Do not answer "
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
