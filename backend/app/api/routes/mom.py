"""Authenticated MOM preferences and finalization contract for the later extension.

There is no arbitrary recipient or transcript-upload endpoint. Capture persists
transcripts server-side, and recipients are resolved from verified Cognito users.
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import current_user
from app.core.config import settings
from app.core.user_messages import product_error
from app.mom.repository import Repository, MomError, Conflict

router = APIRouter(prefix='/api/mom')


def error(exc):
    code = exc.code if isinstance(exc, MomError) else 'MI-MOM-001'
    status = 404 if code == 'MI-MOM-005' else 409 if code == 'MI-MOM-004' else 422 if code in ('MI-MOM-002', 'MI-MOM-003') else 403 if code == 'MI-MOM-008' else 503
    return HTTPException(status, detail={'code': 'mom_' + code[-3:], 'support_code': code, 'message': product_error(code)})


def available():
    return settings.mom_enabled and all((settings.mom_table_name, settings.mom_bucket_name,
        settings.mom_key_arn, settings.mom_queue_url, settings.auth_table_name))


def repository_for(app):
    if not available(): raise MomError()
    cached = getattr(app.state, 'mom', None)
    if cached is None:
        cached = Repository(table=settings.mom_table_name, bucket=settings.mom_bucket_name,
            key_arn=settings.mom_key_arn, queue=settings.mom_queue_url, region=settings.aws_region,
            recovery_table=settings.auth_table_name, grace_seconds=settings.mom_grace_seconds,
            retention_seconds=settings.mom_retention_seconds, max_session_seconds=settings.mom_max_session_seconds)
        app.state.mom = cached
    return cached


async def verified_username(request, principal):
    token = request.headers.get('authorization', '').partition(' ')[2]
    def read():
        user = repository_for(request.app).client('cognito-idp').get_user(AccessToken=token)
        attrs = {item['Name']: item['Value'] for item in user['UserAttributes']}
        if attrs.get('sub') != principal.sub or attrs.get('email_verified') != 'true': raise MomError('MI-MOM-008')
        return user['Username']
    return await asyncio.to_thread(read)


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['live_only', 'mom_only', 'both']
    timezone: str = Field(default='UTC', min_length=1, max_length=64)


@router.get('/settings')
async def get_settings(request: Request, principal=Depends(current_user)):
    try:
        prefs = await asyncio.to_thread(repository_for(request.app).preferences, principal.sub) if available() else {'mode': 'live_only', 'timezone': 'UTC'}
        return {'mode': prefs['mode'], 'timezone': prefs['timezone'], 'minutes_available': bool(available()),
            'grace_seconds': settings.mom_grace_seconds, 'recovery_retention_seconds': settings.mom_retention_seconds}
    except Exception as exc: raise error(exc) from None


@router.post('/settings')
async def update_settings(body: Preferences, request: Request, principal=Depends(current_user)):
    try:
        repo = repository_for(request.app)
        username = await verified_username(request, principal) if body.mode != 'live_only' else None
        await asyncio.to_thread(repo.set_preferences, principal.sub, body.mode, body.timezone, username)
        return await get_settings(request, principal)
    except Exception as exc: raise error(exc) from None


def public_status(row):
    return {'session_id': row['sessionId'], 'state': row['state'], 'mode': row.get('mode'),
        'finalize_after': row.get('finalizeAfter'), 'sent_at': row.get('sentAt'),
        **({'support_code': row['supportCode'], 'message': product_error(row['supportCode'])} if row.get('supportCode') else {})}


@router.get('/sessions/{session_id}')
async def session_status(session_id: str, request: Request, principal=Depends(current_user)):
    try: return public_status(await asyncio.to_thread(repository_for(request.app).status, principal.sub, session_id))
    except Exception as exc: raise error(exc) from None


@router.post('/sessions/{session_id}/finish')
async def finish_session(session_id: str, request: Request, principal=Depends(current_user)):
    try: return public_status(await asyncio.to_thread(repository_for(request.app).finish, principal.sub, session_id))
    except Exception as exc: raise error(exc) from None


@router.post('/sessions/{session_id}/retry')
async def retry_session(session_id: str, request: Request, principal=Depends(current_user)):
    try:
        repo = repository_for(request.app)
        row = await asyncio.to_thread(repo.status, principal.sub, session_id)
        if row['state'] == 'delivery_unknown': raise MomError('MI-MOM-006')
        if row['state'] != 'failed' or not row.get('retryable') or row.get('attempts', 0) >= 3 or row['purgeAt'] <= repo.clock(): raise Conflict()
        row = await asyncio.to_thread(repo.replace, row, state='ready', dueAt=int(repo.clock()), supportCode=None)
        # Ready is durable even if enqueue fails; the sweeper retries it.
        try: await asyncio.to_thread(repo.enqueue, row)
        except Exception: pass
        return public_status(row)
    except Exception as exc: raise error(exc) from None
