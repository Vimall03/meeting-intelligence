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


DEFAULT_FIXTURE = Path(__file__).parent / "tests/fixtures/jev_real_meeting.json"
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
            predicted = score[signal] >= threshold
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


async def replay_live(windows: list[dict]) -> list[dict]:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for --live")
    service = JevService(
        api_key=settings.openrouter_api_key,
        model=settings.jev_model,
        endpoint=settings.jev_decisions_url,
    )
    try:
        scores = []
        for window in windows:
            decision = await service.decide(
                window["recent_context"], window["new_context"]
            )
            scores.append(
                {
                    "search_existing": decision.search_existing_work_probability,
                    "suggest_new_task": decision.suggest_new_task_probability,
                }
            )
        return scores
    finally:
        await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--live", action="store_true", help="Query Jev again")
    parser.add_argument("--output", type=Path, help="Write per-window live scores to JSON")
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    if fixture["schema_version"] != 1:
        raise ValueError("Unsupported fixture schema")
    windows = fixture["windows"]
    print(f"Fixture: {args.fixture} ({len(windows)} scored windows)")
    baseline = [window["observed"] for window in windows]
    print("Observed log baseline:")
    print(json.dumps(summarize(windows, baseline, fixture["thresholds"]), indent=2))
    if args.live:
        scores = asyncio.run(replay_live(windows))
        print("Current instructions, live Jev:")
        print(json.dumps(summarize(windows, scores, fixture["thresholds"]), indent=2))
        if args.output:
            args.output.write_text(
                json.dumps(
                    {
                        "fixture": str(args.fixture),
                        "model": settings.jev_model,
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
