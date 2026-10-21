import asyncio, copy, json, secrets, time, unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import httpx, jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from app.core import auth
from app.core.config import settings
from app.services.user_store import UserStore, CredentialStore, StoreConflict
from app.services.integrations import callback_session
from app.api.routes.google_drive import router as drive_router
from app.api.routes.session import router as session_router

class FakeKms:
    def __init__(self): self.keys = {}
    def generate_data_key(self, **args):
        marker, plain = secrets.token_bytes(32), secrets.token_bytes(32)
        self.keys[marker] = (plain, args['EncryptionContext'])
        return {'Plaintext': plain, 'CiphertextBlob': marker}
    def decrypt(self, **args):
        plain, context = self.keys[args['CiphertextBlob']]
        if args['EncryptionContext'] != context: raise ValueError('wrong context')
        return {'Plaintext': plain}

class MemoryStore(UserStore):
    """Conditional storage model with real AES-GCM and a fake KMS service."""
    def __init__(self):
        super().__init__(); self.rows = {}; self.lock = asyncio.Lock(); self.crypto = FakeKms()
    def _key_service(self): return self.crypto
    async def get(self, key): return copy.deepcopy(self.rows.get(key))
    async def put(self, key, value, *, version=None, absent=False, family=None):
        async with self.lock:
            old = self.rows.get(key)
            if (absent and old is not None) or (version is not None and (old is None or old['version'] != version)) or (family and 'REVOKED#' + family in self.rows): raise StoreConflict()
            self.rows[key] = copy.deepcopy({'pk': key, **value})
    async def limit(self, *_args): pass

class AuthIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MemoryStore(); self.a = auth.Principal('A', 'family-A', int(time.time()) + 900)
        self.b = auth.Principal('B', 'family-B', int(time.time()) + 900)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.patches = [patch.object(settings, 'auth_user_pool_id', 'us-east-1_test'), patch.object(settings, 'auth_client_id', 'app-client'), patch.object(settings, 'aws_region', 'us-east-1'), patch.object(auth, 'jwks', lambda _url: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=self.private.public_key())))]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
    def token(self, user='A', **overrides):
        claims = {'sub': user, 'origin_jti': 'family-' + user, 'iss': auth.issuer(), 'client_id': 'app-client', 'token_use': 'access', 'scope': 'aws.cognito.signin.user.admin', 'exp': int(time.time()) + 900, 'iat': int(time.time())}; claims.update(overrides)
        return jwt.encode(claims, self.private, algorithm='RS256')
    async def seed(self, user, provider='google_drive', **extra):
        store = CredentialStore(self.db, user, provider)
        await store.write({'access_token': user + '-private-access', 'refresh_token': user + '-private-refresh', 'expires_at': time.time() + 900, **extra}); return store
    async def test_real_jwt_rejects_wrong_pool_client_type_expiry_and_signature(self):
        self.assertEqual((await auth.verify(self.token(), self.db)).sub, 'A')
        for claims in ({'iss': 'https://wrong.example'}, {'client_id': 'other'}, {'token_use': 'id'}, {'exp': 1}):
            with self.assertRaises(HTTPException): await auth.verify(self.token(**claims), self.db)
        with self.assertRaises(HTTPException): await auth.verify(jwt.encode({'sub': 'A'}, 'fake' * 16, algorithm='HS256'), self.db)
    async def test_revocation_rejects_unexpired_token_for_only_that_session(self):
        await self.db.put('REVOKED#family-A', {'expiresAt': int(time.time()) + 86400})
        with self.assertRaises(HTTPException): await auth.verify(self.token(), self.db)
        self.assertEqual((await auth.verify(self.token('B'), self.db)).sub, 'B')

    async def test_public_key_outage_does_not_invalidate_session(self):
        def unavailable(_token):
            raise jwt.exceptions.PyJWKClientConnectionError('offline')
        with patch.object(auth, 'jwks', lambda _: SimpleNamespace(get_signing_key_from_jwt=unavailable)):
            with self.assertRaises(HTTPException) as result:
                await auth.verify(self.token(), self.db)
        self.assertEqual(result.exception.status_code, 503)

    async def test_ticket_database_outage_returns_retryable_failure(self):
        async def unavailable(_key): raise RuntimeError('offline')
        with patch.object(self.db, 'get', unavailable):
            with self.assertRaises(HTTPException) as result:
                await auth.consume_ticket('opaque-ticket', self.db)
        self.assertEqual(result.exception.status_code, 503)
    async def test_credentials_encrypted_isolated_without_otp_ttl(self):
        a, b = await self.seed('A'), await self.seed('B')
        self.assertEqual((await a.read())['access_token'], 'A-private-access'); self.assertEqual((await b.read())['access_token'], 'B-private-access')
        text = json.dumps(self.db.rows); self.assertNotIn('private-refresh', text); self.assertNotIn('private-access', text)
        self.assertNotIn('expiresAt', await self.db.get(a.key))
        with self.assertRaises(ValueError): await self.db.decrypt('B', 'google_drive', (await self.db.get(a.key))['cipher'])
    async def test_distributed_refresh_exchanges_once_across_instances(self):
        await self.seed('A', expires_at=0); exchanges = []
        async def transport(request):
            exchanges.append(request.content); await asyncio.sleep(.02); return httpx.Response(200, json={'access_token': 'rotated', 'expires_in': 900})
        from app.services.google_drive_service import GoogleDriveService, GoogleDriveSettings
        config = GoogleDriveSettings(client_id='client', client_secret='secret', redirect_uri='https://callback', oauth_state_secret='secret', token_path='', scopes='scope')
        stores = [CredentialStore(self.db, 'A', 'google_drive', self.a.family) for _ in range(6)]
        services = [GoogleDriveService(config, token_store=store, transport=httpx.MockTransport(transport)) for store in stores]
        try:
            self.assertEqual(await asyncio.gather(*(s._access_token() for s in services)), ['rotated'] * 6); self.assertEqual(len(exchanges), 1)
            self.assertEqual((await stores[0].read())['refresh_token'], 'A-private-refresh')
        finally: await asyncio.gather(*(s.close() for s in services))
    async def test_disconnect_fences_late_refresh(self):
        await self.seed('A'); refreshing = CredentialStore(self.db, 'A', 'google_drive')
        async with refreshing.refresh_lock():
            token = await refreshing.read(); await CredentialStore(self.db, 'A', 'google_drive').clear()
            with self.assertRaises(StoreConflict): await refreshing.write({**token, 'access_token': 'late'})
        self.assertIsNone(await refreshing.read())
    async def test_logout_fences_credential_write(self):
        store = await self.seed('A'); bound = CredentialStore(self.db, 'A', 'google_drive', self.a.family); await bound.read()
        await self.db.put('REVOKED#family-A', {'expiresAt': int(time.time()) + 86400})
        with self.assertRaises(StoreConflict): await bound.write({'access_token': 'late'})
        self.assertEqual((await store.read())['access_token'], 'A-private-access')
    async def test_connection_version_fences_old_callback(self):
        await self.seed('A'); store = CredentialStore(self.db, 'A', 'google_drive'); store.callback_version = (await self.db.get(store.key))['version']
        await CredentialStore(self.db, 'A', 'google_drive').clear(); await store.read()
        with self.assertRaises(StoreConflict): await store.write({'access_token': 'old'})
    async def test_tickets_one_use_user_bound_and_revoked(self):
        ticket = await auth.issue_ticket(self.a, self.db); self.assertEqual(await auth.consume_ticket(ticket, self.db), self.a)
        with self.assertRaises(HTTPException): await auth.consume_ticket(ticket, self.db)
        ticket = await auth.issue_ticket(self.b, self.db); await self.db.put('REVOKED#family-B', {'expiresAt': int(time.time()) + 86400})
        with self.assertRaises(HTTPException): await auth.consume_ticket(ticket, self.db)
    async def test_expired_and_mismatched_callback_state(self):
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(users=self.db)))
        await self.db.put(auth.opaque_key('OAUTH#', 'expired'), {'expiresAt': 1, 'version': 1})
        with self.assertRaises(HTTPException): await anext(callback_session(request, 'expired', 'google_drive'))
        await self.db.put(auth.opaque_key('OAUTH#', 'other'), {'expiresAt': int(time.time()) + 600, 'version': 1, 'provider': 'atlassian'})
        with self.assertRaises(HTTPException): await anext(callback_session(request, 'other', 'google_drive'))
    async def test_http_concurrent_users_search_and_logout_isolation(self):
        await self.seed('A'); await self.seed('B')
        app = FastAPI(); app.state.users = self.db; app.include_router(drive_router); app.include_router(session_router)
        def provider(request):
            owner = request.headers['authorization'].split('-private-access')[0].split()[-1]
            return httpx.Response(200, json={'files': [{'id': owner + '-file', 'name': owner + '-private-document', 'mimeType': 'text/plain'}]})
        from app.services.google_drive_service import GoogleDriveService
        original = GoogleDriveService.__init__
        def init(service, config, **kwargs): original(service, config, transport=httpx.MockTransport(provider), **kwargs)
        with patch.object(GoogleDriveService, '__init__', init):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                headers = lambda user: {'Authorization': 'Bearer ' + self.token(user)}
                a, b = await asyncio.gather(*(client.get('/api/integrations/google-drive/files/search?query=plan', headers=headers(user)) for user in ('A', 'B')))
                self.assertIn('A-private-document', a.text); self.assertNotIn('B-private-document', a.text)
                self.assertIn('B-private-document', b.text); self.assertNotIn('A-private-document', b.text)
                self.assertEqual((await client.get('/api/integrations/google-drive/status')).status_code, 401)
                await client.post('/api/session/logout', headers=headers('A'))
                self.assertEqual((await client.get('/api/integrations/google-drive/status', headers=headers('A'))).status_code, 401)
                self.assertEqual((await client.get('/api/integrations/google-drive/status', headers=headers('B'))).status_code, 200)


    async def test_atlassian_refresh_rotates_once_and_persists_new_refresh_token(self):
        from app.services.atlassian_service import AtlassianService, AtlassianSettings
        await self.seed('A', 'atlassian', expires_at=0, cloud_id='site-A', site_url='https://a.atlassian.net', scopes=['read:jira-work'])
        count = []
        async def provider(request):
            count.append(json.loads(request.content)['refresh_token'])
            await asyncio.sleep(.02)
            return httpx.Response(200, json={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 900})
        config = AtlassianSettings(client_id='client', client_secret='secret', redirect_uri='https://callback', oauth_state_secret='secret', token_path='')
        services = [AtlassianService(config, token_store=CredentialStore(self.db, 'A', 'atlassian', self.a.family), transport=httpx.MockTransport(provider)) for _ in range(5)]
        try:
            results = await asyncio.gather(*(s._access_token() for s in services))
            self.assertEqual([r[0] for r in results], ['new-access'] * 5)
            self.assertEqual(count, ['A-private-refresh'])
            self.assertEqual((await services[0].token_store.read())['refresh_token'], 'new-refresh')
        finally: await asyncio.gather(*(s.close() for s in services))

    async def test_oauth_callback_saves_only_initiating_user_and_cannot_be_replayed(self):
        from app.services.integrations import provider_session
        from app.services.google_drive_service import GoogleDriveService
        from urllib.parse import urlparse, parse_qs
        original = GoogleDriveService.__init__
        def init(service, config, **kwargs):
            original(service, config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'access_token': 'A-google', 'refresh_token': 'A-refresh', 'expires_in': 900})), **kwargs)
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(users=self.db)))
        with patch.object(settings, 'google_drive_client_id', 'client'), patch.object(settings, 'google_drive_client_secret', 'secret'), patch.object(GoogleDriveService, '__init__', init):
            async with provider_session(self.db, self.a, 'google_drive') as service:
                start = await service.authorization_url()
            state = parse_qs(urlparse(start['authorization_url']).query)['state'][0]
            callback = callback_session(request, state, 'google_drive')
            service = await anext(callback)
            await service.complete_authorization('code', state)
            await callback.aclose()
            self.assertEqual((await CredentialStore(self.db, 'A', 'google_drive').read())['refresh_token'], 'A-refresh')
            self.assertIsNone(await CredentialStore(self.db, 'B', 'google_drive').read())
            with self.assertRaises(HTTPException): await anext(callback_session(request, state, 'google_drive'))


    async def test_dynamodb_wire_types_and_revocation_condition_are_serialized_once(self):
        db = UserStore(); wire = Mock()
        wire.get_item.return_value = {'Item': {'pk': {'S': 'USER#A#CONNECTION#google_drive'}, 'version': {'N': '2'}}}
        with patch.object(db, '_db', return_value=wire), patch.object(settings, 'auth_table_name', 'shared'):
            self.assertEqual(int((await db.get('USER#A#CONNECTION#google_drive'))['version']), 2)
            await db.put('USER#A#CONNECTION#google_drive', {'version': 3, 'cipher': {'data': 'encrypted'}}, version=2, family='family-A')
            transaction = wire.transact_write_items.call_args.kwargs['TransactItems']
            self.assertEqual(transaction[0]['Put']['ExpressionAttributeValues'], {':v': {'N': '2'}})
            self.assertEqual(transaction[0]['Put']['Item']['cipher'], {'M': {'data': {'S': 'encrypted'}}})
            self.assertEqual(transaction[1]['ConditionCheck']['Key'], {'pk': {'S': 'REVOKED#family-A'}})
            await db.limit('A', 'http', 120, 60)
            self.assertEqual(wire.update_item.call_args.kwargs['ExpressionAttributeValues'][':max'], {'N': '120'})

    async def test_disconnect_does_not_erase_a_newer_consent(self):
        old = await self.seed('A'); await old.read()
        new = CredentialStore(self.db, 'A', 'google_drive'); await new.read(); await new.write({'access_token': 'new', 'refresh_token': 'new'})
        with self.assertRaises(StoreConflict): await old.clear()
        self.assertEqual((await new.read())['access_token'], 'new')


class StorageTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        table = patch.object(settings, 'auth_table_name', 'test-table')
        table.start()
        self.addCleanup(table.stop)

    async def check_failure(self, codes, expected_calls):
        from unittest.mock import MagicMock
        from botocore.exceptions import ClientError
        from app.services.user_store import UserStore,StoreConflict
        db=UserStore();client=MagicMock();db._table=client
        error=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':code} for code in codes]},'TransactWriteItems')
        client.transact_write_items.side_effect=error
        with self.assertRaises(StoreConflict):
            await db.put('USER#test#MEETING#one',{'version':2},version=1,family='login')
        self.assertEqual(client.transact_write_items.call_count,expected_calls)
        calls=client.transact_write_items.call_args_list
        self.assertTrue(all(call==calls[0] for call in calls))

    async def test_shared_fence_conflicts_retry_same_transaction_with_a_bounded_budget(self):
        await self.check_failure(['None','TransactionConflict'],3)

    async def test_revocation_or_version_conflicts_never_retry(self):
        for codes in [['None','ConditionalCheckFailed'],['TransactionConflict','ConditionalCheckFailed'],[]]:
            await self.check_failure(codes,1)

    async def test_transient_conflict_can_commit_without_changing_the_fences(self):
        from unittest.mock import MagicMock
        from botocore.exceptions import ClientError
        from app.services.user_store import UserStore
        db=UserStore();db._table=MagicMock()
        db._table.transact_write_items.side_effect=[ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'None'},{'Code':'TransactionConflict'}]},'TransactWriteItems'),{}]
        await db.put('USER#test#MEETING#one',{'version':2},version=1,family='login')
        calls=db._table.transact_write_items.call_args_list
        self.assertEqual(len(calls),2);self.assertEqual(calls[0],calls[1])
        self.assertIn('ConditionCheck',calls[0].kwargs['TransactItems'][1])
