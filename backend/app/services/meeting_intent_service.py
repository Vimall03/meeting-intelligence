"""Review uncertain decisions without weakening either action's threshold."""
from dataclasses import replace

from app.core.observability import observe_operation
from app.services.jev_service import JevDecision, JevService
from app.services.llm_service import LlmService
from app.services.intent_cues import direct_retrieval_request, direct_task_request, grounded_search_target, fresh_work_transition


async def decide_meeting_intent(
    jev: JevService,
    llm: LlmService | None,
    recent_context: str,
    new_context: str,
    *,
    search_threshold: float,
    task_threshold: float,
    review_minimum: float,
    latest_context: str | None = None,
    background_context: str | None = None,
    known_tasks=(),
    known_search_terms=(),
) -> JevDecision:
    context_options = {"latest_context": latest_context} if latest_context is not None else {}
    decision = await jev.decide(recent_context, new_context, **context_options)
    fresh = latest_context if latest_context is not None else new_context
    explicit_search = direct_retrieval_request(fresh, recent_context)
    explicit_task = direct_task_request(fresh, recent_context)
    search_candidate = review_minimum <= decision.search_existing_work_probability < search_threshold
    task_candidate = review_minimum <= decision.suggest_new_task_probability < task_threshold
    if llm is None or not ((search_candidate and not explicit_search) or (task_candidate and not explicit_task)):
        return replace(decision, search_confirmed=explicit_search, task_confirmed=explicit_task) if explicit_search or explicit_task else decision
    review_options = {**context_options}
    if background_context:
        review_options["background_context"] = background_context
    if known_tasks:
        review_options["known_tasks"] = known_tasks
    if known_search_terms:
        review_options["known_search_terms"] = known_search_terms
    review = await observe_operation('intent_review', llm.review_intent(recent_context, new_context, **review_options))
    # Preserve JEV's original probabilities. A semantic confirmation isn't a
    # calibrated numeric score, and it may only approve its own candidate.
    search_confirmed = explicit_search or (search_candidate and review.search_existing_work
        and grounded_search_target(review.search_target, fresh, recent_context))
    task_confirmed = explicit_task or (task_candidate and review.suggest_new_task and fresh_work_transition(fresh))
    return replace(decision, search_confirmed=search_confirmed, task_confirmed=task_confirmed,
                   wait_for_continuation=review.needs_more_context and not (
                       search_confirmed or task_confirmed
                       or decision.search_existing_work_probability >= search_threshold
                       or decision.suggest_new_task_probability >= task_threshold))
