"""Cognito identity verification shared by HTTP requests and meeting sockets."""
import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.user_messages import product_error

bearer = HTTPBearer(auto_error=False)


def failure(code='MI-AUTH-105', status=401):
    return HTTPException(status_code=status, detail={'code': code, 'support_code': code, 'message': product_error(code)})


@dataclass(frozen=True)
class Principal:
    sub: str
    family: str
    expires: int


def issuer():
    if not settings.auth_user_pool_id or not settings.auth_client_id:
        raise failure('MI-AUTH-104', 503)
    return f'https://cognito-idp.{settings.aws_region}.amazonaws.com/{settings.auth_user_pool_id}'


@lru_cache(maxsize=4)
def jwks(url):
    return jwt.PyJWKClient(url + '/.well-known/jwks.json', cache_keys=True, lifespan=300, timeout=4)


async def verify(token, db):
    try:
        url = issuer()
        signing_key = await asyncio.to_thread(jwks(url).get_signing_key_from_jwt, token)
        claims = jwt.decode(token, signing_key.key, algorithms=['RS256'], issuer=url,
            options={'verify_aud': False, 'require': ['exp', 'iat', 'sub', 'token_use', 'client_id', 'origin_jti']})
        if claims['token_use'] != 'access' or claims['client_id'] != settings.auth_client_id or 'aws.cognito.signin.user.admin' not in claims.get('scope', '').split() or not isinstance(claims['origin_jti'], str) or len(claims['origin_jti']) > 128:
            raise failure()
        principal = Principal(claims['sub'], claims['origin_jti'], int(claims['exp']))
        await ensure_active(principal, db)
        return principal
    except HTTPException:
        raise
    except jwt.PyJWTError:
        raise failure() from None
    except Exception:
        raise failure('MI-AUTH-104', 503) from None


async def ensure_active(principal, db):
    if principal.expires <= time.time() or await db.get('REVOKED#' + principal.family):
        raise failure()


async def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != 'bearer' or len(credentials.credentials) > 8192:
        raise failure()
    principal = await verify(credentials.credentials, request.app.state.users)
    await request.app.state.users.limit(principal.sub, 'http', 120, 60)
    return principal


def opaque_key(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()


async def issue_ticket(principal, db):
    await ensure_active(principal, db)
    await db.limit(principal.sub, 'meeting-ticket', 30, 60)
    ticket = secrets.token_urlsafe(32)
    await db.put(opaque_key('WS#', ticket), {'sub': principal.sub, 'family': principal.family,
        'tokenExpires': principal.expires, 'expiresAt': int(time.time()) + 30, 'version': 1}, absent=True)
    return ticket


async def consume_ticket(ticket, db):
    if not isinstance(ticket, str) or len(ticket) > 128:
        raise failure()
    try:
        row = await db.take(opaque_key('WS#', ticket))
        principal = Principal(row['sub'], row['family'], int(row['tokenExpires']))
        await ensure_active(principal, db)
        return principal
    except HTTPException:
        raise
    except Exception:
        raise failure() from None
