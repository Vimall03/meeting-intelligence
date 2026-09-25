import asyncio
import contextlib
import logging
import tempfile
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.services.meeting_context_service import MeetingContextService
from app.services.jev_routing_service import JevRoutingDecision, JevRoutingService
from app.services.transcription_services import transcribe_realtime_chunk

router = APIRouter()
logger = logging.getLogger("uvicorn.error")


def write_audio_snapshot(audio: bytes) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as audio_file:
        audio_file.write(audio)
        return audio_file.name


async def transcribe_snapshot(audio: bytes) -> dict:
    snapshot_path = Path(await run_in_threadpool(write_audio_snapshot, audio))
    try:
        return await run_in_threadpool(
            transcribe_realtime_chunk,
            str(snapshot_path),
        )
    finally:
        snapshot_path.unlink(missing_ok=True)


async def evaluate_new_context(
    meeting_context: MeetingContextService,
    jev: JevRoutingService,
) -> tuple[JevRoutingDecision, str]:
    snapshot = meeting_context.prepare_jev_context()
    if snapshot is None:
        raise RuntimeError("Jev evaluation started without new context")
    decision = await jev.decide(snapshot.recent_context, snapshot.new_context)
    meeting_context.complete_jev_evaluation(snapshot, time.monotonic())
    return decision, snapshot.new_context

@router.websocket("/ws/meeting")
async def meeting_stream(websocket: WebSocket):
    await websocket.accept()
    pending_audio_windows: deque[bytes] = deque()
    transcribed_audio_end = 0.0
    meeting_context = MeetingContextService(
        window_seconds=settings.meeting_context_window_seconds,
        turn_gap_seconds=settings.meeting_context_turn_gap_seconds,
    )
    transcription_task: asyncio.Task[dict] | None = None
    transcription_started_at: float | None = None
    jev = (
        JevRoutingService(
            api_key=settings.openrouter_api_key,
            model=settings.jev_model,
            endpoint=settings.jev_decisions_url,
        )
        if settings.openrouter_api_key
        else None
    )
    jev_task: asyncio.Task[tuple[JevRoutingDecision, str]] | None = None
    jev_evaluation_requested = False
    last_dispatched_jev_at: dict[str, float] = {}
    if jev is None:
        logger.warning("Jev routing disabled: OPENROUTER_API_KEY is not configured")
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_bytes(), timeout=1)
                pending_audio_windows.append(data)
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if transcription_task and transcription_task.done():
                completed_task = transcription_task
                transcription_task = None
                try:
                    transcript = completed_task.result()
                except Exception:
                    logger.exception("Transcription failed")
                else:
                    elapsed = time.monotonic() - transcription_started_at if transcription_started_at else 0
                    logger.info("Transcription completed in %.1f seconds", elapsed)
                    new_segments = [
                        {
                            **segment,
                            "start": segment["start"] + transcribed_audio_end,
                            "end": segment["end"] + transcribed_audio_end,
                        }
                        for segment in transcript["segments"]
                    ]
                    transcribed_audio_end += transcript["audio_duration"]
                    if new_segments:
                        accepted_segments = meeting_context.add_segments(new_segments)
                        for segment in accepted_segments:
                            logger.debug(
                                "%s: %s",
                                segment.speaker,
                                segment.text,
                            )
                        if accepted_segments:
                            jev_evaluation_requested = True
                            logger.info(
                                "Meeting context updated (%d new segment%s):\n%s",
                                len(accepted_segments),
                                "s" if len(accepted_segments) != 1 else "",
                                meeting_context.as_text(),
                            )
                    elif not transcript["segments"]:
                        logger.info("No speech detected in the audio snapshot")

            if (
                transcription_task is None
                and pending_audio_windows
            ):
                transcription_started_at = now
                audio_window = pending_audio_windows.popleft()
                logger.info(
                    "Transcribing audio window at %.1f seconds (%d bytes, %d queued)",
                    transcribed_audio_end,
                    len(audio_window),
                    len(pending_audio_windows),
                )
                transcription_task = asyncio.create_task(
                    transcribe_snapshot(audio_window)
                )

            if jev_task and jev_task.done():
                completed_jev_task = jev_task
                jev_task = None
                try:
                    decision, evaluated_new_context = completed_jev_task.result()
                except Exception:
                    logger.exception("Jev routing failed")
                else:
                    candidates = (
                        (
                            "SEARCH_EXISTING_WORK",
                            decision.search_existing_work_probability,
                            settings.jev_search_threshold,
                        ),
                        (
                            "SUGGEST_NEW_TASK",
                            decision.suggest_new_task_probability,
                            settings.jev_task_threshold,
                        ),
                    )
                    triggered: list[str] = []
                    suppressed: list[str] = []
                    for action, probability, threshold in candidates:
                        if probability < threshold:
                            continue
                        last_at = last_dispatched_jev_at.get(action)
                        if last_at is not None and now - last_at < settings.jev_dedup_window_seconds:
                            suppressed.append(action)
                            continue
                        last_dispatched_jev_at[action] = now
                        triggered.append(action)
                        await websocket.send_json(
                            {
                                "type": "jev_decision",
                                "decision": action,
                                "confidence": probability,
                            }
                        )
                    logger.info(
                        "JEV evaluation\n\nNEW:\n%s\n\n"
                        "SEARCH_EXISTING_WORK: %.2f\nSUGGEST_NEW_TASK:     %.2f\n\n"
                        "Triggered:\n%s\n"
                        "Suppressed (duplicate):\n%s\n"
                        "Cost: %s",
                        evaluated_new_context,
                        decision.search_existing_work_probability,
                        decision.suggest_new_task_probability,
                        "\n".join(triggered) if triggered else "none",
                        "\n".join(suppressed) if suppressed else "none",
                        f"${decision.cost:.8f}" if decision.cost is not None else "unknown",
                    )

            if (
                jev is not None
                and jev_task is None
                and jev_evaluation_requested
                and meeting_context.prepare_jev_context() is not None
            ):
                jev_evaluation_requested = False
                jev_task = asyncio.create_task(
                    evaluate_new_context(meeting_context, jev)
                )
    except WebSocketDisconnect:
        pass
    finally:
        if transcription_task:
            transcription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await transcription_task
        if jev_task:
            jev_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await jev_task
        if jev:
            await jev.close()