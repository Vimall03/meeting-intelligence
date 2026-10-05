import logging
from contextlib import asynccontextmanager
from app.services.meeting_sessions import MeetingConnections
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from html import escape
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError

from app.api.routes.google_drive import router as google_drive_router
from app.api.routes.atlassian import router as atlassian_router
from app.api.routes.health import router as health_router
from app.api.routes.meeting_stream import router as meeting_stream_router
from app.api.routes.session import router as session_router
from app.core.config import settings
from app.core.user_messages import product_error
from app.services.user_store import UserStore, StoreConflict

@asynccontextmanager
async def lifespan(app):
    app.state.meetings = MeetingConnections()
    yield
    await app.state.meetings.drain()


app = FastAPI(lifespan=lifespan)
if settings.extension_origin:
    app.add_middleware(CORSMiddleware, allow_origins=[settings.extension_origin],
        allow_methods=['GET', 'POST', 'OPTIONS'], allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'])
app.state.users = UserStore()


@app.exception_handler(HTTPException)
async def http_failure(request: Request, exc):
    if request.url.path.endswith('/oauth/callback'):
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8"><title>Connection not completed</title></head>'
            '<body style="font:16px system-ui;max-width:32rem;margin:12vh auto;padding:24px"><h1>Connection not completed</h1><p>'
            + escape(product_error('MI-AUTH-105')) + '</p><p>Return to Meeting Intelligence and try connecting again.</p></body></html>', status_code=exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc):
    return JSONResponse(status_code=422, content={'detail': {'code': 'MI-AUTH-108', 'support_code': 'MI-AUTH-108', 'message': product_error('MI-AUTH-108')}})


@app.middleware('http')
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(Exception)
async def safe_failure(request: Request, exc):
    logging.getLogger(__name__).warning('MI-AUTH-104 request failed (%s)', type(exc).__name__)
    return JSONResponse(status_code=503, content={'detail': {'code': 'MI-AUTH-104',
        'support_code': 'MI-AUTH-104', 'message': product_error('MI-AUTH-104')}})


@app.exception_handler(StoreConflict)
async def storage_conflict(request: Request, exc):
    return await safe_failure(request, exc)


for router in (health_router, meeting_stream_router, google_drive_router, atlassian_router, session_router):
    app.include_router(router)
