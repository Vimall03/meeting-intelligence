import asyncio
import json
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from app.core import auth
from app.core.observability import (
    JsonFormatter, RequestLogMiddleware, error_fields, identify_request,
    log_context, log_event, observe_operation, provider_request,
)
from app.main import safe_failure
from app.services.openrouter_stt_service import OpenRouterTranscriptionService
from app.services.user_store import CredentialStore
from test_auth_integration import MemoryStore


def entries(captured):
    return [json.loads(JsonFormatter().format(record)) for record in captured.records]


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_context_follows_audio_thread_pool_and_never_crosses_users(self):
        ready = asyncio.Event()
        async def meeting(owner):
            with log_context(user_id=owner, meeting_id='shared-client-meeting-id', request_id=owner + '-request'):
                await ready.wait()
                await observe_operation('stt', run_in_threadpool(log_event, 'thread.audio'), sequence=1)
        with self.assertLogs('meeting_intelligence', level='INFO') as captured:
            tasks = [asyncio.create_task(meeting(owner)) for owner in ('A', 'B')]
            ready.set()
            await asyncio.gather(*tasks)
            log_event('outside')
        rows = entries(captured)
        for owner in ('A', 'B'):
            own = [r for r in rows if r.get('user_id') == owner]
            self.assertEqual({r['request_id'] for r in own}, {owner + '-request'})
            self.assertEqual(len({r['operation_id'] for r in own}), 1)
            self.assertTrue(any(r['event'] == 'thread.audio' and r['sequence'] == 1 for r in own))
        self.assertNotIn('user_id', rows[-1])

    async def test_provider_failure_and_unknown_fields_never_log_body_url_or_credentials(self):
        secret = 'private-query-token-and-audio'
        def fail(request):
            raise httpx.HTTPStatusError(secret, request=request,
                response=httpx.Response(503, json={'error': secret}, request=request))
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with self.assertLogs('meeting_intelligence', level='INFO') as captured, log_context(user_id='A', request_id='request'):
                log_event('metadata', token=secret, query=secret, transcript=secret,
                          cost_usd=float('nan'), data={'credential': secret})
                with self.assertRaises(httpx.HTTPStatusError):
                    await provider_request(client, 'POST', 'https://provider.test/?code=' + secret,
                        provider='openrouter', stage='stt', json={'audio': secret})
        encoded = json.dumps(entries(captured))
        self.assertNotIn(secret, encoded)
        self.assertNotIn('https://', encoded)
        failed = entries(captured)[-1]
        self.assertEqual(failed['status_code'], 503)
        self.assertEqual(failed['error_type'], 'HTTPStatusError')
        self.assertEqual(failed['user_id'], 'A')

    async def test_operation_timing_and_cancellation_are_logged_without_changing_result(self):
        async def work():
            await asyncio.sleep(.01)
            return {'result': 'private'}
        with self.assertLogs('meeting_intelligence', level='INFO') as captured:
            result = await observe_operation('llm', work(), job_id='stable-job')
            # Waiting before handling the result cannot inflate the operation timer.
            await asyncio.sleep(.05)
            async def never():
                await asyncio.Event().wait()
            task = asyncio.create_task(observe_operation('drive', never()))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        rows = entries(captured)
        complete = next(r for r in rows if r['event'] == 'meeting.operation.completed')
        self.assertEqual(result, {'result': 'private'})
        self.assertEqual(complete['job_id'], 'stable-job')
        self.assertGreaterEqual(complete['duration_ms'], 5)
        self.assertLess(complete['duration_ms'], 50)
        self.assertEqual(rows[-1]['event'], 'meeting.operation.cancelled')
        self.assertNotIn('private', json.dumps(rows))

    async def test_http_users_unknown_routes_and_errors_have_safe_server_request_ids(self):
        app = FastAPI()
        app.state.users = MemoryStore()
        app.add_middleware(RequestLogMiddleware)
        app.add_exception_handler(Exception, safe_failure)

        @app.get('/files/{file_id}')
        async def file(request: Request, file_id: str, principal=Depends(auth.current_user)):
            await asyncio.sleep(.01)
            log_event('file.read')
            return {'owner': principal.sub}

        @app.get('/fail')
        async def failing(principal=Depends(auth.current_user)):
            raise RuntimeError('provider-body-with-token')

        async def verified(token, db):
            if token not in ('A', 'B'):
                raise auth.failure()
            return auth.Principal(token, 'family-' + token, int(time.time()) + 900)

        with patch.object(auth, 'verify', verified), self.assertLogs('meeting_intelligence', level='INFO') as captured:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url='http://test') as client:
                responses = await asyncio.gather(*(client.get('/files/private-document-' + owner + '?code=private-code',
                    headers={'Authorization': 'Bearer ' + owner, 'X-Request-ID': 'forged', 'X-User-ID': 'spoof'}) for owner in ('A', 'B')))
                rejected = await client.get('/files/secret-path', headers={'Authorization': 'Bearer spoof'})
                unknown = await client.get('/private-unknown-path?token=private-code')
                failed = await client.get('/fail', headers={'Authorization': 'Bearer A'})
        rows = entries(captured)
        requests = [r for r in rows if r['event'] == 'http.request']
        self.assertEqual(len({r['request_id'] for r in requests}), 5)
        for owner, response in zip(('A', 'B'), responses):
            request_id = response.headers['X-Request-ID']
            self.assertNotEqual(request_id, 'forged')
            own = [r for r in rows if r.get('request_id') == request_id]
            self.assertEqual({r.get('user_id') for r in own}, {owner})
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.json()['owner'], owner)
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.json()['detail']['support_code'], 'MI-AUTH-104')
        self.assertTrue(failed.headers['X-Request-ID'])
        self.assertIsNone(next(r for r in requests if r['status_code'] == 401)['user_id'])
        encoded = json.dumps(rows)
        for secret in ('private-document', 'private-code', 'private-unknown-path', 'provider-body-with-token', 'forged', 'spoof'):
            self.assertNotIn(secret, encoded)

    async def test_callback_identity_is_from_verified_state_and_query_is_not_logged(self):
        from app.services.integrations import callback_session
        app = FastAPI()
        app.state.users = MemoryStore()
        app.add_middleware(RequestLogMiddleware)
        store = CredentialStore(app.state.users, 'A', 'google_drive')
        await store.write({'refresh_token': 'private-refresh'})
        await app.state.users.put(auth.opaque_key('OAUTH#', 'private-state'), {
            'sub': 'A', 'family': 'family-A', 'provider': 'google_drive', 'version': 1,
            'connectionVersion': 1, 'expiresAt': int(time.time()) + 600,
        })

        @app.get('/oauth/callback')
        async def callback(request: Request, state: str):
            async for _service in callback_session(request, state, 'google_drive'):
                log_event('integration.connected', provider='google_drive')
                return {'connected': True}

        with self.assertLogs('meeting_intelligence', level='INFO') as captured:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                response = await client.get('/oauth/callback?state=private-state&code=private-code')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(r['user_id'] == 'A' for r in entries(captured)))
        self.assertNotIn('private-', json.dumps(entries(captured)))

    async def test_sync_transcription_diagnostics_follow_verified_identity_without_transcript(self):
        import numpy as np
        def provider(request):
            return httpx.Response(200, json={'text': 'confidential meeting speech'})
        service = OpenRouterTranscriptionService('private-api-key', transport=httpx.MockTransport(provider))
        with patch('app.services.openrouter_stt_service.load_audio', return_value=np.zeros(16000, dtype=np.float32)), \
             self.assertLogs('meeting_intelligence', level='INFO') as captured, \
             log_context(user_id='A', meeting_id='meeting', request_id='request', sequence=2):
            result = await run_in_threadpool(service.transcribe, 'private-audio-path')
        self.assertEqual(result['segments'][0]['text'], 'confidential meeting speech')
        rows = entries(captured)
        self.assertTrue(all(r['user_id'] == 'A' and r['sequence'] == 2 for r in rows))
        self.assertTrue(any(r['event'] == 'audio.decode.completed' for r in rows))
        self.assertTrue(any(r['event'] == 'provider.request.completed' and r['status_code'] == 200 for r in rows))
        for secret in ('confidential', 'private-api-key', 'private-audio-path'):
            self.assertNotIn(secret, json.dumps(rows))

    def test_aws_error_uses_code_not_description(self):
        error = RuntimeError('private token')
        error.response = {'Error': {'Code': 'AccessDeniedException', 'Message': 'private identity and token'}}
        self.assertEqual(error_fields(error), {'error_type': 'RuntimeError', 'provider_error_code': 'AccessDeniedException'})
