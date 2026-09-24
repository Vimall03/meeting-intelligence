from fastapi import FastAPI, WebSocket
from app.api.routes.health import router as health_router
from app.api.routes.transcription import router as transcription_router
from app.api.routes.meeting_stream import router as meeting_stream_router

app = FastAPI()

app.include_router(health_router)
app.include_router(transcription_router)
app.include_router(meeting_stream_router)