from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.services.jev_service import JevDecision

EventType = Literal["SEARCH_EXISTING_WORK", "SUGGEST_NEW_TASK"]

# A Jev threshold crossing does not fire Gemma immediately. It opens a short
# enrichment window so a half sentence like "where the user needs to be" can
# pick up the next few words before we spend a Gemma call on it.
SILENCE_FINALIZE_SECONDS = 1.5
WORD_COUNT_FINALIZE_THRESHOLD = 10
HARD_CAP_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class MeetingEvent:
    """A finalized, enriched event ready to be sent to the LLM."""

    type: EventType
    score: float
    triggered_at: float
    recent_context: str
    triggering_context: str


@dataclass(slots=True)
class _PendingEvent:
    type: EventType
    score: float
    started_at: float
    last_update_at: float
    text_parts: list[str] = field(default_factory=list)
    new_word_count: int = 0


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "?", "!"))


class EventService:
    """Thresholds + dedupes Jev decisions, then buffers a short enrichment
    window before finalizing so Gemma is called once per event with a
    complete-enough thought instead of on every Jev evaluation."""

    def __init__(
        self,
        search_threshold: float,
        task_threshold: float,
        dedupe_window_seconds: float,
    ) -> None:
        self._thresholds: dict[EventType, float] = {
            "SEARCH_EXISTING_WORK": search_threshold,
            "SUGGEST_NEW_TASK": task_threshold,
        }
        self._dedupe_window_seconds = dedupe_window_seconds
        self._last_emitted_at: dict[EventType, float] = {}
        self._pending: dict[EventType, _PendingEvent] = {}

    def pending_types(self) -> list[EventType]:
        return list(self._pending)

    def start_pending(
        self,
        decision: JevDecision,
        recent_context: str,
        triggering_context: str,
        now: float,
    ) -> list[MeetingEvent]:
        """Open (or ignore) enrichment windows for thresholds crossed by a
        Jev decision. Returns events that already qualify to finalize
        immediately, e.g. the triggering text is already a full sentence."""
        candidates: tuple[tuple[EventType, float], ...] = (
            ("SEARCH_EXISTING_WORK", decision.search_existing_work_probability),
            ("SUGGEST_NEW_TASK", decision.suggest_new_task_probability),
        )
        for event_type, probability in candidates:
            if probability < self._thresholds[event_type]:
                continue
            if event_type in self._pending:
                continue
            last_emitted_at = self._last_emitted_at.get(event_type)
            if (
                last_emitted_at is not None
                and now - last_emitted_at < self._dedupe_window_seconds
            ):
                continue
            self._pending[event_type] = _PendingEvent(
                type=event_type,
                score=probability,
                started_at=now,
                last_update_at=now,
                text_parts=[triggering_context],
                new_word_count=len(triggering_context.split()),
            )
        return self._finalize_ready(
            now, recent_context=recent_context, just_appended=triggering_context
        )

    def enrich(
        self, new_text: str, recent_context: str, now: float
    ) -> list[MeetingEvent]:
        """Feed newly transcribed text into any open enrichment windows."""
        if not self._pending or not new_text.strip():
            return []
        for pending in self._pending.values():
            pending.text_parts.append(new_text)
            pending.last_update_at = now
            pending.new_word_count += len(new_text.split())
        return self._finalize_ready(
            now, recent_context=recent_context, just_appended=new_text
        )

    def check_timeouts(self, recent_context: str, now: float) -> list[MeetingEvent]:
        """Finalize any pending events that have gone silent or hit the hard cap."""
        if not self._pending:
            return []
        return self._finalize_ready(now, recent_context=recent_context, just_appended=None)

    def _finalize_ready(
        self,
        now: float,
        recent_context: str,
        just_appended: str | None,
    ) -> list[MeetingEvent]:
        events: list[MeetingEvent] = []
        for event_type in list(self._pending):
            pending = self._pending[event_type]
            silence_seconds = now - pending.last_update_at
            age_seconds = now - pending.started_at
            should_finalize = (
                silence_seconds >= SILENCE_FINALIZE_SECONDS
                or pending.new_word_count >= WORD_COUNT_FINALIZE_THRESHOLD
                or (just_appended is not None and _ends_sentence(just_appended))
                or age_seconds >= HARD_CAP_SECONDS
            )
            if not should_finalize:
                continue
            del self._pending[event_type]
            self._last_emitted_at[event_type] = now
            events.append(
                MeetingEvent(
                    type=event_type,
                    score=pending.score,
                    triggered_at=now,
                    recent_context=recent_context,
                    triggering_context=" ".join(pending.text_parts),
                )
            )
        return events
