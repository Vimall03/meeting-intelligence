from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class MeetingTimelineService:
    """Ordered result history for one live meeting WebSocket session."""

    def __init__(self) -> None:
        self.session_id = uuid4().hex
        self._items: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "timeline_snapshot",
            "session_id": self.session_id,
            "revision": len(self._items),
            "items": deepcopy(list(reversed(self._items))),
        }

    def add_task(self, *, title: str, description: str, confidence: float) -> dict[str, Any]:
        title = " ".join(title.split())
        description = " ".join(description.split())
        if not title or not description:
            raise ValueError("Task suggestions need both a title and description")
        # Separate tasks may emerge seconds apart. Dedupe the draft itself,
        # not every task event for a fixed period after the first suggestion.
        for item in self._items:
            if item["kind"] == "task_suggestion" and item["title"].casefold() == title.casefold():
                return deepcopy(item)
        return self._append(
            {
                "kind": "task_suggestion",
                "title": title,
                "description": description,
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )

    def add_search(
        self,
        *,
        query: str,
        files: list[dict[str, Any]],
        error: str | None = None,
        source_id: str = "google_drive",
        source_label: str = "Google Drive",
    ) -> dict[str, Any] | None:
        if not files:
            return None
        return self._append(
            {
                "kind": "search_results",
                "source": {"id": source_id, "label": source_label},
                "query": query,
                "files": deepcopy(files),
                "error": error,
            }
        )

    def _append(self, data: dict[str, Any]) -> dict[str, Any]:
        sequence = len(self._items) + 1
        item = {
            "id": f"{self.session_id}:{sequence}",
            "sequence": sequence,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **data,
        }
        self._items.append(item)
        return item
