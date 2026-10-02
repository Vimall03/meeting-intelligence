import json
import unittest
from pathlib import Path

from jev_eval import summarize


FIXTURE = Path(__file__).parent / "fixtures/jev_real_meeting.json"


class JevRealMeetingFixtureTests(unittest.TestCase):
    def test_fixture_is_complete_and_baseline_is_reproducible(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        windows = fixture["windows"]
        self.assertEqual(len(windows), 46)
        self.assertEqual(len({window["id"] for window in windows}), len(windows))
        for window in windows:
            self.assertTrue(window["recent_context"].startswith("UNKNOWN:"))
            self.assertTrue(window["new_context"].startswith("UNKNOWN:"))
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
            (0, 3),
        )
        self.assertEqual(
            (baseline["suggest_new_task"]["tp"], baseline["suggest_new_task"]["fn"]),
            (1, 7),
        )
        self.assertEqual(baseline["search_existing"]["fp"], 0)
        self.assertEqual(baseline["suggest_new_task"]["fp"], 0)


if __name__ == "__main__":
    unittest.main()
