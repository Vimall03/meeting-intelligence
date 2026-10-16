import json
import unittest
from pathlib import Path

from jev_eval import DEFAULT_FIXTURE, summarize


FIXTURE = Path(__file__).parent / "fixtures/jev_natural_meetings.json"


class JevSyntheticMeetingFixtureTests(unittest.TestCase):
    def test_a_positive_decision_without_a_usable_draft_is_not_reported_as_a_success(self):
        windows = [{"id": "request", "expected": {"search_existing": True, "suggest_new_task": True}}]
        scores = [{"search_existing": .9, "suggest_new_task": .9,
                   "search_existing_draft_valid": False, "suggest_new_task_draft_valid": False}]
        result = summarize(windows, scores, {"search_existing": .75, "suggest_new_task": .75})
        self.assertEqual(result["search_existing"]["fn"], 1)
        self.assertEqual(result["suggest_new_task"]["fn"], 1)

    def test_verified_lower_score_with_a_valid_draft_is_counted_without_changing_its_score(self):
        windows = [{"id": "request", "expected": {"search_existing": False, "suggest_new_task": True}}]
        scores = [{"search_existing": .1, "suggest_new_task": .4,
                   "suggest_new_task_confirmed": True, "suggest_new_task_draft_valid": True}]
        result = summarize(windows, scores, {"search_existing": .75, "suggest_new_task": .75})
        self.assertEqual(result["suggest_new_task"]["tp"], 1)
        self.assertEqual(scores[0]["suggest_new_task"], .4)

    def test_fixture_is_complete_and_baseline_is_reproducible(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        windows = fixture["windows"]
        self.assertEqual(DEFAULT_FIXTURE, FIXTURE)
        self.assertIn("Synthetic", fixture["description"])
        self.assertEqual(len(windows), 12)
        self.assertEqual(len({window["id"] for window in windows}), len(windows))
        for window in windows:
            self.assertTrue(window["recent_context"])
            self.assertTrue(window["new_context"])
            for signal in ("search_existing", "suggest_new_task"):
                self.assertIs(type(window["expected"][signal]), bool)
                self.assertGreaterEqual(window["observed"][signal], 0)
                self.assertLessEqual(window["observed"][signal], 1)

        baseline = summarize(
            windows,
            [window["observed"] for window in windows],
            fixture["thresholds"],
        )
        self.assertEqual(
            (baseline["search_existing"]["tp"], baseline["search_existing"]["fn"]),
            (5, 0),
        )
        self.assertEqual(
            (baseline["suggest_new_task"]["tp"], baseline["suggest_new_task"]["fn"]),
            (3, 0),
        )
        self.assertEqual(baseline["search_existing"]["fp"], 0)
        self.assertEqual(baseline["suggest_new_task"]["fp"], 0)


if __name__ == "__main__":
    unittest.main()
