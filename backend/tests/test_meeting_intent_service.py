import unittest
from unittest.mock import AsyncMock

from app.services.jev_service import JevDecision
from app.services.llm_service import IntentReview
from app.services.meeting_intent_service import decide_meeting_intent


class MeetingIntentTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, decision, review=IntentReview(False, False)):
        jev = AsyncMock()
        llm = AsyncMock()
        jev.decide.return_value = decision
        llm.review_intent.return_value = review
        result = await decide_meeting_intent(
            jev, llm, "Concrete previous work", "The rollout plan is yours to finish.",
            search_threshold=.75, task_threshold=.75, review_minimum=.3)
        return result, llm

    async def test_clear_decisions_and_smalltalk_do_not_add_model_calls(self):
        for scores in ((.9, .1), (.1, .9), (.02, .04), (.9, .9)):
            with self.subTest(scores=scores):
                decision = JevDecision(*scores)
                result, llm = await self.evaluate(decision)
                self.assertIs(result, decision)
                llm.review_intent.assert_not_awaited()

    async def test_uncertain_continuation_is_confirmed_without_inventing_a_score(self):
        result, llm = await self.evaluate(JevDecision(.12, .37), IntentReview(False, True))
        self.assertTrue(result.task_confirmed)
        self.assertFalse(result.search_confirmed)
        self.assertEqual(result.suggest_new_task_probability, .37)
        llm.review_intent.assert_awaited_once_with("Concrete previous work", "The rollout plan is yours to finish.")

    async def test_one_review_handles_both_candidates_without_approving_unrelated_actions(self):
        result, llm = await self.evaluate(JevDecision(.6, .6), IntentReview(True, False, search_target="rollout plan"))
        self.assertTrue(result.search_confirmed)
        self.assertFalse(result.task_confirmed)
        llm.review_intent.assert_awaited_once()
        result, _ = await self.evaluate(JevDecision(.01, .6), IntentReview(True, True))
        self.assertFalse(result.search_confirmed)
        self.assertTrue(result.task_confirmed)

    async def test_negative_review_keeps_both_actions_suppressed(self):
        result, _ = await self.evaluate(JevDecision(.6, .6))
        self.assertFalse(result.search_confirmed)
        self.assertFalse(result.task_confirmed)
        self.assertFalse(result.wait_for_continuation)

    async def test_review_failure_preserves_pending_context_for_route_retry(self):
        jev, llm = AsyncMock(), AsyncMock()
        jev.decide.return_value = JevDecision(.1, .5)
        llm.review_intent.side_effect = TimeoutError("private response")
        with self.assertRaises(TimeoutError):
            await decide_meeting_intent(jev, llm, "recent", "new",
                search_threshold=.75, task_threshold=.75, review_minimum=.3)

    async def test_retained_thought_is_distinguished_from_latest_speech(self):
        jev, llm = AsyncMock(), AsyncMock()
        jev.decide.return_value = JevDecision(.4, .4)
        llm.review_intent.return_value = IntentReview(False, False)
        result = await decide_meeting_intent(jev, llm, "recent", "retained thought and fresh speech",
            search_threshold=.75, task_threshold=.75, review_minimum=.3, latest_context="fresh speech")
        self.assertFalse(result.wait_for_continuation)
        jev.decide.assert_awaited_once_with("recent", "retained thought and fresh speech", latest_context="fresh speech")
        llm.review_intent.assert_awaited_once_with("recent", "retained thought and fresh speech", latest_context="fresh speech")

    async def test_older_referents_and_offered_work_are_passed_to_scoped_review(self):
        jev, llm = AsyncMock(), AsyncMock()
        jev.decide.return_value = JevDecision(.4, .6)
        llm.review_intent.return_value = IntentReview(False, True)
        await decide_meeting_intent(jev, llm, "recent", "fresh",
            search_threshold=.75, task_threshold=.75, review_minimum=.3,
            background_context="Older referent", known_tasks=("Already offered",), known_search_terms=("prior topic",))
        jev.decide.assert_awaited_once_with("recent", "fresh")
        llm.review_intent.assert_awaited_once_with("recent", "fresh", background_context="Older referent",
            known_tasks=("Already offered",), known_search_terms=("prior topic",))

    async def test_only_an_unfinished_intent_is_retained_for_future_speech(self):
        result, _ = await self.evaluate(JevDecision(.4, .6), IntentReview(False, False, True))
        self.assertTrue(result.wait_for_continuation)
        result, _ = await self.evaluate(JevDecision(.4, .6), IntentReview(False, True, True))
        self.assertFalse(result.wait_for_continuation)
