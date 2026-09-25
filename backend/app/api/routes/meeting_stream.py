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
from app.services.google_drive_service import (
    GoogleDriveSearchTerms,
    GoogleDriveService,
)
from app.services.jev_service import JevDecision, JevService
from app.services.llm_service import LlmResult, LlmService
from app.services.meeting_context_service import MeetingContextService
from app.services.openrouter_stt_service import transcribe_realtime_chunk

router = APIRouter()
logger = logging.getLogger("uvicorn.error")
_LOG_COLORS = {
    "STT": "36",
    "CONTEXT": "34",
    "JEV": "33",
    "LLM": "35",
    "DRIVE": "32",
}


def _log_tag(stage: str) -> str:
    return f"\033[{_LOG_COLORS[stage]}m[MEETING][{stage}]\033[0m"


def _format_cost(cost: float | None) -> str:
    return f"${cost:.5f}" if cost is not None else "n/a"


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
    google_drive: GoogleDriveService = websocket.app.state.google_drive
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
    jev_started_at: float | None = None
    jev_evaluation_requested = False
    llm_tasks: list[tuple[asyncio.Task[LlmResult], float, MeetingEvent]] = []
    drive_tasks: list[tuple[asyncio.Task[dict], float, str]] = []
    if jev is None:
        logger.warning(
            "[MEETING][JEV] disabled reason=OPENROUTER_API_KEY_not_configured"
        )

    async def dispatch_events(events: list[MeetingEvent]) -> None:
        for event in events:
            logger.info(
                "%s emitted action=%s confidence=%.2f refinement=%s",
                _log_tag("JEV"),
                event.type,
                event.score,
                event.is_refinement,
            )
            await websocket.send_json(
                {
                    "type": "jev_decision",
                    "decision": event.type,
                    "confidence": event.score,
                }
            )
            if llm is not None:
                llm_tasks.append(
                    (
                        asyncio.create_task(llm.process_event(event)),
                        time.monotonic(),
                        event,
                    )
                )

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_bytes(), timeout=0.25)
                pending_audio_windows.append(data)
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if transcription_task and transcription_task.done():
                completed_task = transcription_task
                transcription_task = None
                latency = now - transcription_started_at if transcription_started_at else 0.0
                try:
                    transcript = completed_task.result()
                except Exception as error:
                    logger.error(
                        "%s failed latency=%.2fs error=%s: %s",
                        _log_tag("STT"),
                        latency,
                        type(error).__name__,
                        error,
                    )
                else:
                    window_start = transcribed_audio_end
                    new_segments = [
                        {
                            **segment,
                            "start": segment["start"] + transcribed_audio_end,
                            "end": segment["end"] + transcribed_audio_end,
                        }
                        for segment in transcript["segments"]
                    ]
                    transcribed_audio_end += transcript["audio_duration"]
                    logger.info(
                        "%s window=%.1fs-%.1fs latency=%.2fs segments=%d",
                        _log_tag("STT"),
                        window_start,
                        transcribed_audio_end,
                        latency,
                        len(new_segments),
                    )
                    if new_segments:
                        accepted_segments = meeting_context.add_segments(new_segments)
                        if accepted_segments:
                            jev_evaluation_requested = True
                            new_text = " ".join(
                                segment.text for segment in accepted_segments
                            )
                            logger.info(
                                "%s active_context accepted_segments=%d text_chars=%d\n%s",
                                _log_tag("CONTEXT"),
                                len(accepted_segments),
                                len(new_text),
                                meeting_context.as_text(),
                            )
                            await dispatch_events(
                                event_service.enrich(
                                    new_text,
                                    recent_context=meeting_context.as_text(),
                                    now=now,
                                )
                            )

            if (
                transcription_task is None
                and pending_audio_windows
            ):
                transcription_started_at = now
                audio_window = pending_audio_windows.popleft()
                transcription_task = asyncio.create_task(
                    transcribe_snapshot(audio_window)
                )

            if jev_task and jev_task.done():
                completed_jev_task = jev_task
                jev_task = None
                jev_latency = now - jev_started_at if jev_started_at else 0.0
                try:
                    decision, evaluated_new_context = completed_jev_task.result()
                except Exception as error:
                    logger.error(
                        "%s failed latency=%.2fs error=%s: %s",
                        _log_tag("JEV"),
                        jev_latency,
                        type(error).__name__,
                        error,
                    )
                else:
                    events = event_service.start_pending(
                        decision,
                        recent_context=meeting_context.as_text(),
                        triggering_context=evaluated_new_context,
                        now=now,
                    )
                    await dispatch_events(events)
                    logger.info(
                        "%s decision latency=%.2fs search_existing=%.2f suggest_task=%.2f "
                        "pending=%s finalized=%s cost=%s",
                        _log_tag("JEV"),
                        jev_latency,
                        decision.search_existing_work_probability,
                        decision.suggest_new_task_probability,
                        ",".join(event_service.pending_types()) or "-",
                        ",".join(event.type for event in events) or "-",
                        _format_cost(decision.cost),
                    )

            timed_out_events = event_service.check_timeouts(
                recent_context=meeting_context.as_text(), now=now
            )
            await dispatch_events(timed_out_events)

            if llm_tasks:
                still_running: list[
                    tuple[asyncio.Task[LlmResult], float, MeetingEvent]
                ] = []
                for task, started_at, event in llm_tasks:
                    if not task.done():
                        still_running.append((task, started_at, event))
                        continue
                    latency = now - started_at
                    try:
                        result = task.result()
                    except Exception as error:
                        logger.error(
                            "%s failed latency=%.2fs error=%s: %s",
                            _log_tag("LLM"),
                            latency,
                            type(error).__name__,
                            error,
                        )
                        continue
                    logger.info(
                        "%s action=%s refinement=%s latency=%.2fs cost=%s data=%s",
                        _log_tag("LLM"),
                        result.event_type,
                        event.is_refinement,
                        latency,
                        _format_cost(result.cost),
                        result.data,
                    )
                    if result.event_type == "SEARCH_EXISTING_WORK":
                        search_terms = GoogleDriveSearchTerms.from_mapping(result.data)
                        weighted_terms = [
                            term for term, _ in search_terms.weighted_terms()
                        ]
                        if weighted_terms and not event_service.accept_search_terms(
                            search_terms.primary_terms,
                            weighted_terms,
                            is_refinement=event.is_refinement,
                            now=now,
                        ):
                            logger.info(
                                "%s action suppressed action=SEARCH_EXISTING_WORK confidence=%.2f reason=%s",
                                _log_tag("JEV"),
                                event.score,
                                event_service.last_search_terms_suppression_reason
                                or "duplicate_search_topic",
                            )
                            logger.info(
                                "%s skipped reason=no_new_primary_terms",
                                _log_tag("DRIVE"),
                            )
                        elif weighted_terms:
                            drive_started_at = time.perf_counter()
                            logger.info(
                                "%s request query=%r refinement=%s terms=%s",
                                _log_tag("DRIVE"),
                                search_terms.query,
                                event.is_refinement,
                                weighted_terms,
                            )
                            drive_tasks.append(
                                (
                                    asyncio.create_task(
                                        google_drive.search_relevant_files(
                                            search_terms,
                                            top_k=5,
                                        )
                                    ),
                                    drive_started_at,
                                    search_terms.query,
                                )
                            )
                    await websocket.send_json(
                        {
                            "type": "llm_result",
                            "decision": result.event_type,
                            "data": result.data,
                        }
                    )
                llm_tasks = still_running

            if drive_tasks:
                still_running_drive: list[tuple[asyncio.Task[dict], float, str]] = []
                for task, started_at, query in drive_tasks:
                    if not task.done():
                        still_running_drive.append((task, started_at, query))
                        continue
                    total_retrieval_seconds = time.perf_counter() - started_at
                    try:
                        retrieval = task.result()
                    except Exception as error:
                        logger.error(
                            "%s failed latency=%.2fs error=%s: %s",
                            _log_tag("DRIVE"),
                            total_retrieval_seconds,
                            type(error).__name__,
                            error,
                        )
                        await websocket.send_json(
                            {
                                "type": "RELATED_FILES",
                                "source": "google_drive",
                                "query": query,
                                "files": [],
                                "error": str(error),
                            }
                        )
                        continue

                    logger.info(
                        "%s response query=%r latency=%.2fs search=%.0fms rank=%.0fms files=%s",
                        _log_tag("DRIVE"),
                        retrieval["query"],
                        total_retrieval_seconds,
                        retrieval["search_latency_ms"],
                        retrieval["ranking_latency_ms"],
                        [file["name"] for file in retrieval["files"]],
                    )
                    await websocket.send_json(
                        {
                            "type": "RELATED_FILES",
                            "source": "google_drive",
                            "query": retrieval["query"],
                            "files": retrieval["files"],
                        }
                    )
                drive_tasks = still_running_drive

            if (
                jev is not None
                and jev_task is None
                and jev_evaluation_requested
            ):
                jev_snapshot = meeting_context.prepare_jev_context()
                if jev_snapshot is not None:
                    jev_evaluation_requested = False
                    jev_started_at = now
                    logger.info(
                        "%s request\n  recent context:\n%s\n  new context:\n%s",
                        _log_tag("JEV"),
                        jev_snapshot.recent_context or "(empty)",
                        jev_snapshot.new_context,
                    )
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
        for task, _, _ in llm_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task, _, _ in drive_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if jev:
            await jev.close()
        if llm:
            await llm.close()
