from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.core.user_messages import product_error

router = APIRouter()

@router.get('/')
def health():
    return {"message": "healthy"}


@router.get('/health/live')
def live():
    return {'status': 'ok'}


@router.get('/health/ready')
def ready(request: Request):
    # Readiness checks configuration without charging for AWS/provider probes.
    required = (settings.aws_region, settings.auth_user_pool_id, settings.auth_client_id,
        settings.auth_table_name, settings.integration_key_id, settings.extension_origin,
        settings.openrouter_api_key)
    if getattr(getattr(request.app.state, 'meetings', None), 'draining', False) or not all(value.strip() for value in required):
        return JSONResponse(status_code=503, content={'detail': {
            'code': 'service_not_ready', 'support_code': 'MI-CON-009',
            'message': product_error('MI-CON-009')}})
    return {'status': 'ready'}
