from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.services.jev_service import JevDecision
from app.core.observability import log_event

EventType = Literal["SEARCH_EXISTING_WORK", "SUGGEST_NEW_TASK"]

# A Jev threshold crossing does not fire Gemma immediately. It opens a short
# enrichment window so a half sentence like "where the user needs to be" can
# pick up the next few words before we spend a Gemma call on it.
SILENCE_FINALIZE_SECONDS = 1.5
WORD_COUNT_FINALIZE_THRESHOLD = 10
HARD_CAP_SECONDS = 4.0
# When the text trails off ("I think it was something"), wait for roughly one
# more ~3s STT chunk before finalizing.
INCOMPLETE_SILENCE_SECONDS = 4.0
INCOMPLETE_HARD_CAP_SECONDS = 6.0

# After a search fires, its topic stays open briefly so a stronger clue said a
# moment later (e.g. the actual document title) can refine the query.
SEARCH_SESSION_SECONDS = 20.0
MIN_REFINEMENT_INTERVAL_SECONDS = 2.5
MAX_REFINEMENTS_PER_SESSION = 1

# Words too generic to decide whether two searches share a topic.
_TOPIC_STOPWORDS = frozenset(
    {
        "a", "an", "and", "the", "for", "of", "on", "to", "in", "with", "about",
        "doc", "docs", "document", "documents", "file", "files", "page", "pages",
        "sheet", "sheets", "spreadsheet", "slide", "slides", "deck", "report",
        "notes", "draft", "version", "latest", "new", "old", "update", "updates",
        "updated",
    }
)

_INCOMPLETE_TRAILING_WORDS = frozenset(
    {
        "a", "about", "an", "and", "around", "as", "because", "but", "called",
        "for", "from", "in", "is", "it's", "like", "maybe", "named", "of", "on",
        "or", "related", "something", "that", "the", "think", "titled", "to",
        "uh", "um", "was", "where", "which", "with",
    }
)
_TICKET_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_RETRIEVAL_CUE_PATTERN = re.compile(
    r"[\"“][^\"”]+[\"”]"
    r"|\b(?:called|named|titled)\b"
    r"|\bsomething\s+(?:around|like|about|along)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MeetingEvent:
    """A finalized, enriched event ready to be sent to the LLM."""

    type: EventType
    score: float
    triggered_at: float
    recent_context: str
    triggering_context: str
    is_refinement: bool = False
    previous_search_terms: tuple[str, ...] = ()
    # Raised while the active search topic is frozen; only searches if the
    # resulting terms turn out to be a different topic.
    is_topic_check: bool = False
    background_context: str = ""


@dataclass(slots=True)
class _PendingEvent:
    type: EventType
    score: float
    started_at: float
    last_update_at: float
    text_parts: list[str] = field(default_factory=list)
    new_word_count: int = 0
    is_refinement: bool = False
    is_topic_check: bool = False
    background_context: str = ""


@dataclass(slots=True)
class _SearchSession:
    score: float
    last_search_at: float
    last_text: str
    last_activity_at: float
    refinements: int = 0
    searched_terms: list[str] = field(default_factory=list)


def _topic_tokens(terms: Sequence[str]) -> set[str]:
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", " ".join(terms).casefold()):
        # Keep quarters, years and ticket numbers: PAY-42 and PAY-43 are
        # distinct targets even though the project prefix is identical.
        if token in _TOPIC_STOPWORDS or (len(token) < 3 and not any(c.isdigit() for c in token)):
            continue
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        tokens.add(token)
    return tokens


def _shares_topic(new_terms: Sequence[str], known_terms: Sequence[str]) -> bool:
    known = _topic_tokens(known_terms)
    new = _topic_tokens(new_terms)
    matched = set()
    for token in new:
        for known_token in known:
            if token == known_token:
                matched.add(token)
                break
            shorter, longer = sorted((token, known_token), key=len)
            # e.g. "chart" / "flowchart"
            if len(shorter) >= 4 and shorter in longer:
                matched.add(token)
                break
    # One shared word such as "budget" must not freeze a different budget.
    # A short reference to the already searched artifact can still match.
    return bool(new) and len(matched) / len(new) >= 0.8


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "?", "!"))


