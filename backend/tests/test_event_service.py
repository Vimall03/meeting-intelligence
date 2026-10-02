import unittest

from app.services.event_service import EventService
from app.services.jev_service import JevDecision


def _search_decision(probability: float) -> JevDecision:
    return JevDecision(
        search_existing_work_probability=probability,
        suggest_new_task_probability=0.0,
    )


def _task_decision(probability: float) -> JevDecision:
    return JevDecision(
        search_existing_work_probability=0.0,
        suggest_new_task_probability=probability,
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

    def test_dedupes_new_task_event_within_window_after_finalizing(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _task_decision(0.9),
            recent_context="recent",
            triggering_context="Investigate the database error.",
            now=0.0,
        )

        again = service.start_pending(
            _task_decision(0.9),
            recent_context="recent",
            triggering_context="Investigate the database error.",
            now=30.0,
        )
        later = service.start_pending(
            _task_decision(0.9),
            recent_context="recent",
            triggering_context="Investigate the database error.",
            now=200.0,
        )

        self.assertEqual(again, [])
        self.assertEqual(len(later), 1)

    def test_distinct_tasks_are_not_suppressed_for_ninety_seconds(self) -> None:
        service = EventService(search_threshold=.75, task_threshold=.75, dedupe_window_seconds=90)
        first = service.start_pending(_task_decision(.9), "Database errors.", "Investigate the database error.", 0)
        second = service.start_pending(_task_decision(.9), "Support needs notes.", "Send rollout notes to support.", 6)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    def test_new_search_topic_is_not_suppressed_by_previous_search_cooldown(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.83),
            recent_context="recent",
            triggering_context="Cost comparison.",
            now=0.0,
        )

        new_topic = service.start_pending(
            _search_decision(0.83),
            recent_context="recent",
            triggering_context="Laptop invoice.",
            now=30.0,
        )

        self.assertEqual(len(new_topic), 1)
        self.assertEqual(new_topic[0].type, "SEARCH_EXISTING_WORK")
        self.assertFalse(new_topic[0].is_refinement)
        self.assertTrue(
            service.accept_search_terms(
                ["laptop invoice"], ["laptop invoice"], is_refinement=False, now=30.0
            )
        )

    def test_duplicate_search_topic_is_suppressed_with_reason(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.83), "recent", "Cost comparison.", now=0.0
        )
        self.assertTrue(
            service.accept_search_terms(
                ["cost comparison"],
                ["cost comparison"],
                is_refinement=False,
                now=0.0,
            )
        )
        service.start_pending(
            _search_decision(0.83), "recent", "Compare the cost.", now=30.0
        )

        accepted = service.accept_search_terms(
            ["cost comparison"],
            ["cost comparison", "supplier"],
            is_refinement=False,
            now=30.0,
        )

        self.assertFalse(accepted)
        self.assertEqual(
            service.last_search_terms_suppression_reason, "duplicate_search_topic"
        )

    def test_waits_for_one_more_chunk_when_phrase_is_incomplete(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.77),
            recent_context="recent",
            triggering_context="don't remember exactly what we called",
            now=23.9,
        )

        # Would previously finalize on word count (>= 10 words).
        self.assertEqual(
            service.enrich("but I think it was something", "recent", now=25.0), []
        )
        self.assertEqual(service.check_timeouts("recent", now=27.0), [])

        finalized = service.enrich("around cost comparison.", "recent", now=28.0)

        self.assertEqual(len(finalized), 1)
        self.assertTrue(
            finalized[0].triggering_context.endswith("around cost comparison.")
        )

    def test_incomplete_phrase_still_times_out(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.8),
            recent_context="recent",
            triggering_context="I think it was something",
            now=0.0,
        )

        self.assertEqual(service.check_timeouts("recent", now=3.9), [])
        self.assertEqual(len(service.check_timeouts("recent", now=4.0)), 1)

    def test_jev_crossing_during_search_session_emits_refinement(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        [initial] = service.start_pending(
            _search_decision(0.8),
            recent_context="recent",
            triggering_context="Where is the dropshipping margin sheet?",
            now=0.0,
        )
        self.assertFalse(initial.is_refinement)
        service.accept_search_terms(
            ["dropshipping costs margin comparison"],
            ["dropshipping costs margin comparison", "dropshipping"],
            is_refinement=False,
            now=0.0,
        )

        too_soon = service.start_pending(
            _search_decision(0.8), "recent", "Hmm, it was about costs.", now=1.0
        )
        self.assertEqual(too_soon, [])

        [refined] = service.start_pending(
            _search_decision(0.75),
            "recent",
            "I think it was something around cost comparison.",
            now=3.0,
        )
        self.assertTrue(refined.is_refinement)
        self.assertEqual(
            refined.previous_search_terms,
            ("dropshipping costs margin comparison", "dropshipping"),
        )

    def test_retrieval_clue_split_across_chunks_opens_refinement(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.8),
            recent_context="recent",
            triggering_context="Can we find the dropshipping file?",
            now=0.0,
        )

        self.assertEqual(service.enrich("but I think it was something", "recent", 3.0), [])
        [refined] = service.enrich("around cost comparison.", "recent", now=6.0)

        self.assertTrue(refined.is_refinement)
        self.assertEqual(
            refined.triggering_context,
            "but I think it was something around cost comparison.",
        )

    def test_limits_refinements_per_search_session(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(_search_decision(0.8), "recent", "Find it.", now=0.0)

        [first] = service.start_pending(_search_decision(0.8), "recent", "A.", now=3.0)
        [second] = service.start_pending(_search_decision(0.8), "recent", "B.", now=6.0)

        self.assertTrue(first.is_refinement)
        self.assertFalse(second.is_refinement)
        self.assertTrue(second.is_topic_check)

    def _frozen_maturity_session(self) -> EventService:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(
            _search_decision(0.78), "recent", "The flowchart showing maturity.", now=0.0
        )
        self.assertTrue(
            service.accept_search_terms(
                ["flowchart showing maturity"],
                ["flowchart showing maturity", "maturity"],
                is_refinement=False,
                now=0.0,
            )
        )
        [refined] = service.start_pending(
            _search_decision(0.79), "recent", "The maturity flowchart.", now=3.0
        )
        self.assertTrue(refined.is_refinement)
        self.assertTrue(
            service.accept_search_terms(
                ["maturity flowchart diagram"],
                ["maturity flowchart diagram"],
                is_refinement=True,
                now=3.0,
            )
        )
        return service

    def test_refinement_limit_freezes_same_topic(self) -> None:
        service = self._frozen_maturity_session()

        now = 6.0
        for primary in (["maturity page"], ["updates for the charts"]):
            [event] = service.start_pending(
                _search_decision(0.88), "recent", "Look at that.", now=now
            )
            self.assertTrue(event.is_topic_check)
            self.assertFalse(
                service.accept_search_terms(
                    primary, primary, is_refinement=False, now=now, is_topic_check=True
                )
            )
            self.assertEqual(
                service.last_search_terms_suppression_reason, "search_topic_frozen"
            )
            # Continued discussion keeps the topic frozen past the session window.
            now += 15.0

    def test_refinement_limit_allows_materially_new_topic(self) -> None:
        service = self._frozen_maturity_session()

        [event] = service.start_pending(
            _search_decision(0.85),
            "recent",
            "Can we look at the laptop invoice?",
            now=6.0,
        )
        self.assertTrue(event.is_topic_check)
        self.assertTrue(
            service.accept_search_terms(
                ["laptop invoice"],
                ["laptop invoice"],
                is_refinement=False,
                now=6.0,
                is_topic_check=True,
            )
        )

        # The new topic gets its own refinement budget.
        [refined] = service.start_pending(
            _search_decision(0.8), "recent", "The Dell one.", now=9.0
        )
        self.assertTrue(refined.is_refinement)

    def test_refinement_without_new_primary_terms_skips_search(self) -> None:
        service = EventService(
            search_threshold=0.75, task_threshold=0.75, dedupe_window_seconds=90.0
        )
        service.start_pending(_search_decision(0.8), "recent", "Find it.", now=0.0)
        self.assertTrue(
            service.accept_search_terms(
                ["cost comparison"],
                ["cost comparison"],
                is_refinement=False,
                now=0.0,
            )
        )

        self.assertFalse(
            service.accept_search_terms(
                ["Cost Comparison"],
                ["Cost Comparison", "supplier"],
                is_refinement=True,
                now=3.0,
            )
        )
        self.assertTrue(
            service.accept_search_terms(
                ["supplier cost sheet"],
                ["supplier cost sheet"],
                is_refinement=True,
                now=6.0,
            )
        )


if __name__ == "__main__":
    unittest.main()

