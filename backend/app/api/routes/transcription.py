import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File
from fastapi.concurrency import run_in_threadpool

from backend.app.services.transcription_services import transcribe_audio
router = APIRouter()

@router.post('/transcribe')
async def transcribe(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
            temporary_path = Path(temporary_file.name)
            await run_in_threadpool(shutil.copyfileobj, file.file, temporary_file)

        return await run_in_threadpool(transcribe_audio, str(temporary_path))
    finally:
        await file.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)