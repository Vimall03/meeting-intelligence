import asyncio
import contextlib
import logging
import time
import secrets
from dataclasses import asdict
from collections import deque

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from contextlib import AsyncExitStack
from app.core.auth import consume_ticket, ensure_active, failure
from app.services.integrations import provider_session
from app.services.meeting_sessions import MeetingSessions, MeetingCheckpoints, MeetingBusy, MeetingResumeExpired, context_state, restore_context, event_state, restore_events

from app.core.config import settings
from app.api.routes.mom import available as mom_available, repository_for as mom_repository
from app.mom.repository import MomError
from app.core.observability import (
    log_event, log_context, set_log_context, new_id, error_fields, observe_operation,
)
from app.core.user_messages import product_error
from app.services.event_service import EventService, MeetingEvent
from app.services.google_drive_service import (
    GoogleDriveSearchTerms,
    GoogleDriveService,
)
from app.services.jev_service import JevDecision, JevService
from app.services.llm_service import LlmResult, LlmService
from app.services.meeting_context_service import MeetingContextService, JevContextSnapshot
from app.services.meeting_intent_service import decide_meeting_intent
from app.services.meeting_timeline_service import MeetingTimelineService
from app.services.streaming_stt import AmazonStreamingTranscription, StreamingTranscriptionError

router = APIRouter()


async def evaluate_new_context(
    meeting_context: MeetingContextService,
    jev: JevService,
    llm: LlmService | None = None,
    *, known_tasks=(), known_search_terms=(),
) -> tuple[JevDecision, JevContextSnapshot]:
    snapshot = meeting_context.prepare_jev_context()
    if snapshot is None:
        raise RuntimeError("Jev evaluation started without new context")
    decision = await decide_meeting_intent(jev, llm, snapshot.recent_context, snapshot.new_context,
        search_threshold=settings.jev_search_threshold, task_threshold=settings.jev_task_threshold,
        review_minimum=settings.intent_review_minimum, latest_context=snapshot.latest_context,
        background_context=snapshot.background_context, known_tasks=known_tasks, known_search_terms=known_search_terms)
    return decision, snapshot

async def search_atlassian(atlassian, search_terms) -> dict:
    if not (await atlassian.status())["connected"]:
        return {"query": search_terms.query, "sources": {}, "errors": {}}
    return await atlassian.search_relevant_files(search_terms, top_k=5)


@router.websocket("/ws/meeting/stream")
async def meeting_stream(websocket: WebSocket):
    with log_context(request_id=new_id(), connection_id=new_id(), user_id=None,
                     meeting_id=None, operation_id=None, job_id=None, sequence=None):
        await _meeting_stream(websocket)


