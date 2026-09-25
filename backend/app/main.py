from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.google_drive import router as google_drive_router
from app.api.routes.health import router as health_router
from app.api.routes.transcription import router as transcription_router
from app.api.routes.meeting_stream import router as meeting_stream_router
from app.core.config import settings
from app.services.google_drive_service import GoogleDriveService, GoogleDriveSettings


google_drive_service = GoogleDriveService(
    GoogleDriveSettings(
        client_id=settings.google_drive_client_id,
        client_secret=settings.google_drive_client_secret,
        redirect_uri=settings.google_drive_redirect_uri,
        oauth_state_secret=settings.google_drive_oauth_state_secret,
        token_path=settings.google_drive_token_path,
        scopes=settings.google_drive_scopes,
        timeout_seconds=settings.google_drive_timeout_seconds,
    )
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        await google_drive_service.close()


app = FastAPI(lifespan=lifespan)

app.include_router(health_router)
app.include_router(transcription_router)
app.include_router(meeting_stream_router)
app.include_router(google_drive_router)

app.state.google_drive = google_drive_service
