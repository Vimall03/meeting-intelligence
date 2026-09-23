from fastapi import APIRouter, UploadFile, File
from fastapi.concurrency import run_in_threadpool

from app.services.transcription_services import transcribe_audio
router = APIRouter()

@router.post('/transcribe')
async def transcribe(file: UploadFile = File(...)):
    result = await run_in_threadpool(transcribe_audio, file.file)
    return result