async def _meeting_stream(websocket: WebSocket):
    connection_started = time.perf_counter()
    registry = getattr(websocket.app.state, 'meetings', None)
    if registry and registry.draining:
        log_event('meeting.rejected', reason='service_draining', close_code=1012, support_code='MI-CON-004')
        await websocket.close(code=1012, reason='MI-CON-004')
        return
    await websocket.accept()
    if registry and (registry.draining or len(registry.sockets) >= settings.streaming_max_connections):
        await websocket.close(code=1013, reason='MI-CON-009')
        return
    if registry:
        registry.sockets.add(websocket)
    db = websocket.app.state.users
    providers = AsyncExitStack()
    session = None
    mom_repo = None
    mom_attachment = None
    live_enabled = True
    try:
        hello = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if len(hello) > 512:
            raise failure()
        import json
        payload = json.loads(hello)
        principal = await consume_ticket(payload.get('ticket'), db)
        if payload.get('audioFormat') != 'pcm_s16le_16000_v2':
            raise StreamingTranscriptionError()
        if not payload.get('meetingId'):
            raise MeetingResumeExpired()
        set_log_context(user_id=principal.sub)
        session = MeetingSessions(db, principal, payload['meetingId'])
        set_log_context(meeting_id=session.session_id)
        restored = await session.acquire()
        if restored and restored.get('transport') != 'pcm_s16le_16000_v2':
            raise MeetingResumeExpired()
        last_ack = payload.get('lastAck', 0)
        if not isinstance(last_ack, int) or isinstance(last_ack, bool) or last_ack < 0 or last_ack > restored.get('ack', 0):
            raise MeetingResumeExpired()
        if mom_available():
            mom_repo = mom_repository(websocket.app)
            prefs = await asyncio.to_thread(mom_repo.preferences, principal.sub)
            if payload.get('roomId'):
                mom_attachment = await asyncio.to_thread(mom_repo.begin, principal.sub,
                    payload['roomId'], session.session_id, payload.get('momSessionId'))
            elif prefs['mode'] != 'live_only':
                raise MomError('MI-MOM-003')
            if mom_attachment:
                saved = await asyncio.to_thread(mom_repo.load_checkpoint, mom_attachment, session.session_id)
                if saved is None and restored.get('ack', 0):
                    raise MeetingResumeExpired()
                if saved is not None:
                    restored = saved
                    if restored.get('transport') != 'pcm_s16le_16000_v2' or last_ack > restored.get('ack', 0):
                        raise MeetingResumeExpired()
            live_enabled = not mom_attachment or mom_attachment['mode'] != 'mom_only'
        if live_enabled:
            google_drive = await providers.enter_async_context(provider_session(db, principal, 'google_drive'))
            atlassian = await providers.enter_async_context(provider_session(db, principal, 'atlassian'))
        else:
            google_drive = atlassian = None
    except Exception as exc:
        await providers.aclose()
        if mom_attachment:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(mom_repo.detach, mom_attachment, incomplete=True)
        if session and session.version is not None:
            await session.release()
        if registry:
            registry.sockets.discard(websocket)
        code = 4409 if isinstance(exc, MeetingResumeExpired) else 4410 if isinstance(exc, MeetingBusy) else 4401 if isinstance(exc, HTTPException) and exc.status_code == 401 else 1013
        reason = exc.code if isinstance(exc, MomError) else 'MI-STT-001' if isinstance(exc, StreamingTranscriptionError) else 'MI-CON-007' if code == 4409 else 'MI-AUTH-105' if code == 4401 else 'MI-CON-004'
        log_event('meeting.rejected', level=logging.WARNING, **{**error_fields(exc), 'close_code': code, 'support_code': reason})
        await websocket.close(code=code, reason=reason)
        return

    log_event('meeting.connected', resumed=bool(restored), ack=restored.get('ack', 0),
              pending_jobs=len(restored.get('jobs', {})), duration_ms=round((time.perf_counter() - connection_started) * 1000, 2))
    checkpoint_count = 0
    checkpoint_total_ms = 0.0
    checkpoint_max_ms = 0.0
    class MinutesCheckpointSession:
        async def renew(self):
            await asyncio.to_thread(mom_repo.touch, mom_attachment)
            await session.renew()

        async def save(self, state):
            await asyncio.to_thread(mom_repo.save_checkpoint, mom_attachment, session.session_id, state)
            # The existing table has PITR. Do not place new MOM content in its backups.
            await session.save({'ack': state['ack'], 'audioEnd': state['audioEnd'],
                'transport': state['transport'],
                'transcript': '', 'context': {}, 'events': {}, 'jobs': {},
                'timeline': {'session_id': state['timeline']['session_id'], 'revision': state['timeline']['revision'], 'items': []}})
    checkpoints = MeetingCheckpoints(MinutesCheckpointSession() if mom_attachment else session)

    async def checkpoint():
        nonlocal checkpoint_count, checkpoint_total_ms, checkpoint_max_ms
        started = time.perf_counter()
        wrote = False
        try:
            wrote = await checkpoints.save({'ack': ack, 'audioEnd': transcribed_audio_end,
                'transcript': transcript_text, 'context': context_state(meeting_context),
                'events': event_state(event_service), 'timeline': timeline.snapshot(), 'jobs': jobs,
                'transport': 'pcm_s16le_16000_v2', 'recapPending': recap_pending, 'recapSequence': recap_sequence})
        except Exception as exc:
            log_event('meeting.checkpoint.failed', level=logging.WARNING,
                      **{**error_fields(exc), 'support_code': 'MI-CON-004'})
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            if wrote:
                checkpoint_count += 1
                checkpoint_total_ms += duration_ms
                checkpoint_max_ms = max(checkpoint_max_ms, duration_ms)
            if duration_ms >= 500:
                log_event('meeting.checkpoint.slow', duration_ms=round(duration_ms, 2), ack=ack)

    async def safe_send(message):
        await ensure_active(principal, db)
        await checkpoint()
        await websocket.send_json(message)

    last_auth_check = 0.0
    pending_audio: deque[tuple[int, bytes]] = deque()
    ack = int(restored.get('ack', 0))
    received_sequence = ack
    audio_sequence = None
    transcription_sequence = None
    jobs = restored.get('jobs', {}) if live_enabled else {}
    task_jobs = {}
    stream_offset = ack / 16000
    transcribed_audio_end = stream_offset
    recap_sequence = restored.get('recapSequence', 0)
    recap_pending = restored.get('recapPending')
    recap_applied_sequence = None
    transcript_text = restored.get('transcript', '')
    streaming_session = None
    streaming_updates = None
    streaming_update_task = None
    streaming_finish_task = None
    streaming_ended = False
    frame_budget_at = time.monotonic()
    frame_budget = 50.0
    meeting_context = MeetingContextService(
        window_seconds=settings.meeting_context_window_seconds,
        turn_gap_seconds=settings.meeting_context_turn_gap_seconds,
        background_window_seconds=settings.meeting_background_window_seconds,
    )
    transcription_task: asyncio.Task | None = None
    transcription_started_at: float | None = None
    jev = (
        JevService(
            api_key=settings.openrouter_api_key,
            model=settings.jev_model,
            endpoint=settings.jev_decisions_url,
        )
        if settings.openrouter_api_key and live_enabled
        else None
    )
    llm = (
        LlmService(
            api_key=settings.openrouter_api_key,
            model=settings.gemma_model,
            endpoint=settings.gemma_chat_completions_url,
        )
        if settings.openrouter_api_key and live_enabled
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
    jev_attempt = 0
    jev_retry_at = 0.0
    jev_retry_context = None
    jev_sequence = None
    llm_tasks: list[tuple[asyncio.Task[LlmResult], float, MeetingEvent]] = []
    drive_tasks: list[tuple[asyncio.Task[dict], float, str]] = []
    atlassian_tasks: list[tuple[asyncio.Task[dict], float, str]] = []
    if jev is None:
        log_event('meeting.stage.disabled', level=logging.WARNING, stage='jev', reason='not_configured')

    async def dispatch_events(events: list[MeetingEvent]) -> None:
        entries = [(secrets.token_hex(16), event) for event in events]
        if llm:
            for job_id, event in entries:
                jobs[job_id] = {'stage': 'llm',
                    'event': {key: value for key, value in asdict(event).items() if key != 'background_context'},
                    'background_context': event.background_context}
        await checkpoint()
        for job_id, event in entries:
            log_event('meeting.event', job_id=job_id, action=event.type,
                      confidence=event.score, refinement=event.is_refinement)
            await safe_send(
                {
                    "type": "jev_decision",
                    "decision": event.type,
                    "confidence": event.score,
                }
            )
            if llm is not None:
                task = asyncio.create_task(observe_operation('llm', llm.process_event(event),
                    job_id=job_id, model=settings.gemma_model, action=event.type, refinement=event.is_refinement))
                task_jobs[task] = job_id
                llm_tasks.append((task, time.monotonic(), event))

    def start_search(job_id, job):
        terms = GoogleDriveSearchTerms.from_mapping(job['terms'])
        if 'google_drive' not in job.get('done', []):
            task = asyncio.create_task(observe_operation('drive', google_drive.search_relevant_files(terms, top_k=5),
                job_id=job_id, provider='google_drive', term_count=len(terms.weighted_terms())))
            task_jobs[task] = job_id
            drive_tasks.append((task, time.perf_counter(), job['query']))
        if 'atlassian' not in job.get('done', []):
            task = asyncio.create_task(observe_operation('atlassian', search_atlassian(atlassian, terms),
                job_id=job_id, provider='atlassian', term_count=len(terms.weighted_terms())))
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
            event_data = {**job['event']}
            event_data['background_context'] = job.get('background_context', event_data.get('background_context', ''))
            event = MeetingEvent(**event_data)
            task = asyncio.create_task(observe_operation('llm', llm.process_event(event),
                job_id=job_id, model=settings.gemma_model, action=event.type, refinement=event.is_refinement))
            task_jobs[task] = job_id
            llm_tasks.append((task, time.monotonic(), event))
        elif job['stage'] == 'search':
            start_search(job_id, job)


    async def accept_transcript(transcript):
        nonlocal transcribed_audio_end, transcript_text, ack, jev_evaluation_requested, recap_pending, recap_applied_sequence, recap_sequence
        window_start = transcribed_audio_end
        new_segments = [
            {
                **segment,
                "start": segment["start"] + transcribed_audio_end,
                "end": segment["end"] + transcribed_audio_end,
            }
            for segment in transcript["segments"]
        ]
        transcribed_audio_end += transcript['audio_duration']
        ack = round(transcribed_audio_end * 16000)
        if mom_attachment and new_segments:
            # Durable outbox: save these exact segments with the cursor first.
            # Re-appending after a crash uses the same write-once object key.
            recap_sequence += 1
            recap_pending = {'sequence': recap_sequence, 'segments': new_segments}
        log_event('meeting.transcription.accepted', sequence=ack,
            audio_start_seconds=window_start, audio_end_seconds=transcribed_audio_end,
            segment_count=len(new_segments))
        if new_segments:
            transcript_text = (transcript_text + " " + " ".join(segment["text"] for segment in new_segments)).strip()[-3000:]
        if new_segments and live_enabled:
            accepted_segments = meeting_context.add_segments(new_segments)
            if accepted_segments:
                jev_evaluation_requested = True
                new_text = " ".join(
                    segment.text for segment in accepted_segments
                )
                log_event('meeting.context.updated', sequence=ack,
                          segment_count=len(accepted_segments), text_chars=len(new_text))
                await dispatch_events(
                    event_service.enrich(new_text, recent_context=meeting_context.as_text(),
                        now=time.monotonic(), background_context=meeting_context.background_text())
                )

        await checkpoint()
        if mom_attachment and recap_pending and recap_applied_sequence != recap_pending['sequence']:
            await asyncio.to_thread(mom_repo.append, mom_attachment, session.session_id,
                recap_pending['sequence'], recap_pending['segments'])
            recap_applied_sequence = recap_pending['sequence']
        await websocket.send_json({'type': 'transcription_state', 'transcript': transcript_text, 'warning': None})
        await websocket.send_json({'type': 'audio_ack', 'sequence': ack})

    last_heartbeat = 0.0
    last_client_ping = time.monotonic()
    close_reason = 'disconnected'
    receive_task = None
    ending = False
    finish_minutes = False
    last_mom_touch = 0.0
    try:
        if mom_attachment and recap_pending and recap_applied_sequence != recap_pending['sequence']:
            await asyncio.to_thread(mom_repo.append, mom_attachment, session.session_id,
                recap_pending['sequence'], recap_pending['segments'])
            recap_applied_sequence = recap_pending['sequence']
        streaming_started = time.perf_counter()
        streaming_session = await AmazonStreamingTranscription(region=settings.aws_region,
            language=settings.streaming_stt_language).start()
        log_event('meeting.streaming.connected', provider='amazon-transcribe', stage='stt',
            duration_ms=round((time.perf_counter()-streaming_started)*1000,2))
        streaming_updates = streaming_session.updates().__aiter__()
        streaming_update_task = asyncio.create_task(anext(streaming_updates))
        await safe_send({'type': 'auth_ready', 'expiresAt': principal.expires,
            'expiresIn': max(0, principal.expires - int(time.time())), 'ack': ack, 'meetingId': session.session_id,
            'audioFormat': 'pcm_s16le_16000_v2'})
        await safe_send(timeline.snapshot())
        if mom_attachment:
            await safe_send({'type': 'mom_session', 'sessionId': mom_attachment['sessionId'],
                'mode': mom_attachment['mode'], 'graceSeconds': settings.mom_grace_seconds})
        while True:
            try:
                if receive_task is None and not ending:
                    receive_task = asyncio.create_task(websocket.receive())
                work = {*(task for task, _, _ in llm_tasks),
                        *(task for task, _, _ in drive_tasks), *(task for task, _, _ in atlassian_tasks)}
                if receive_task:
                    work.add(receive_task)
                if transcription_task:
                    work.add(transcription_task)
                if jev_task:
                    work.add(jev_task)
                if streaming_update_task:
                    work.add(streaming_update_task)
                if streaming_finish_task:
                    work.add(streaming_finish_task)
                if not work:
                    ready = set()
                else:
                    ready, _ = await asyncio.wait(work, timeout=0.25, return_when=asyncio.FIRST_COMPLETED)
                message = {}
                if receive_task in ready:
                    received, receive_task = receive_task, None
                    message = received.result()
                if message.get('type') == 'websocket.disconnect':
                    raise WebSocketDisconnect()
                if message.get('text') is not None:
                    if len(message['text']) > 512:
                        raise failure()
                    control = json.loads(message['text'])
                    if control.get('type') == 'audio':
                        # Storage/analysis can delay reads, then release valid queued
                        # frames together. Permit a bounded burst while retaining a
                        # 2x-real-time sustained budget and the separate queue cap.
                        frame_now = time.monotonic()
                        frame_budget = min(50.0, frame_budget + (frame_now-frame_budget_at)*10)
                        frame_budget_at = frame_now
                        if frame_budget < 1: raise failure('MI-AUTH-102', 429)
                        frame_budget -= 1
                    else:
                        await db.limit(principal.sub, 'meeting-controls', 120, 60)
                    if control.get('type') == 'ping':
                        last_client_ping = time.monotonic()
                        await safe_send({'type': 'pong'})
                    elif control.get('type') == 'audio':
                        value = control.get('startSample')
                        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or audio_sequence is not None:
                            raise ValueError('Invalid audio sequence')
                        audio_sequence = value
                    elif control.get('type') in ('end', 'finish'):
                        final_sequence = control.get('lastSequence')
                        if not isinstance(final_sequence, int) or isinstance(final_sequence, bool) or final_sequence != received_sequence or audio_sequence is not None:
                            raise MomError('MI-MOM-003')
                        ending = True
                        finish_minutes = control['type'] == 'finish'
                    else:
                        renewed = await consume_ticket(control.get('ticket'), db)
                        if renewed.sub != principal.sub or renewed.family != principal.family:
                            raise failure()
                        principal = renewed
                        session.principal = renewed
                        log_event('meeting.auth.renewed')
                        await safe_send({'type': 'auth_ready', 'expiresAt': principal.expires,
                            'expiresIn': max(0, principal.expires - int(time.time())), 'ack': ack,
                            'audioFormat': 'pcm_s16le_16000_v2'})
                elif message.get('bytes'):
                    if len(message['bytes']) > 6400 or len(message['bytes']) % 2:
                        raise StreamingTranscriptionError()
                    if len(pending_audio) >= 50:
                        raise failure('MI-AUTH-102', 429)
                    start = audio_sequence
                    audio_sequence = None
                    if start is None or start > received_sequence:
                        raise ValueError('Missing audio samples')
                    end = start + len(message['bytes']) // 2
                    if end > 21600 * 16000:
                        raise StreamingTranscriptionError()
                    if end > received_sequence:
                        pcm = message['bytes'][(received_sequence-start)*2:]
                        received_sequence = end
                        pending_audio.append((end, pcm))
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if streaming_update_task and streaming_update_task.done():
                completed_update, streaming_update_task = streaming_update_task, None
                try:
                    update = completed_update.result()
                except StopAsyncIteration:
                    streaming_ended = True
                    if not ending: raise StreamingTranscriptionError()
                else:
                    if update.final:
                        absolute_end = stream_offset + update.end
                        end_sample = round(absolute_end * 16000)
                        if end_sample > received_sequence or end_sample < ack:
                            raise StreamingTranscriptionError()
                        if end_sample > ack:
                            await accept_transcript({'audio_duration': absolute_end - transcribed_audio_end,
                                'segments': [{'start': max(0, stream_offset+update.start-transcribed_audio_end),
                                    'end': absolute_end-transcribed_audio_end, 'text': update.text}] if update.text else []})
                    else:
                        # Previews never enter checkpoints, tasks, searches or recaps.
                        await websocket.send_json({'type':'transcription_partial', 'text':update.text})
                    streaming_update_task = asyncio.create_task(anext(streaming_updates))
            if streaming_finish_task and streaming_finish_task.done():
                streaming_finish_task.result()
                streaming_finish_task = None
            if mom_attachment and now - last_mom_touch >= 5:
                await asyncio.to_thread(mom_repo.touch, mom_attachment)
                last_mom_touch = now
            if ending and transcription_task is None and not pending_audio:
                if not streaming_session.closed:
                    streaming_finish_task = asyncio.create_task(streaming_session.finish())
                if streaming_ended:
                    if ack != received_sequence:
                        raise StreamingTranscriptionError()
                    await safe_send({'type': 'meeting_segment_ended', 'ack': ack, 'minutesFinalizing': finish_minutes})
                    close_reason = 'finished' if finish_minutes else 'stopped'
                    break
            if not ending and now - last_client_ping > 50:
                close_reason = 'heartbeat_timeout'
                log_event('meeting.heartbeat.timeout', level=logging.WARNING, close_code=1013, support_code='MI-CON-004')
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
                latency = time.monotonic() - transcription_started_at if transcription_started_at else 0.0
                try:
                    completed_task.result()
                except Exception as error:
                    close_reason = 'transcription_failed'
                    log_event('meeting.transcription.failed', level=logging.WARNING, sequence=transcription_sequence,
                              elapsed_ms=round(latency * 1000, 2), **{**error_fields(error), 'support_code': 'MI-CON-004'})
                    # Leave this window unacknowledged; the reconnect replays it.
                    await websocket.close(code=1013, reason='MI-CON-004')
                    break
            if (
                transcription_task is None
                and pending_audio
            ):
                transcription_started_at = now
                transcription_sequence, audio_window = pending_audio.popleft()
                transcription_task = asyncio.create_task(streaming_session.send(audio_window))

            if jev_task and jev_task.done():
                completed_jev_task = jev_task
                jev_task = None
                jev_latency = time.monotonic() - jev_started_at if jev_started_at else 0.0
                try:
                    decision, evaluated_snapshot = completed_jev_task.result()
                except Exception as error:
                    log_event('meeting.decision.failed', level=logging.WARNING,
                              elapsed_ms=round(jev_latency * 1000, 2), attempt=jev_attempt,
                              sequence=jev_sequence,
                              **error_fields(error))
                    # Keep the snapshot unconsumed and retry even if the
                    # speaker stopped. New speech gets its own retry budget.
                    if jev_attempt < 3:
                        jev_evaluation_requested = True
                        jev_retry_at = time.monotonic() + 0.5 * 2 ** (jev_attempt - 1)
                        log_event('meeting.decision.retry', attempt=jev_attempt + 1,
                                  reason='decision_unavailable')
                    else:
                        log_event('meeting.decision.exhausted', level=logging.WARNING,
                                  attempt=jev_attempt, reason='decision_unavailable')
                else:
                    jev_attempt = 0
                    jev_retry_at = 0.0
                    meeting_context.complete_jev_evaluation(evaluated_snapshot, time.monotonic(),
                        retain_for_followup=decision.wait_for_continuation)
                    if decision.wait_for_continuation:
                        log_event('meeting.intent.continuation', sequence=jev_sequence,
                                  reason='awaiting_more_speech', text_chars=len(evaluated_snapshot.new_context))
                    events = event_service.start_pending(
                        decision,
                        recent_context=meeting_context.as_text(),
                        triggering_context=evaluated_snapshot.new_context,
                        now=now,
                        background_context=evaluated_snapshot.background_context,
                    )
                    await dispatch_events(events)
                    log_event('meeting.decision', elapsed_ms=round(jev_latency * 1000, 2),
                              search_probability=decision.search_existing_work_probability,
                              task_probability=decision.suggest_new_task_probability, cost_usd=decision.cost,
                              search_confirmed=decision.search_confirmed, task_confirmed=decision.task_confirmed,
                              sequence=jev_sequence)

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
                    latency = time.monotonic() - started_at
                    job_id = task_jobs.pop(task)
                    try:
                        result = task.result()
                    except Exception as error:
                        log_event('meeting.action.failed', level=logging.WARNING, job_id=job_id,
                                  elapsed_ms=round(latency * 1000, 2), **error_fields(error))
                        jobs.pop(job_id, None)
                        continue
                    jobs.pop(job_id, None)
                    log_event('meeting.action', job_id=job_id, action=result.event_type,
                              refinement=event.is_refinement, elapsed_ms=round(latency * 1000, 2), cost_usd=result.cost)
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
                            log_event('meeting.search.skipped', job_id=job_id, confidence=event.score,
                                      reason=event_service.last_search_terms_suppression_reason or 'duplicate_search_topic')
                        elif weighted_terms:
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
                            log_event('meeting.task.skipped', level=logging.WARNING, job_id=job_id,
                                      reason='incomplete_suggestion')
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
                    job_id = task_jobs[task]
                    finish_source(task, 'google_drive')
                    total_retrieval_seconds = time.perf_counter() - started_at
                    try:
                        retrieval = task.result()
                    except Exception as error:
                        log_event('meeting.search.failed', level=logging.WARNING, job_id=job_id, provider='google_drive',
                                  elapsed_ms=round(total_retrieval_seconds * 1000, 2),
                                  **{**error_fields(error), 'support_code': 'MI-DRV-001'})
                        await safe_send(
                            {
                                "type": "RELATED_FILES",
                                "source": "google_drive",
                                "query": query,
                                "files": [],
                                "error": product_error('MI-DRV-001'),
                                "code": 'google_drive_api_error',
                                "support_code": 'MI-DRV-001',
                            }
                        )
                        continue

                    log_event('meeting.search.results', job_id=job_id, provider='google_drive',
                              elapsed_ms=round(total_retrieval_seconds * 1000, 2),
                              search_ms=retrieval['search_latency_ms'], ranking_ms=retrieval['ranking_latency_ms'],
                              file_count=len(retrieval['files']))
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
                    job_id = task_jobs[task]
                    finish_source(task, 'atlassian')
                    try:
                        retrieval = task.result()
                    except Exception as error:
                        log_event('meeting.search.failed', level=logging.WARNING, job_id=job_id, provider='atlassian',
                                  elapsed_ms=round((time.perf_counter() - started_at) * 1000, 2),
                                  **{**error_fields(error), 'support_code': 'MI-ATL-001'})
                        continue
                    log_event('meeting.search.results', job_id=job_id, provider='atlassian',
                              elapsed_ms=round((time.perf_counter() - started_at) * 1000, 2),
                              file_count=sum(len(files) for files in retrieval['sources'].values()),
                              source_count=len(retrieval['sources']), error_count=len(retrieval.get('errors', {})))
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
                        log_event('meeting.search.partial_failure', level=logging.WARNING,
                                  job_id=job_id, provider='atlassian', source=source_id, support_code='MI-ATL-001')
                atlassian_tasks = still_running_atlassian

            if (
                jev is not None
                and jev_task is None
                and jev_evaluation_requested
                and now >= jev_retry_at
            ):
                jev_snapshot = meeting_context.prepare_jev_context()
                if jev_snapshot is not None:
                    if jev_snapshot.new_context != jev_retry_context:
                        jev_retry_context = jev_snapshot.new_context
                        jev_attempt = 0
                    jev_attempt += 1
                    jev_sequence = ack
                    jev_evaluation_requested = False
                    jev_started_at = now
                    jev_task = asyncio.create_task(
                        observe_operation('jev', evaluate_new_context(meeting_context, jev, llm,
                            known_tasks=tuple(item['title'] for item in timeline.snapshot()['items']
                                if item['kind'] == 'task_suggestion')[:12],
                            known_search_terms=tuple(event_service._search_session.searched_terms)[:6]
                                if event_service._active_search_session(now) is not None else ()),
                            model=settings.jev_model, sequence=ack, text_chars=len(jev_snapshot.new_context))
                    )
            # Changed work is captured before the next receive can suspend.
            # Heartbeats/outputs also checkpoint, so no per-loop extra write is needed.
    except StreamingTranscriptionError:
        close_reason = 'streaming_interrupted'
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason='MI-STT-001')
    except MomError as exc:
        close_reason = 'minutes_interrupted'
        log_event('meeting.interrupted', level=logging.WARNING, reason=close_reason, support_code=exc.code)
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason=exc.code)
    except MeetingBusy:
        close_reason = 'lease_conflict'
        log_event('meeting.interrupted', level=logging.WARNING, reason=close_reason, close_code=4410, support_code='MI-CON-004')
        await websocket.close(code=4410, reason='MI-CON-004')
    except HTTPException as exc:
        close_reason = 'authorization_failed' if exc.status_code == 401 else 'request_rejected'
        log_event('meeting.interrupted', level=logging.WARNING, reason=close_reason, **error_fields(exc))
        await websocket.close(code=4401 if exc.status_code == 401 else 1013, reason='MI-AUTH-105' if exc.status_code == 401 else 'MI-CON-004')
    except ValueError:
        close_reason = 'invalid_control'
        log_event('meeting.interrupted', level=logging.WARNING, reason=close_reason, close_code=4401, support_code='MI-AUTH-105')
        await websocket.close(code=4401, reason='MI-AUTH-105')
    except WebSocketDisconnect:
        if llm_tasks or drive_tasks or atlassian_tasks:
            log_event('meeting.work.pending', pending_llm=len(llm_tasks), pending_drive=len(drive_tasks),
                      pending_atlassian=len(atlassian_tasks))
    except Exception as exc:
        close_reason = 'processing_failed'
        log_event('meeting.interrupted', level=logging.WARNING, reason=close_reason,
                  **{**error_fields(exc), 'support_code': 'MI-CON-004'})
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason='MI-CON-004')
    finally:
        if receive_task:
            receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receive_task
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
        if streaming_update_task:
            streaming_update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception): await streaming_update_task
        if streaming_finish_task:
            streaming_finish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception): await streaming_finish_task
        if streaming_session:
            with contextlib.suppress(Exception): await streaming_session.abort()
            log_event('meeting.streaming.closed', provider='amazon-transcribe', stage='stt',
                audio_seconds=round(streaming_session.audio_seconds,3), ack=ack,
                outcome='completed' if streaming_ended else 'interrupted')
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
        if mom_attachment:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(mom_repo.detach, mom_attachment,
                    finish=finish_minutes and ack == received_sequence and streaming_ended,
                    incomplete=ack != received_sequence or not streaming_ended, capture=session.session_id)
        log_event('meeting.disconnected', reason=close_reason, ack=ack, pending_jobs=len(jobs),
                  duration_ms=round((time.perf_counter() - connection_started) * 1000, 2),
                  checkpoint_count=checkpoint_count, checkpoint_total_ms=round(checkpoint_total_ms, 2),
                  checkpoint_max_ms=round(checkpoint_max_ms, 2))
