import asyncio
import contextlib
import logging
import tempfile
import time
import secrets
from dataclasses import asdict
from collections import deque
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from contextlib import AsyncExitStack
from app.core.auth import consume_ticket, ensure_active, failure
from app.services.integrations import provider_session
from app.services.meeting_sessions import MeetingSessions, MeetingBusy, MeetingResumeExpired, context_state, restore_context, event_state, restore_events
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.services.event_service import EventService, MeetingEvent
from app.services.google_drive_service import (
    GoogleDriveSearchTerms,
    GoogleDriveService,
)
from app.services.jev_service import JevDecision, JevService
from app.services.llm_service import LlmResult, LlmService
from app.services.meeting_context_service import MeetingContextService, JevContextSnapshot
from app.services.meeting_timeline_service import MeetingTimelineService
from app.services.openrouter_stt_service import transcribe_realtime_chunk

router = APIRouter()
logger = logging.getLogger("uvicorn.error")
_LOG_COLORS = {
    "STT": "36",
    "CONTEXT": "34",
    "JEV": "33",
    "LLM": "35",
    "DRIVE": "32",
    "ATLASSIAN": "32",
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
) -> tuple[JevDecision, JevContextSnapshot]:
    snapshot = meeting_context.prepare_jev_context()
    if snapshot is None:
        raise RuntimeError("Jev evaluation started without new context")
    decision = await jev.decide(snapshot.recent_context, snapshot.new_context)
    return decision, snapshot

async def search_atlassian(atlassian, search_terms) -> dict:
    if not (await atlassian.status())["connected"]:
        return {"query": search_terms.query, "sources": {}, "errors": {}}
    return await atlassian.search_relevant_files(search_terms, top_k=5)