def _ends_incomplete(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped or _ends_sentence(stripped):
        return False
    if stripped.endswith((",", ":", "-")):
        return True
    last_word = stripped.split()[-1].strip("\"'“”").casefold()
    return last_word in _INCOMPLETE_TRAILING_WORDS


def _count_retrieval_clues(text: str) -> int:
    return len(_TICKET_KEY_PATTERN.findall(text)) + len(
        _RETRIEVAL_CUE_PATTERN.findall(text)
    )


def _adds_retrieval_clue(previous_text: str, new_text: str) -> bool:
    # Joined with the previous chunk so cues split across chunks
    # ("it was something" / "around cost comparison") are still seen.
    joined = f"{previous_text} {new_text}"
    return _count_retrieval_clues(joined) > _count_retrieval_clues(previous_text)


class EventService:
    """Thresholds + dedupes Jev decisions, then buffers a short enrichment
    window before finalizing so Gemma is called once per event with a
    complete-enough thought instead of on every Jev evaluation.

    A finalized search opens a short-lived search session: while it is
    active, later Jev crossings or explicit retrieval clues produce a limited
    number of refinement events instead of being deduped away."""

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
        self._recent_task_triggers: list[tuple[float, str]] = []
        self._pending: dict[EventType, _PendingEvent] = {}
        self._search_session: _SearchSession | None = None
        self._recent_search_topics: list[tuple[float, frozenset[str]]] = []
        self.last_search_terms_suppression_reason: str | None = None

    def pending_types(self) -> list[EventType]:
        return list(self._pending)

    def accept_search_terms(
        self,
        primary_terms: Sequence[str],
        all_terms: Sequence[str],
        is_refinement: bool,
        now: float,
        is_topic_check: bool = False,
    ) -> bool:
        """Record terms from Gemma and say whether they merit a Drive search.
        Refinements only search when they add a new primary term. Topic checks
        only search (and open a new session) when the topic materially changed."""
        self.last_search_terms_suppression_reason = None
        if is_topic_check:
            session = self._active_search_session(now)
            if session is not None and _shares_topic(
                primary_terms, session.searched_terms
            ):
                session.last_activity_at = now
                self.last_search_terms_suppression_reason = "search_topic_frozen"
                return False
        self._recent_search_topics[:] = [
            (searched_at, topic)
            for searched_at, topic in self._recent_search_topics
            if now - searched_at < self._dedupe_window_seconds
        ]
        topic = frozenset(
            _topic_tokens(primary_terms)
        )
        if topic:
            for _, previous_topic in self._recent_search_topics:
                shared_terms = topic & previous_topic
                larger_topic_size = max(len(topic), len(previous_topic))
                if topic == previous_topic or (
                    len(shared_terms) >= 2
                    and len(shared_terms) / larger_topic_size >= 0.8
                ):
                    self.last_search_terms_suppression_reason = "duplicate_search_topic"
                    return False

        if is_topic_check:
            previous = self._search_session
            self._search_session = _SearchSession(
                score=previous.score if previous is not None else 0.0,
                last_search_at=now,
                last_text="",
                last_activity_at=now,
            )

        session = self._active_search_session(now)
        if session is None:
            if topic:
                self._recent_search_topics.append((now, topic))
            return True
        known = {term.casefold() for term in session.searched_terms}
        if is_refinement and all(term.casefold() in known for term in primary_terms):
            self.last_search_terms_suppression_reason = "duplicate_search_topic"
            return False
        for term in all_terms:
            if term.casefold() not in known:
                known.add(term.casefold())
                session.searched_terms.append(term)
        if topic:
            self._recent_search_topics.append((now, topic))
        return True

    def _active_search_session(self, now: float) -> _SearchSession | None:
        session = self._search_session
        if session is None or now - session.last_activity_at >= SEARCH_SESSION_SECONDS:
            return None
        return session

    @staticmethod
    def _can_refine(session: _SearchSession, now: float) -> bool:
        return (
            session.refinements < MAX_REFINEMENTS_PER_SESSION
            and now - session.last_search_at >= MIN_REFINEMENT_INTERVAL_SECONDS
        )

    def start_pending(
        self,
        decision: JevDecision,
        recent_context: str,
        triggering_context: str,
        now: float,
        *, background_context: str = "",
    ) -> list[MeetingEvent]:
        """Open (or ignore) enrichment windows for thresholds crossed by a
        Jev decision. Returns events that already qualify to finalize
        immediately, e.g. the triggering text is already a full sentence."""
        candidates: tuple[tuple[EventType, float, bool], ...] = (
            ("SEARCH_EXISTING_WORK", decision.search_existing_work_probability, decision.search_confirmed),
            ("SUGGEST_NEW_TASK", decision.suggest_new_task_probability, decision.task_confirmed),
        )
        for event_type, probability, confirmed in candidates:
            if probability < self._thresholds[event_type] and not confirmed:
                continue
            if event_type in self._pending:
                log_event('meeting.event.suppressed', action=event_type, confidence=probability, reason='event_already_pending')
                continue
            is_refinement = False
            is_topic_check = False
            session = (
                self._active_search_session(now)
                if event_type == "SEARCH_EXISTING_WORK"
                else None
            )
            if session is not None:
                if session.refinements >= MAX_REFINEMENTS_PER_SESSION:
                    log_event('meeting.topic.check', action=event_type, confidence=probability, reason='refinement_limit_reached')
                    is_topic_check = True
                elif not self._can_refine(session, now):
                    # Determine the topic before applying the cooldown. A
                    # different request can arrive immediately after a search.
                    is_topic_check = True
                else:
                    is_refinement = True
            elif event_type == "SUGGEST_NEW_TASK":
                self._recent_task_triggers[:] = [(at, text) for at, text in self._recent_task_triggers if now - at < self._dedupe_window_seconds]
                trigger = " ".join(triggering_context.casefold().split())
                if any(trigger == text for _, text in self._recent_task_triggers):
                    continue
            self._pending[event_type] = _PendingEvent(
                type=event_type,
                score=probability,
                started_at=now,
                last_update_at=now,
                text_parts=[triggering_context],
                new_word_count=len(triggering_context.split()),
                is_refinement=is_refinement,
                is_topic_check=is_topic_check,
                background_context=background_context,
            )
        return self._finalize_ready(
            now, recent_context=recent_context, just_appended=triggering_context
        )

    def enrich(
        self, new_text: str, recent_context: str, now: float, *, background_context: str = ""
    ) -> list[MeetingEvent]:
        """Feed newly transcribed text into any open enrichment windows, and
        open a refinement window if it adds a retrieval clue to an active
        search session."""
        if not new_text.strip():
            return []
        for pending in self._pending.values():
            if background_context:
                pending.background_context = background_context
            pending.text_parts.append(new_text)
            pending.last_update_at = now
            pending.new_word_count += len(new_text.split())
        session = self._active_search_session(now)
        if session is not None:
            if (
                "SEARCH_EXISTING_WORK" not in self._pending
                and self._can_refine(session, now)
                and _adds_retrieval_clue(session.last_text, new_text)
            ):
                text_parts = [session.last_text, new_text]
                self._pending["SEARCH_EXISTING_WORK"] = _PendingEvent(
                    type="SEARCH_EXISTING_WORK",
                    score=session.score,
                    started_at=now,
                    last_update_at=now,
                    text_parts=text_parts,
                    new_word_count=len(" ".join(text_parts).split()),
                    is_refinement=True,
                    background_context=background_context,
                )
            session.last_text = new_text
        if not self._pending:
            return []
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
            triggering_context = " ".join(pending.text_parts)
            if _ends_incomplete(triggering_context):
                should_finalize = (
                    silence_seconds >= INCOMPLETE_SILENCE_SECONDS
                    or age_seconds >= INCOMPLETE_HARD_CAP_SECONDS
                )
            else:
                should_finalize = (
                    silence_seconds >= SILENCE_FINALIZE_SECONDS
                    or pending.new_word_count >= WORD_COUNT_FINALIZE_THRESHOLD
                    or (just_appended is not None and _ends_sentence(just_appended))
                    or age_seconds >= HARD_CAP_SECONDS
                )
            if not should_finalize:
                continue
            del self._pending[event_type]
            if event_type == "SUGGEST_NEW_TASK":
                self._recent_task_triggers.append((now, " ".join(triggering_context.casefold().split())))
            previous_search_terms: tuple[str, ...] = ()
            if event_type == "SEARCH_EXISTING_WORK":
                session = self._search_session
                if pending.is_refinement and session is not None:
                    session.refinements += 1
                    session.last_search_at = now
                    session.last_activity_at = now
                    session.last_text = pending.text_parts[-1]
                    previous_search_terms = tuple(session.searched_terms)
                elif pending.is_topic_check:
                    # Session is replaced in accept_search_terms only if the topic changed.
                    pass
                else:
                    self._search_session = _SearchSession(
                        score=pending.score,
                        last_search_at=now,
                        last_text=pending.text_parts[-1],
                        last_activity_at=now,
                    )
            events.append(
                MeetingEvent(
                    type=event_type,
                    score=pending.score,
                    triggered_at=now,
                    recent_context=recent_context,
                    triggering_context=triggering_context,
                    is_refinement=pending.is_refinement,
                    previous_search_terms=previous_search_terms,
                    is_topic_check=pending.is_topic_check,
                    background_context=pending.background_context,
                )
            )
        return events
