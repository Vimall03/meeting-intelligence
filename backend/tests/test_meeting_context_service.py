import unittest

from app.services.meeting_context_service import MeetingContextService


class MeetingContextServiceTests(unittest.TestCase):
    def test_discards_garbage_and_keeps_cleaned_context(self) -> None:
        context = MeetingContextService()

        added = context.add_segments(
            [
                {"start": 0, "end": 1, "text": "   "},
                {"start": 1, "end": 2, "text": "[noise]"},
                {"start": 2, "end": 2, "text": "invalid timing"},
                {
                    "start": 2,
                    "end": 3,
                    "text": "  We   should ship it.  ",
                    "speaker": "Alex",
                },
                {"start": 3, "end": 4, "text": "..."},
            ]
        )

        self.assertEqual(len(added), 1)
        self.assertEqual(context.as_text(), "Alex: We should ship it.")

    def test_evicts_expired_and_duplicate_segments(self) -> None:
        context = MeetingContextService(window_seconds=10)
        first_segment = {"start": 0, "end": 2, "text": "Old discussion"}

        context.add_segments([first_segment, first_segment])
        context.add_segments(
            [{"start": 11, "end": 13, "text": "Current decision"}]
        )

        self.assertEqual(context.as_text(), "UNKNOWN: Current decision")

    def test_default_window_discards_older_meeting_context(self) -> None:
        context = MeetingContextService()

        context.add_segments(
            [
                {"start": 0, "end": 2, "text": "Hello, how's it going?"},
                {"start": 44, "end": 46, "text": "Recent discussion"},
            ]
        )

        self.assertEqual(context.as_text(), "UNKNOWN: Recent discussion")

    def test_combines_nearby_fragments_into_one_turn(self) -> None:
        context = MeetingContextService(turn_gap_seconds=12)

        context.add_segments(
            [
                {"start": 10, "end": 12, "text": "Yeah, the problem"},
                {"start": 13, "end": 16, "text": "is we need active context"},
                {"start": 20, "end": 23, "text": "for Jev."},
            ]
        )

        self.assertEqual(
            context.as_text(),
            "UNKNOWN: Yeah, the problem is we need active context for Jev.",
        )

    def test_tracks_only_text_added_since_last_jev_evaluation(self) -> None:
        context = MeetingContextService()
        context.add_segments(
            [{"start": 0, "end": 2, "text": "We discussed the theme update."}]
        )

        first_snapshot = context.prepare_jev_context()
        self.assertIsNotNone(first_snapshot)
        assert first_snapshot is not None
        context.add_segments(
            [{"start": 3, "end": 5, "text": "The password issue is fixed."}]
        )
        context.complete_jev_evaluation(first_snapshot, evaluated_at=10.0)

        second_snapshot = context.prepare_jev_context()
        self.assertIsNotNone(second_snapshot)
        assert second_snapshot is not None
        self.assertEqual(
            second_snapshot.recent_context,
            "UNKNOWN: We discussed the theme update. The password issue is fixed.",
        )
        self.assertEqual(
            second_snapshot.new_context,
            "UNKNOWN: The password issue is fixed.",
        )
        self.assertEqual(context.last_jev_evaluation_time, 10.0)

    def test_failed_or_uncommitted_snapshot_keeps_new_context(self) -> None:
        context = MeetingContextService()
        context.add_segments([{"start": 0, "end": 2, "text": "Create a follow-up."}])

        first_snapshot = context.prepare_jev_context()
        retry_snapshot = context.prepare_jev_context()

        self.assertEqual(first_snapshot, retry_snapshot)

    def test_uncertain_thought_is_joined_to_fresh_speech_and_then_consumed(self):
        context = MeetingContextService()
        context.add_segments([{"start": 0, "end": 3, "text": "I can get that"}])
        first = context.prepare_jev_context()
        context.complete_jev_evaluation(first, 0, retain_for_followup=True)
        self.assertIsNone(context.prepare_jev_context())
        context.add_segments([{"start": 3, "end": 6, "text": "ready by Thursday."}])
        second = context.prepare_jev_context()
        self.assertEqual(second.new_context, "UNKNOWN: I can get that ready by Thursday.")
        self.assertEqual(second.latest_context, "UNKNOWN: ready by Thursday.")
        self.assertEqual(second.segment_count, 1)
        # An arrival while the decision runs is not accidentally consumed.
        context.add_segments([{"start": 6, "end": 9, "text": "Next topic."}])
        context.complete_jev_evaluation(second, 1)
        self.assertEqual(context.prepare_jev_context().new_context, "UNKNOWN: Next topic.")

    def test_continuation_memory_expires_and_survives_checkpoints(self):
        from app.services.meeting_sessions import context_state, restore_context
        context = MeetingContextService()
        context.add_segments([{"start": 0, "end": 3, "text": "We could look at that"}])
        context.complete_jev_evaluation(context.prepare_jev_context(), 0, retain_for_followup=True)
        restored = MeetingContextService()
        restore_context(restored, context_state(context))
        restored.add_segments([{"start": 3, "end": 6, "text": "after the call."}])
        self.assertIn("We could look at that", restored.prepare_jev_context().new_context)
        context.add_segments([{"start": 18, "end": 21, "text": "Unrelated subject."}])
        self.assertEqual(context.prepare_jev_context().new_context, "UNKNOWN: Unrelated subject.")

    def test_older_referent_survives_outside_recent_window_but_background_is_bounded(self):
        context = MeetingContextService(window_seconds=10, background_window_seconds=30)
        context.add_segments([{"start": 0, "end": 3, "text": "Customer meetings are the goal."}])
        context.complete_jev_evaluation(context.prepare_jev_context(), 0)
        context.add_segments([{"start": 18, "end": 21, "text": "Get those set up."}])
        snapshot = context.prepare_jev_context()
        self.assertNotIn("Customer meetings", snapshot.recent_context)
        self.assertIn("Customer meetings", snapshot.background_context)
        self.assertEqual(snapshot.latest_context, "UNKNOWN: Get those set up.")
        context.complete_jev_evaluation(snapshot, 1)
        context.add_segments([{"start": 33, "end": 36, "text": "Different topic."}])
        self.assertNotIn("Customer meetings", context.prepare_jev_context().background_context)

    def test_background_restores_without_becoming_new_speech_or_crossing_contexts(self):
        from app.services.meeting_sessions import context_state, restore_context
        first = MeetingContextService(window_seconds=10, background_window_seconds=30)
        first.add_segments([{"start": 0, "end": 3, "text": "First meeting private context."}])
        first.complete_jev_evaluation(first.prepare_jev_context(), 0)
        first.add_segments([{"start": 18, "end": 21, "text": "Current words."}])
        first.complete_jev_evaluation(first.prepare_jev_context(), 1)
        restored = MeetingContextService(window_seconds=10, background_window_seconds=30)
        restore_context(restored, context_state(first))
        self.assertIsNone(restored.prepare_jev_context())
        self.assertIn("First meeting private context", restored.background_text())
        separate = MeetingContextService()
        self.assertNotIn("First meeting private context", separate.background_text())
        restored.clear()
        self.assertEqual(restored.background_text(), "")


if __name__ == "__main__":
    unittest.main()
