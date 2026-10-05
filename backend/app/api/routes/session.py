import time
from fastapi import APIRouter, Depends, Request
from app.core.auth import current_user, issue_ticket

router = APIRouter(prefix='/api')


@router.post('/meeting/connection-ticket')
async def connection_ticket(request: Request, principal=Depends(current_user)):
    ticket = await issue_ticket(principal, request.app.state.users)
    return {'ticket': ticket, 'expiresIn': 30}


@router.post('/session/logout')
async def logout(request: Request, principal=Depends(current_user)):
    await request.app.state.users.put('REVOKED#' + principal.family,
        {'expiresAt': int(time.time()) + 31 * 86400, 'sub': principal.sub})
    return {'message': 'You’re signed out.'}
