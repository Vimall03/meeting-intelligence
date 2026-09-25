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
from app.services.event_service import EventService, MeetingEvent
from app.services.jev_service import JevDecision, JevService
from app.services.llm_service import LlmResult, LlmService
from app.services.meeting_context_service import MeetingContextService
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
    jev: JevService,
) -> tuple[JevDecision, str]:
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
        JevService(
            api_key=settings.openrouter_api_key,
            model=settings.jev_model,
            endpoint=settings.jev_decisions_url,
        )
        if settings.openrouter_api_key
        else None
    )
    llm = (
        LlmService(
            api_key=settings.openrouter_api_key,
            model=settings.gemma_model,
            endpoint=settings.gemma_chat_completions_url,
        )
        if settings.openrouter_api_key
        else None
    )
    event_service = EventService(
        search_threshold=settings.jev_search_threshold,
        task_threshold=settings.jev_task_threshold,
        dedupe_window_seconds=settings.jev_dedup_window_seconds,
    )
    jev_task: asyncio.Task[tuple[JevDecision, str]] | None = None
    jev_evaluation_requested = False
    llm_tasks: list[tuple[asyncio.Task[LlmResult], float]] = []
    if jev is None:
        logger.warning("Jev routing disabled: OPENROUTER_API_KEY is not configured")

    async def dispatch_events(events: list[MeetingEvent]) -> None:
        for event in events:
            await websocket.send_json(
                {
                    "type": "jev_decision",
                    "decision": event.type,
                    "confidence": event.score,
                }
            )
            if llm is not None:
                llm_tasks.append(
                    (asyncio.create_task(llm.process_event(event)), time.monotonic())
                )

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
                            new_text = " ".join(
                                segment.text for segment in accepted_segments
                            )
                            await dispatch_events(
                                event_service.enrich(
                                    new_text,
                                    recent_context=meeting_context.as_text(),
                                    now=now,
                                )
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
                    events = event_service.start_pending(
                        decision,
                        recent_context=meeting_context.as_text(),
                        triggering_context=evaluated_new_context,
                        now=now,
                    )
                    await dispatch_events(events)
                    logger.info(
                        "JEV evaluation\n\nNEW:\n%s\n\n"
                        "SEARCH_EXISTING_WORK: %.2f\nSUGGEST_NEW_TASK:     %.2f\n\n"
                        "Pending enrichment:\n%s\n"
                        "Finalized:\n%s\n"
                        "Cost: %s",
                        evaluated_new_context,
                        decision.search_existing_work_probability,
                        decision.suggest_new_task_probability,
                        "\n".join(event_service.pending_types()) or "none",
                        "\n".join(event.type for event in events) if events else "none",
                        f"${decision.cost:.8f}" if decision.cost is not None else "unknown",
                    )

            timed_out_events = event_service.check_timeouts(
                recent_context=meeting_context.as_text(), now=now
            )
            await dispatch_events(timed_out_events)

            if llm_tasks:
                still_running: list[tuple[asyncio.Task[LlmResult], float]] = []
                for task, started_at in llm_tasks:
                    if not task.done():
                        still_running.append((task, started_at))
                        continue
                    latency = now - started_at
                    try:
                        result = task.result()
                    except Exception:
                        logger.exception(
                            "Gemma call failed after %.1f seconds", latency
                        )
                        continue
                    logger.info(
                        "Gemma result for %s in %.1f seconds: %s (cost: %s)",
                        result.event_type,
                        latency,
                        result.data,
                        f"${result.cost:.8f}" if result.cost is not None else "unknown",
                    )
                    await websocket.send_json(
                        {
                            "type": "llm_result",
                            "decision": result.event_type,
                            "data": result.data,
                        }
                    )
                llm_tasks = still_running

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
        for task, _ in llm_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if jev:
            await jev.close()
        if llm:
            await llm.close()