from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


_WHITESPACE = re.compile(r"\s+")
_NON_ALPHANUMERIC = re.compile(r"[^\w]+", re.UNICODE)
_NON_SPEECH_MARKERS = {
    "[applause]",
    "[inaudible]",
    "[laughter]",
    "[music]",
    "[noise]",
    "[silence]",
    "(applause)",
    "(inaudible)",
    "(laughter)",
    "(music)",
    "(noise)",
    "(silence)",
}


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """A cleaned piece of speech suitable for use as meeting context."""

    start: float
    end: float
    text: str
    speaker: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class JevContextSnapshot:
    recent_context: str
    new_context: str
    segment_count: int
    latest_context: str = ""
    background_context: str = ""


class MeetingContextService:
    """Keeps the most recent meaningful portion of a live transcript."""

    def __init__(
        self,
        window_seconds: float = 45.0,
        max_segments: int = 250,
        turn_gap_seconds: float = 12.0,
        background_window_seconds: float = 180.0,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")
        if max_segments <= 0:
            raise ValueError("max_segments must be greater than zero")
        if turn_gap_seconds < 0:
            raise ValueError("turn_gap_seconds must not be negative")
        if background_window_seconds <= 0:
            raise ValueError("background_window_seconds must be greater than zero")

        self.window_seconds = window_seconds
        self.max_segments = max_segments
        self.turn_gap_seconds = turn_gap_seconds
        self.background_window_seconds = max(background_window_seconds, window_seconds)
        self._segments: list[TranscriptSegment] = []
        self._background_segments: list[TranscriptSegment] = []
        self._unevaluated_segments: list[TranscriptSegment] = []
        self._intent_candidates: list[TranscriptSegment] = []
        self._seen: set[tuple[float, float, str, str]] = set()
        self.last_jev_evaluation_time: float | None = None

    def add_segments(
        self, segments: Iterable[Mapping[str, object]]
    ) -> list[TranscriptSegment]:
        """Add usable segments and return only the ones newly retained."""
        accepted: list[TranscriptSegment] = []
        for raw_segment in segments:
            segment = self._clean_segment(raw_segment)
            if segment is None:
                continue

            segment_key = self._segment_key(segment)
            if segment_key in self._seen:
                continue

            self._segments.append(segment)
            self._background_segments.append(segment)
            self._unevaluated_segments.append(segment)
            self._seen.add(segment_key)
            accepted.append(segment)

        self._segments.sort(key=lambda segment: (segment.start, segment.end))
        self._evict_expired_segments()
        retained_keys = {self._segment_key(segment) for segment in self._segments}
        return [
            segment for segment in accepted if self._segment_key(segment) in retained_keys
        ]

    def get_segments(self) -> tuple[TranscriptSegment, ...]:
        """Return the cleaned rolling transcript in chronological order."""
        return tuple(self._segments)

    def as_text(self) -> str:
        """Return the rolling transcript as conversational speaker turns."""
        return self._segments_as_text(self._segments)

    def background_text(self) -> str:
        """Older speech only helps resolve the object of a fresh cue."""
        return self._segments_as_text(self._background_segments)[-9000:]

    def prepare_jev_context(self) -> JevContextSnapshot | None:
        """Build a stable Jev snapshot without consuming newly added text."""
        if not self._unevaluated_segments:
            return None
        # An uncertain, unfinished thought can span multiple successful
        # decisions. Keep a short overlap only until the next fresh speech;
        # candidates alone never schedule another evaluation.
        cutoff = max(s.end for s in self._unevaluated_segments) - 12.0
        candidates = [s for s in self._intent_candidates if s.end > cutoff]
        return JevContextSnapshot(
            recent_context=self.as_text(),
            new_context=self._segments_as_text([*candidates, *self._unevaluated_segments]),
            segment_count=len(self._unevaluated_segments),
            latest_context=self._segments_as_text(self._unevaluated_segments),
            background_context=self.background_text(),
        )

    def complete_jev_evaluation(
        self, snapshot: JevContextSnapshot, evaluated_at: float, *, retain_for_followup: bool = False
    ) -> None:
        """Commit a successful evaluation while preserving later arrivals."""
        evaluated = self._unevaluated_segments[: snapshot.segment_count]
        if retain_for_followup and evaluated:
            cutoff = max(s.end for s in evaluated) - 12.0
            self._intent_candidates = [s for s in [*self._intent_candidates, *evaluated] if s.end > cutoff][-self.max_segments:]
        else:
            self._intent_candidates.clear()
        del self._unevaluated_segments[: snapshot.segment_count]
        self.last_jev_evaluation_time = evaluated_at

    def clear(self) -> None:
        self._segments.clear()
        self._background_segments.clear()
        self._unevaluated_segments.clear()
        self._intent_candidates.clear()
        self._seen.clear()
        self.last_jev_evaluation_time = None

    def _segments_as_text(self, segments: Iterable[TranscriptSegment]) -> str:
        turns: list[TranscriptSegment] = []
        for segment in segments:
            if (
                turns
                and segment.speaker == turns[-1].speaker
                and segment.start - turns[-1].end <= self.turn_gap_seconds
            ):
                previous_turn = turns[-1]
                turns[-1] = TranscriptSegment(
                    start=previous_turn.start,
                    end=segment.end,
                    speaker=segment.speaker,
                    text=f"{previous_turn.text} {segment.text}",
                )
            else:
                turns.append(segment)

        return "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)

    def _clean_segment(
        self, raw_segment: Mapping[str, object]
    ) -> TranscriptSegment | None:
        text = _WHITESPACE.sub(" ", str(raw_segment.get("text", "")).strip())
        normalized_text = text.casefold()
        if (
            not text
            or normalized_text in _NON_SPEECH_MARKERS
            or not _NON_ALPHANUMERIC.sub("", text)
        ):
            return None

        try:
            start = float(raw_segment["start"])
            end = float(raw_segment["end"])
        except (KeyError, TypeError, ValueError):
            return None
        if start < 0 or end <= start:
            return None

        speaker = _WHITESPACE.sub(
            " ", str(raw_segment.get("speaker") or "UNKNOWN").strip()
        )
        return TranscriptSegment(start=start, end=end, text=text, speaker=speaker or "UNKNOWN")

    def _evict_expired_segments(self) -> None:
        if not self._segments:
            return

        newest_end = max(segment.end for segment in self._segments)
        cutoff = newest_end - self.window_seconds
        self._segments = [
            segment for segment in self._segments if segment.start >= cutoff
        ][-self.max_segments :]
        background_cutoff = newest_end - self.background_window_seconds
        self._background_segments = [
            segment for segment in self._background_segments if segment.end > background_cutoff
        ][-self.max_segments :]
        self._seen = {self._segment_key(segment) for segment in self._segments}

    @staticmethod
    def _segment_key(segment: TranscriptSegment) -> tuple[float, float, str, str]:
        return (
            round(segment.start, 3),
            round(segment.end, 3),
            segment.speaker.casefold(),
            segment.text.casefold(),
        )
