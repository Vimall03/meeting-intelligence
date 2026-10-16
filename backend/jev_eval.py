"""Replay labeled Jev decision windows from a meeting fixture.

Run from backend with ``uv run python jev_eval.py``. Pass ``--live`` to ask
Jev again with the current production instructions and compare the new scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import settings
from app.services.jev_service import JevService
from app.services.llm_service import LlmService
from app.services.meeting_intent_service import decide_meeting_intent
from app.services.event_service import MeetingEvent
from app.services.google_drive_service import GoogleDriveSearchTerms


DEFAULT_FIXTURE = Path(__file__).parent / "tests/fixtures/jev_natural_meetings.json"
SIGNALS = ("search_existing", "suggest_new_task")


def summarize(windows: list[dict], scores: list[dict], thresholds: dict) -> dict:
    result = {}
    for signal in SIGNALS:
        threshold = thresholds[signal]
        tp = fp = fn = tn = 0
        misses = []
        false_alarms = []
        for window, score in zip(windows, scores, strict=True):
            expected = window["expected"][signal]
            predicted = score[signal] >= threshold or score.get(signal + "_confirmed", False)
            if signal + "_draft_valid" in score:
                predicted = predicted and score[signal + "_draft_valid"]
            if expected and predicted:
                tp += 1
            elif expected:
                fn += 1
                misses.append(window["id"])
            elif predicted:
                fp += 1
                false_alarms.append(window["id"])
            else:
                tn += 1
        result[signal] = {
            "threshold": threshold,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "misses": misses,
            "false_alarms": false_alarms,
        }
    return result


async def replay_live(windows: list[dict], *, review: bool = False, conversation: bool = False) -> list[dict]:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for --live")
    service = JevService(
        api_key=settings.openrouter_api_key,
        model=settings.jev_model,
        endpoint=settings.jev_decisions_url,
    )
    llm = LlmService(api_key=settings.openrouter_api_key, model=settings.gemma_model,
                     endpoint=settings.gemma_chat_completions_url) if review else None
    try:
        scores = []
        background = windows[0]["recent_context"] if windows else ""
        retained = []
        known_tasks, known_search_terms = [], []
        for index, window in enumerate(windows):
            new_context = window["new_context"]
            options = {"latest_context": new_context}
            if conversation:
                # The saved fixture has no audio timestamps. Four preceding
                # windows approximate the live 12-second continuation buffer;
                # this replay measures text decisions, not capture latency.
                retained = [(at, text) for at, text in retained if index - at < 4]
                background = (background + "\n" + new_context)[-9000:]
                new_context = "\n".join([text for _, text in retained] + [new_context])
                options.update(background_context=background, known_tasks=tuple(known_tasks[-12:]),
                               known_search_terms=tuple(known_search_terms[-6:]))
            decision = await decide_meeting_intent(
                service, llm, window["recent_context"], new_context,
                search_threshold=settings.jev_search_threshold, task_threshold=settings.jev_task_threshold,
                review_minimum=settings.intent_review_minimum,
                **options,
            )
            scores.append(
                {
                    "search_existing": decision.search_existing_work_probability,
                    "suggest_new_task": decision.suggest_new_task_probability,
                    "search_existing_confirmed": decision.search_confirmed,
                    "suggest_new_task_confirmed": decision.task_confirmed,
                }
            )
            if conversation:
                if decision.wait_for_continuation:
                    retained.append((index, window["new_context"]))
                else:
                    retained.clear()
                for action, active, score in (
                    ("SEARCH_EXISTING_WORK", decision.search_existing_work_probability >= settings.jev_search_threshold
                        or decision.search_confirmed, decision.search_existing_work_probability),
                    ("SUGGEST_NEW_TASK", decision.suggest_new_task_probability >= settings.jev_task_threshold
                        or decision.task_confirmed, decision.suggest_new_task_probability),
                ):
                    signal = "search_existing" if action == "SEARCH_EXISTING_WORK" else "suggest_new_task"
                    scores[-1][signal + "_draft_valid"] = False
                    if not active:
                        continue
                    # Generate the same query/draft as the app, solely to
                    # remember what was offered. Never invoke a connector.
                    result = await llm.process_event(MeetingEvent(action, score, index * 3,
                        window["recent_context"], new_context, background_context=background))
                    if action == "SEARCH_EXISTING_WORK":
                        terms = GoogleDriveSearchTerms.from_mapping(result.data)
                        scores[-1][signal + "_draft_valid"] = bool(terms.weighted_terms())
                        if terms.weighted_terms():
                            known_search_terms.extend(terms.primary_terms)
                    elif result.data.get("task_title") and result.data.get("task_description"):
                        scores[-1][signal + "_draft_valid"] = True
                        known_tasks.append(result.data["task_title"])
        return scores
    finally:
        await service.close()
        if llm:
            await llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--live", action="store_true", help="Query Jev again")
    parser.add_argument("--review", action="store_true", help="Include the production uncertain-intent review")
    parser.add_argument("--conversation", action="store_true", help="Replay background, continuations and offered-work state (no connectors)")
    parser.add_argument("--output", type=Path, help="Write per-window live scores to JSON")
    args = parser.parse_args()
    if args.conversation and not (args.live and args.review):
        parser.error("--conversation requires --live and --review")
    fixture = json.loads(args.fixture.read_text())
    if fixture["schema_version"] != 1:
        raise ValueError("Unsupported fixture schema")
    windows = fixture["windows"]
    print(f"Fixture: {args.fixture} ({len(windows)} scored windows)")
    if all("observed" in window for window in windows):
        baseline = [window["observed"] for window in windows]
        print("Observed log baseline:")
        print(json.dumps(summarize(windows, baseline, fixture["thresholds"]), indent=2))
    if args.live:
        scores = asyncio.run(replay_live(windows, review=args.review, conversation=args.conversation))
        print("Current instructions, live intent pipeline:")
        runtime_thresholds = {"search_existing": settings.jev_search_threshold,
                              "suggest_new_task": settings.jev_task_threshold}
        print(json.dumps(summarize(windows, scores, runtime_thresholds), indent=2))
        if args.output:
            args.output.write_text(
                json.dumps(
                    {
                        "fixture": str(args.fixture),
                        "model": settings.jev_model,
                        "thresholds": runtime_thresholds,
                        "review_model": settings.gemma_model if args.review else None,
                        "review_minimum": settings.intent_review_minimum if args.review else None,
                        "conversation": args.conversation,
                        "scores": [
                            {"id": window["id"], **score}
                            for window, score in zip(windows, scores, strict=True)
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
