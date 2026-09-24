from fastapi import FastAPI
from backend.app.api.routes.health import router as health_router
from backend.app.api.routes.transcription import router as transcription_router
app = FastAPI()

app.include_router(health_router)
app.include_router(transcription_router)