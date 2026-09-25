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
                            logger.info(
                                "%s: %s",
                                segment.speaker,
                                segment.text,
                            )
                        if accepted_segments:
                            logger.info(
                                "Active meeting context:\n%s",
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
    except WebSocketDisconnect:
        pass
    finally:
        if transcription_task:
            transcription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await transcription_task