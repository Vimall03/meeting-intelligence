import unittest

from app.services.meeting_timeline_service import MeetingTimelineService


class MeetingTimelineServiceTests(unittest.TestCase):
    def test_search_and_task_results_are_newest_first_with_complete_data(self) -> None:
        timeline = MeetingTimelineService()
        initial = timeline.snapshot()
        self.assertEqual(initial["revision"], 0)
        self.assertEqual(initial["items"], [])

        file = {
            "id": "file-1",
            "name": "Launch plan",
            "mime_type": "application/vnd.google-apps.document",
            "web_view_link": "https://drive.google.com/file-1",
            "source": "google_drive",
        }
        search = timeline.add_search(query="launch plan", files=[file])
        task = timeline.add_task(
            title="  Send   plan ",
            description=" Share  the plan with   Maya. ",
            confidence=0.84,
        )
        file["name"] = "Changed later"

        snapshot = timeline.snapshot()
        self.assertEqual(snapshot["session_id"], initial["session_id"])
        self.assertEqual(snapshot["revision"], 2)
        self.assertEqual([item["id"] for item in snapshot["items"]], [task["id"], search["id"]])
        self.assertEqual([item["sequence"] for item in snapshot["items"]], [2, 1])
        self.assertTrue(task["created_at"].endswith("Z"))
        self.assertEqual(task["title"], "Send plan")
        self.assertEqual(task["description"], "Share the plan with Maya.")
        self.assertEqual(search["source"], {"id": "google_drive", "label": "Google Drive"})
        self.assertEqual(snapshot["items"][1]["files"][0]["name"], "Launch plan")

        snapshot["items"][0]["title"] = "Tampered"
        self.assertEqual(timeline.snapshot()["items"][0]["title"], "Send plan")

    def test_empty_searches_do_not_enter_timeline_or_advance_sequence(self) -> None:
        timeline = MeetingTimelineService()
        with self.assertRaises(ValueError):
            timeline.add_task(title="Send plan", description=" ", confidence=0.8)
        self.assertIsNone(timeline.add_search(query="launch plan", files=[]))
        self.assertIsNone(timeline.add_search(query="launch plan", files=[], error="Reconnect Drive"))
        self.assertEqual(timeline.snapshot()["revision"], 0)
        self.assertEqual(timeline.snapshot()["items"], [])

        task = timeline.add_task(title="Send plan", description="Send it to Maya.", confidence=0.8)
        self.assertEqual(task["sequence"], 1)

    def test_identical_draft_is_deduplicated_but_a_new_task_is_kept(self) -> None:
        timeline = MeetingTimelineService()
        first = timeline.add_task(title="Investigate database error", description="Investigate the reported error.", confidence=.9)
        repeated = timeline.add_task(title="investigate database error", description="Look into the database error.", confidence=.95)
        second = timeline.add_task(title="Send rollout notes", description="Send them to support.", confidence=.9)
        self.assertEqual(first["id"], repeated["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(timeline.snapshot()["revision"], 2)
