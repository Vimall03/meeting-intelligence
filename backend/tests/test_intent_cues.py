import unittest

from app.services.intent_cues import direct_task_request, direct_retrieval_request, grounded_search_target, fresh_work_transition


class IntentCueTests(unittest.TestCase):
    def test_direct_work_requests_and_soft_commitments(self):
        for speech in ("UNKNOWN: Please do take the survey.", "Please invest the time to get those set up.",
                       "Could you take a look after the call?", "I can get that ready by Thursday.",
                       "I’m going to run this monthly.", "I'll ask the team to send it."):
            with self.subTest(speech=speech):
                self.assertTrue(direct_task_request(speech))
        self.assertTrue(direct_task_request("We should add retries before launch."))
        self.assertTrue(direct_task_request("We need to arrange the customer meetings."))
        self.assertTrue(direct_task_request("I can put the sample invoice together."))
        self.assertTrue(direct_task_request("Could you have a look after the call?"))
        self.assertTrue(direct_task_request("Would you mind taking a look after the call?"))
        self.assertFalse(direct_task_request("I can put it this way."))

    def test_negations_hypotheticals_status_and_retrieval_are_not_direct_new_tasks(self):
        for speech in ("Please don't create another ticket.", "It is already fixed, please do not investigate.",
                       "If we ever need it I can prepare that.", "Anybody else in the same condition?",
                       "Please open the runbook.", "Can you find the budget spreadsheet?"):
            with self.subTest(speech=speech):
                self.assertFalse(direct_task_request(speech))

    def test_explicit_retrieval_is_a_request_rather_than_a_bare_verb(self):
        for speech in ("UNKNOWN: Find the migration plan.", "Can somebody pull up that file?",
                       "Someone from the team could find it now."):
            self.assertTrue(direct_retrieval_request(speech, "The migration document."))
        for speech in ("The office is open.", "I searched my email and found it.", "Don't open the file."):
            self.assertFalse(direct_retrieval_request(speech, "The migration document."))

    def test_old_material_cannot_be_promoted_without_a_fresh_cue(self):
        self.assertFalse(grounded_search_target("Customer contact spreadsheet", "Advantage of it. Third.", "Old spreadsheet."))
        self.assertFalse(grounded_search_target("Maturity page", "Raised questions about whether we.", "Maturity page."))
        self.assertTrue(grounded_search_target("Maturity flowchart", "One of which is this flowchart.", "Maturity page."))
        self.assertTrue(grounded_search_target("Rollback runbook", "What did we decide last time?", "Release runbook."))
        self.assertFalse(grounded_search_target("Pulse survey link", "Is there a way to put the link to this?", "Old survey discussion."))
        self.assertFalse(grounded_search_target("Pulse survey", "Survey in the agenda. Well.", "Old survey discussion."))
        self.assertFalse(grounded_search_target("Pulse survey email", "That is the title of the email.", "Old survey discussion."))

    def test_filler_and_completed_work_do_not_promote_an_uncertain_task(self):
        self.assertFalse(fresh_work_transition("Survey in the agenda. Well."))
        self.assertFalse(fresh_work_transition("Anybody else?"))
        self.assertFalse(fresh_work_transition("Already completed, no follow-up needed."))
        self.assertTrue(fresh_work_transition("Yes, that is the next step."))
        self.assertTrue(fresh_work_transition("And get at least three done, if you haven't already."))