@router.websocket("/ws/meeting")
async def meeting_stream(websocket: WebSocket):
    registry = getattr(websocket.app.state, 'meetings', None)
    if registry and registry.draining:
        await websocket.close(code=1012, reason='MI-CON-004')
        return
    await websocket.accept()
    if registry:
        registry.sockets.add(websocket)
    db = websocket.app.state.users
    providers = AsyncExitStack()
    session = None
    try:
        hello = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if len(hello) > 512:
            raise failure()
        import json
        payload = json.loads(hello)
        principal = await consume_ticket(payload.get('ticket'), db)
        session = MeetingSessions(db, principal, payload.get('meetingId') or secrets.token_hex(16))
        restored = await session.acquire()
        last_ack = payload.get('lastAck', 0)
        if not isinstance(last_ack, int) or isinstance(last_ack, bool) or last_ack < 0 or last_ack > restored.get('ack', 0):
            raise MeetingResumeExpired()
        sequenced = bool(payload.get('meetingId'))
        google_drive = await providers.enter_async_context(provider_session(db, principal, 'google_drive'))
        atlassian = await providers.enter_async_context(provider_session(db, principal, 'atlassian'))
    except Exception as exc:
        await providers.aclose()
        if session and session.version is not None:
            await session.release()
        if registry:
            registry.sockets.discard(websocket)
        code = 4409 if isinstance(exc, MeetingResumeExpired) else 4410 if isinstance(exc, MeetingBusy) else 4401 if isinstance(exc, HTTPException) and exc.status_code == 401 else 1013
        await websocket.close(code=code, reason='MI-CON-007' if code == 4409 else 'MI-AUTH-105' if code == 4401 else 'MI-CON-004')
        return

    async def checkpoint():
        await session.save({'ack': ack, 'audioEnd': transcribed_audio_end,
            'transcript': transcript_text, 'context': context_state(meeting_context),
            'events': event_state(event_service), 'timeline': timeline.snapshot(), 'jobs': jobs})

    async def safe_send(message):
        await ensure_active(principal, db)
        await checkpoint()
        await websocket.send_json(message)

    last_auth_check = 0.0
    pending_audio_windows: deque[tuple[int, bytes]] = deque()
    ack = int(restored.get('ack', 0))
    received_sequence = ack
    audio_sequence = None
    transcription_sequence = None
    jobs = restored.get('jobs', {})
    task_jobs = {}
    transcribed_audio_end = restored.get('audioEnd', 0.0)
    transcript_text = restored.get('transcript', '')
    empty_audio_seconds = 0.0
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
    timeline = MeetingTimelineService()
    if restored.get('timeline'):
        timeline.restore(restored['timeline'])
    restore_context(meeting_context, restored.get('context', {}))
    restore_events(event_service, restored.get('events', {}))
    jev_task: asyncio.Task[tuple[JevDecision, JevContextSnapshot]] | None = None
    jev_started_at: float | None = None
    jev_evaluation_requested = bool(meeting_context.prepare_jev_context())
    llm_tasks: list[tuple[asyncio.Task[LlmResult], float, MeetingEvent]] = []
    drive_tasks: list[tuple[asyncio.Task[dict], float, str]] = []
    atlassian_tasks: list[tuple[asyncio.Task[dict], float, str]] = []
    if jev is None:
        logger.warning(
            "[MEETING][JEV] disabled reason=OPENROUTER_API_KEY_not_configured"
        )

    async def dispatch_events(events: list[MeetingEvent]) -> None:
        entries = [(secrets.token_hex(16), event) for event in events]
        if llm:
            for job_id, event in entries:
                jobs[job_id] = {'stage': 'llm', 'event': asdict(event)}
        await checkpoint()
        for job_id, event in entries:
            logger.info(
                "%s emitted action=%s confidence=%.2f refinement=%s",
                _log_tag("JEV"),
                event.type,
                event.score,
                event.is_refinement,
            )
            await safe_send(
                {
                    "type": "jev_decision",
                    "decision": event.type,
                    "confidence": event.score,
                }
            )
            if llm is not None:
                logger.info(
                    "%s request action=%s refinement=%s",
                    _log_tag("LLM"),
                    event.type,
                    event.is_refinement,
                )
                task = asyncio.create_task(llm.process_event(event))
                task_jobs[task] = job_id
                llm_tasks.append((task, time.monotonic(), event))

    def start_search(job_id, job):
        terms = GoogleDriveSearchTerms.from_mapping(job['terms'])
        if 'google_drive' not in job.get('done', []):
            task = asyncio.create_task(google_drive.search_relevant_files(terms, top_k=5))
            task_jobs[task] = job_id
            drive_tasks.append((task, time.perf_counter(), job['query']))
        if 'atlassian' not in job.get('done', []):
            task = asyncio.create_task(search_atlassian(atlassian, terms))
            task_jobs[task] = job_id
            atlassian_tasks.append((task, time.perf_counter(), job['query']))

    def finish_source(task, provider):
        job_id = task_jobs.pop(task)
        job = jobs.get(job_id)
        if job:
            job.setdefault('done', []).append(provider)
            if len(job['done']) == 2:
                jobs.pop(job_id)

    for job_id, job in list(jobs.items()):
        if job['stage'] == 'llm' and llm:
            event = MeetingEvent(**job['event'])
            task = asyncio.create_task(llm.process_event(event))
            task_jobs[task] = job_id
            llm_tasks.append((task, time.monotonic(), event))
        elif job['stage'] == 'search':
            start_search(job_id, job)


    last_heartbeat = 0.0
    last_client_ping = time.monotonic()
    try:
        await safe_send({'type': 'auth_ready', 'expiresAt': principal.expires,
            'expiresIn': max(0, principal.expires - int(time.time())), 'ack': ack, 'meetingId': session.session_id})
        await safe_send(timeline.snapshot())
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=0.25)
                if message['type'] == 'websocket.disconnect':
                    raise WebSocketDisconnect()
                if message.get('text') is not None:
                    if len(message['text']) > 512:
                        raise failure()
                    await db.limit(principal.sub, 'meeting-controls', 120, 60)
                    control = json.loads(message['text'])
                    if control.get('type') == 'ping':
                        last_client_ping = time.monotonic()
                        await safe_send({'type': 'pong'})
                    elif control.get('type') == 'audio':
                        value = control.get('sequence')
                        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or audio_sequence is not None:
                            raise ValueError('Invalid audio sequence')
                        audio_sequence = value
                    elif control.get('type') == 'end':
                        raise WebSocketDisconnect()
                    else:
                        renewed = await consume_ticket(control.get('ticket'), db)
                        if renewed.sub != principal.sub or renewed.family != principal.family:
                            raise failure()
                        principal = renewed
                        session.principal = renewed
                        await safe_send({'type': 'auth_ready', 'expiresAt': principal.expires,
                            'expiresIn': max(0, principal.expires - int(time.time())), 'ack': ack})
                elif message.get('bytes'):
                    await ensure_active(principal, db)
                    if len(message['bytes']) > 2_000_000 or len(pending_audio_windows) >= 20:
                        raise failure('MI-AUTH-102', 429)
                    sequence = audio_sequence if sequenced else received_sequence + 1
                    audio_sequence = None
                    if sequence is None or sequence > received_sequence + 1:
                        raise ValueError('Missing audio sequence')
                    if sequence > received_sequence:
                        received_sequence = sequence
                        pending_audio_windows.append((sequence, message['bytes']))
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if sequenced and now - last_client_ping > 50:
                await websocket.close(code=1013, reason='MI-CON-004')
                break
            if now - last_heartbeat >= 5:
                await safe_send({'type': 'heartbeat'})
                last_heartbeat = now
            if now - last_auth_check >= 5:
                await ensure_active(principal, db)
                last_auth_check = now
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
                    # Leave this window unacknowledged; the reconnect replays it.
                    await websocket.close(code=1013, reason='MI-CON-004')
                    break
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
                        "%s window=%.1fs-%.1fs latency=%.2fs segments=%d audio_rms=%.6f audio_peak=%.6f",
                        _log_tag("STT"),
                        window_start,
                        transcribed_audio_end,
                        latency,
                        len(new_segments),
                        transcript.get("audio_rms", 0.0),
                        transcript.get("audio_peak", 0.0),
                    )
                    empty_audio_seconds = 0.0 if new_segments else empty_audio_seconds + transcript["audio_duration"]
                    if new_segments:
                        transcript_text = (transcript_text + " " + " ".join(segment["text"] for segment in new_segments)).strip()[-3000:]
                    warning = None
                    if empty_audio_seconds >= 15 and transcript.get("audio_rms", 0) > 0.001:
                        warning = "Audio is arriving, but no words were recognized. Check the microphone input and speak clearly."
                    if new_segments:
                        accepted_segments = meeting_context.add_segments(new_segments)
                        if accepted_segments:
                            ack = transcription_sequence
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

                    ack = transcription_sequence
                    await safe_send({"type": "transcription_state", "transcript": transcript_text, "warning": warning})
                    await safe_send({'type': 'audio_ack', 'sequence': ack})

            if (
                transcription_task is None
                and pending_audio_windows
            ):
                transcription_started_at = now
                transcription_sequence, audio_window = pending_audio_windows.popleft()
                transcription_task = asyncio.create_task(
                    transcribe_snapshot(audio_window)
                )

            if jev_task and jev_task.done():
                completed_jev_task = jev_task
                jev_task = None
                jev_latency = now - jev_started_at if jev_started_at else 0.0
                try:
                    decision, evaluated_snapshot = completed_jev_task.result()
                except Exception as error:
                    logger.error(
                        "%s failed latency=%.2fs error=%s: %s",
                        _log_tag("JEV"),
                        jev_latency,
                        type(error).__name__,
                        error,
                    )
                else:
                    meeting_context.complete_jev_evaluation(evaluated_snapshot, time.monotonic())
                    events = event_service.start_pending(
                        decision,
                        recent_context=meeting_context.as_text(),
                        triggering_context=evaluated_snapshot.new_context,
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
                    job_id = task_jobs.pop(task)
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
                        jobs.pop(job_id, None)
                        continue
                    jobs.pop(job_id, None)
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
                            is_topic_check=event.is_topic_check,
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
                            jobs[job_id] = {'stage': 'search', 'terms': result.data,
                                'query': search_terms.query, 'done': []}
                            await checkpoint()
                            start_search(job_id, jobs[job_id])
                    if result.event_type == "SUGGEST_NEW_TASK":
                        title = result.data.get("task_title", "")
                        description = result.data.get("task_description", "")
                        if title and description:
                            timeline.add_task(
                                title=title,
                                description=description,
                                confidence=event.score,
                            )
                            await safe_send(timeline.snapshot())
                        else:
                            logger.warning(
                                "%s skipped incomplete task suggestion data=%s",
                                _log_tag("LLM"),
                                result.data,
                            )
                    await safe_send(
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
                    finish_source(task, 'google_drive')
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
                        await safe_send(
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
                    search_item = timeline.add_search(
                        query=retrieval["query"],
                        files=retrieval["files"],
                    )
                    await safe_send(
                        {
                            "type": "RELATED_FILES",
                            "source": "google_drive",
                            "query": retrieval["query"],
                            "files": retrieval["files"],
                        }
                    )
                    if search_item is not None:
                        await safe_send(timeline.snapshot())
                drive_tasks = still_running_drive

            if atlassian_tasks:
                still_running_atlassian: list[tuple[asyncio.Task[dict], float, str]] = []
                for task, started_at, query in atlassian_tasks:
                    if not task.done():
                        still_running_atlassian.append((task, started_at, query))
                        continue
                    finish_source(task, 'atlassian')
                    try:
                        retrieval = task.result()
                    except Exception as error:
                        logger.error("%s failed latency=%.2fs error=%s", _log_tag("ATLASSIAN"), time.perf_counter() - started_at, error)
                        continue
                    previous_revision = timeline.snapshot()["revision"]
                    for source_id, source_label in (("jira", "Jira"), ("confluence", "Confluence")):
                        files = retrieval["sources"].get(source_id, [])
                        if not files:
                            continue
                        timeline.add_search(
                            query=retrieval["query"], files=files,
                            source_id=source_id, source_label=source_label,
                        )
                    if timeline.snapshot()["revision"] != previous_revision:
                        await safe_send(timeline.snapshot())
                    else:
                        await checkpoint()
                    for source_id, error in retrieval.get("errors", {}).items():
                        logger.warning("%s search source=%s query=%r error=%s", _log_tag("ATLASSIAN"), source_id, query, error)
                atlassian_tasks = still_running_atlassian

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
            # Changed work is captured before the next receive can suspend.
            # Heartbeats/outputs also checkpoint, so no per-loop extra write is needed.
    except MeetingBusy:
        await websocket.close(code=4410, reason='MI-CON-004')
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code == 401 else 1013, reason='MI-AUTH-105' if exc.status_code == 401 else 'MI-CON-004')
    except ValueError:
        await websocket.close(code=4401, reason='MI-AUTH-105')
    except WebSocketDisconnect:
        if llm_tasks or drive_tasks or atlassian_tasks:
            logger.warning(
                "%s client disconnected pending_llm=%d pending_drive=%d pending_atlassian=%d",
                _log_tag("LLM"),
                len(llm_tasks),
                len(drive_tasks),
                len(atlassian_tasks),
            )
    except Exception as exc:
        logger.warning('MI-CON-004 meeting interrupted (%s)', type(exc).__name__)
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason='MI-CON-004')
    finally:
        # Save pending read-only jobs before cancellation; another process can
        # replay them using the same authenticated meeting identity.
        with contextlib.suppress(Exception):
            await checkpoint()
        if registry:
            registry.sockets.discard(websocket)
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
        for task, _, _ in atlassian_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await providers.aclose()
        if jev:
            await jev.close()
        if llm:
            await llm.close()
        await session.release()
