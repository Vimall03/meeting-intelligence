"""Request/socket-scoped adapters; identity is never selected by client input."""
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Request

from app.core.auth import Principal, current_user, ensure_active, failure, opaque_key
from app.core.config import settings
from app.services.user_store import CredentialStore, StoreConflict
from app.services.google_drive_service import GoogleDriveService, GoogleDriveSettings
from app.services.atlassian_service import AtlassianService, AtlassianSettings
from app.core.observability import identify_request


class ProviderSession:
    def __init__(self, db, principal, provider, service):
        self.db, self.principal, self.provider, self.service = db, principal, provider, service
        self.callback_verified = False

    def __getattr__(self, name):
        return getattr(self.service, name)

    async def authorization_url(self):
        await ensure_active(self.principal, self.db)
        await self.db.limit(self.principal.sub, 'connect-' + self.provider, 5, 600)
        result = self.service.authorization_url()
        store = self.service.token_store
        row = await self.db.get(store.key)
        version = int(row['version']) if row else None
        next_version = (version or 0) + 1
        await self.db.put(store.key, {**(row or {}), 'version': next_version}, version=version, absent=row is None,
                          family=self.principal.family)
        state = secrets.token_urlsafe(32)
        await self.db.put(opaque_key('OAUTH#', state), {'sub': self.principal.sub, 'family': self.principal.family,
            'provider': self.provider, 'connectionVersion': next_version,
            'expiresAt': int(time.time()) + 600, 'version': 1}, absent=True, family=self.principal.family)
        parts = urlsplit(result['authorization_url'])
        query = dict(parse_qsl(parts.query))
        query['state'] = state
        result['authorization_url'] = urlunsplit(parts._replace(query=urlencode(query)))
        return result

    async def complete_authorization(self, code, state):
        if not self.callback_verified:
            raise failure()
        await ensure_active(self.principal, self.db)
        return await self.service.complete_authorization(code, state, state_verified=True)


@asynccontextmanager
async def provider_session(db, principal, provider):
    store = CredentialStore(db, principal.sub, provider, principal.family)
    if provider == 'google_drive':
        service = GoogleDriveService(GoogleDriveSettings(client_id=settings.google_drive_client_id,
            client_secret=settings.google_drive_client_secret, redirect_uri=settings.google_drive_redirect_uri,
            oauth_state_secret=settings.google_drive_oauth_state_secret, token_path='', scopes=settings.google_drive_scopes,
            timeout_seconds=settings.google_drive_timeout_seconds), token_store=store)
    else:
        service = AtlassianService(AtlassianSettings(client_id=settings.atlassian_client_id,
            client_secret=settings.atlassian_client_secret, redirect_uri=settings.atlassian_redirect_uri,
            oauth_state_secret=settings.atlassian_oauth_state_secret, token_path='', site_url='',
            timeout_seconds=settings.atlassian_timeout_seconds), token_store=store)
    try:
        yield ProviderSession(db, principal, provider, service)
    finally:
        await service.close()


async def google_service(request: Request, principal=Depends(current_user)):
    async with provider_session(request.app.state.users, principal, 'google_drive') as service:
        yield service


async def atlassian_service(request: Request, principal=Depends(current_user)):
    async with provider_session(request.app.state.users, principal, 'atlassian') as service:
        yield service


async def callback_session(request, state, provider):
    db = request.app.state.users
    if not state or len(state) > 128:
        raise failure()
    try:
        row = await db.take(opaque_key('OAUTH#', state))
    except StoreConflict:
        raise failure() from None
    if row['provider'] != provider:
        raise failure()
    principal = Principal(row['sub'], row['family'], int(row['expiresAt']))
    await ensure_active(principal, db)
    identify_request(request, principal.sub)
    async with provider_session(db, principal, provider) as service:
        service.callback_verified = True
        service.token_store.callback_version = int(row['connectionVersion'])
        yield service


async def google_callback(request: Request, state: str = ''):
    async for service in callback_session(request, state, 'google_drive'):
        yield service


async def atlassian_callback(request: Request, state: str = ''):
    async for service in callback_session(request, state, 'atlassian'):
        yield service
