"""Content-free JSON diagnostics with task-local, server-verified identity.

ContextVars also follow FastAPI's thread pool, so audio processing keeps the
same correlation fields without sharing mutable identity between meetings.
"""
import asyncio
import json
import logging
import math
import secrets
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

logger = logging.getLogger('meeting_intelligence')
_context = ContextVar('meeting_log_context', default=None)
_CONTEXT_FIELDS = {'request_id', 'user_id', 'meeting_id', 'connection_id', 'operation_id', 'job_id', 'sequence'}
# New diagnostics must opt in to scalar metadata. Bodies, URLs, transcripts,
# query terms, credentials, email addresses and exception strings are excluded.
_FIELDS = _CONTEXT_FIELDS | {
    'stage', 'provider', 'model', 'method', 'route', 'status_code', 'support_code',
    'error_type', 'provider_error_code', 'duration_ms', 'queue_wait_ms', 'queue_depth',
    'elapsed_ms', 'search_ms', 'ranking_ms', 'attempt', 'outcome', 'reason', 'action',
    'confidence', 'refinement', 'search_probability', 'task_probability', 'cost_usd',
    'search_confirmed', 'task_confirmed', 'waiting_for_context',
    'segment_count', 'text_chars', 'term_count', 'file_count', 'source', 'source_count',
    'error_count', 'audio_bytes', 'audio_seconds', 'audio_start_seconds', 'audio_end_seconds',
    'audio_rms', 'audio_peak', 'ack', 'resumed', 'pending_jobs', 'pending_llm',
    'pending_drive', 'pending_atlassian', 'close_code', 'checkpoint_count',
    'checkpoint_total_ms', 'checkpoint_max_ms', 'active_sockets',
}


def new_id():
    return secrets.token_hex(16)


def set_log_context(**fields):
    return _context.set({**(_context.get() or {}), **{k: v for k, v in fields.items() if k in _CONTEXT_FIELDS}})


@contextmanager
def log_context(**fields):
    token = set_log_context(**fields)
    try:
        yield
    finally:
        _context.reset(token)


def identify_request(request, user_id):
    # State lets the outer HTTP middleware see a verified identity even if an
    # inner ASGI component schedules the handler in a separate task.
    request.state.log_user_id = user_id
    set_log_context(user_id=user_id)


def error_fields(error):
    fields = {'error_type': type(error).__name__}
    response = getattr(error, 'response', None)
    status = getattr(response, 'status_code', None)
    if isinstance(status, int):
        fields['status_code'] = status
    if isinstance(response, dict):
        code = response.get('Error', {}).get('Code')
        # AWS error codes are fixed identifiers, never descriptions.
        if isinstance(code, str) and len(code) <= 64 and code.replace('_', '').replace('.', '').isalnum():
            fields['provider_error_code'] = code
    detail = getattr(error, 'detail', None)
    if isinstance(detail, dict) and isinstance(detail.get('support_code'), str):
        fields['support_code'] = detail['support_code']
    return fields


def log_event(event, *, level=logging.INFO, **fields):
    metadata = {**(_context.get() or {}), **fields}
    safe = {k: v for k, v in metadata.items() if k in _FIELDS
            and (v is None or isinstance(v, (str, int, float, bool)))
            and not (isinstance(v, float) and not math.isfinite(v))}
    logger.log(level, event, extra={'metadata': safe})


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'service': 'meeting-intelligence', 'schema_version': 1,
            'level': record.levelname, 'event': record.getMessage(),
            **getattr(record, 'metadata', {}),
        }, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def configure_logging():
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # HTTPX's INFO messages include full URLs, including authorization codes.
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def observe(event, **fields):
    """Time actual work; callers may add safe scalar result metadata."""
    started = time.perf_counter()
    metrics = dict(fields)
    log_event(event + '.started', **metrics)
    try:
        yield metrics
    except asyncio.CancelledError:
        log_event(event + '.cancelled', duration_ms=round((time.perf_counter() - started) * 1000, 2), **metrics)
        raise
    except Exception as error:
        log_event(event + '.failed', level=logging.WARNING,
                  **{**metrics, **error_fields(error), 'duration_ms': round((time.perf_counter() - started) * 1000, 2)})
        raise
    else:
        log_event(event + '.completed', duration_ms=round((time.perf_counter() - started) * 1000, 2), **metrics)


async def observe_operation(stage, operation, **fields):
    with log_context(operation_id=new_id(), **{k: v for k, v in fields.items() if k in _CONTEXT_FIELDS}):
        with observe('meeting.operation', stage=stage, **fields):
            return await operation


async def provider_request(client, method, url, *, provider, stage, model=None, attempt=None, **kwargs):
    # Neither URL nor kwargs (which contain credentials and content) is logged.
    with observe('provider.request', provider=provider, stage=stage, model=model, attempt=attempt, method=method) as metrics:
        response = await client.request(method, url, **kwargs)
        metrics.update(status_code=response.status_code, outcome='error' if response.is_error else 'success')
        return response


class RequestLogMiddleware:
    """Log templated routes and IDs, excluding raw paths, query and headers."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = new_id()  # Ignore caller-supplied IDs and spoofed identity.
        state = scope.setdefault('state', {})
        state['log_request_id'] = request_id
        status = 503
        started = time.perf_counter()
        error = None

        async def tracked_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                headers = [(k, v) for k, v in message.get('headers', [])
                           if k.lower() not in (b'x-request-id', b'cache-control')]
                message = {**message, 'headers': [*headers,
                    (b'x-request-id', request_id.encode()), (b'cache-control', b'no-store')]}
            await send(message)

        with log_context(request_id=request_id, user_id=None, meeting_id=None, connection_id=None,
                         operation_id=None, job_id=None, sequence=None):
            try:
                await self.app(scope, receive, tracked_send)
            except Exception as exc:
                error = exc
                raise
            finally:
                route = getattr(scope.get('route'), 'path', 'unmatched')
                # Health probes need no per-request INFO entries.
                if route not in ('/', '/health/live', '/health/ready') or status >= 400:
                    log_event('http.request', level=logging.WARNING if status >= 400 else logging.INFO,
                              **{'request_id': request_id, 'user_id': state.get('log_user_id'),
                                 'method': scope['method'], 'route': route, 'status_code': status,
                                 'duration_ms': round((time.perf_counter() - started) * 1000, 2),
                                 **(error_fields(error) if error else {})})
