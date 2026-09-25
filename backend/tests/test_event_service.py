import unittest

from app.services.event_service import EventService
from app.services.jev_service import JevDecision


def _search_decision(probability: float) -> JevDecision:
    return JevDecision(
        search_existing_work_probability=probability,
        suggest_new_task_probability=0.0,
    )


class EventServiceTests(unittest.TestCase):
    def test_finalizes_immediately_when_trigger_text_ends_a_sentence(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )

        events = service.start_pending(
            _search_decision(0.82),
            recent_context="recent",
            triggering_context="Is that theme issue still open?",
            now=10.0,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "SEARCH_EXISTING_WORK")
        self.assertEqual(events[0].score, 0.82)
        self.assertEqual(events[0].triggering_context, "Is that theme issue still open?")
        self.assertEqual(service.pending_types(), [])

    def test_waits_for_enrichment_before_finalizing(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )

        started = service.start_pending(
            _search_decision(0.82),
            recent_context="recent",
            triggering_context="ticket where the user needs to be",
            now=16.0,
        )
        self.assertEqual(started, [])
        self.assertEqual(service.pending_types(), ["SEARCH_EXISTING_WORK"])

        still_pending = service.enrich("re-enrolled", recent_context="recent", now=17.0)
        self.assertEqual(still_pending, [])

        finalized = service.enrich(
            "into a course after a mixup",
            recent_context="recent",
            now=17.5,
        )

        self.assertEqual(len(finalized), 1)
        self.assertEqual(
            finalized[0].triggering_context,
            "ticket where the user needs to be re-enrolled "
            "into a course after a mixup",
        )
        self.assertEqual(service.pending_types(), [])

    def test_finalizes_on_silence_timeout(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.82),
            recent_context="recent",
            triggering_context="ticket where the user needs to be",
            now=0.0,
        )

        not_yet = service.check_timeouts(recent_context="recent", now=1.0)
        self.assertEqual(not_yet, [])

        finalized = service.check_timeouts(recent_context="recent", now=1.6)
        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0].type, "SEARCH_EXISTING_WORK")

    def test_finalizes_on_hard_cap(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.82),
            recent_context="recent",
            triggering_context="ticket",
            now=0.0,
        )

        service.enrich("still going", recent_context="recent", now=1.0)
        service.enrich("still going", recent_context="recent", now=2.0)
        service.enrich("still going", recent_context="recent", now=3.0)
        finalized = service.enrich("still going", recent_context="recent", now=4.0)

        self.assertEqual(len(finalized), 1)

    def test_ignores_probabilities_below_threshold(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )

        events = service.start_pending(
            _search_decision(0.5),
            recent_context="recent",
            triggering_context="hello.",
            now=0.0,
        )

        self.assertEqual(events, [])
        self.assertEqual(service.pending_types(), [])

    def test_dedupes_new_pending_event_within_window_after_finalizing(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.9),
            recent_context="recent",
            triggering_context="Done.",
            now=0.0,
        )

        again = service.start_pending(
            _search_decision(0.9),
            recent_context="recent",
            triggering_context="Also done.",
            now=30.0,
        )
        later = service.start_pending(
            _search_decision(0.9),
            recent_context="recent",
            triggering_context="Also done.",
            now=200.0,
        )

        self.assertEqual(again, [])
        self.assertEqual(len(later), 1)


if __name__ == "__main__":
    unittest.main()

