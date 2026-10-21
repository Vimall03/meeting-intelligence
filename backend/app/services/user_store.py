"""Server-only user records. No local credential fallback is permitted."""
import asyncio
import base64
import json
import secrets
import time
import threading
from contextlib import asynccontextmanager

import boto3
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from boto3.dynamodb.types import TypeSerializer, TypeDeserializer

from app.core.config import settings


class StoreConflict(Exception):
    pass


class UserStore:
    def __init__(self):
        self._table = None
        self._kms = None
        self._sdk_lock = threading.Lock()

    def _db(self):
        if not settings.auth_table_name:
            raise RuntimeError('Authentication storage is not configured')
        with self._sdk_lock:
            if self._table is None:
                self._table = boto3.client('dynamodb', region_name=settings.aws_region,
                    config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))
        return self._table

    async def get(self, key):
        row = await asyncio.to_thread(lambda: self._db().get_item(TableName=settings.auth_table_name,
            Key={'pk': {'S': key}}, ConsistentRead=True).get('Item'))
        return {k: TypeDeserializer().deserialize(v) for k, v in row.items()} if row else None

    async def put(self, key, value, *, version=None, absent=False, family=None):
        encode = TypeSerializer().serialize
        put = {'TableName': settings.auth_table_name, 'Item': {k: encode(v) for k, v in {'pk': key, **value}.items()}}
        if absent:
            put['ConditionExpression'] = 'attribute_not_exists(pk)'
        elif version is not None:
            put.update(ConditionExpression='#v = :v', ExpressionAttributeNames={'#v': 'version'},
                       ExpressionAttributeValues={':v': encode(version)})
        try:
            if family:
                transaction = [
                    {'Put': put}, {'ConditionCheck': {'TableName': settings.auth_table_name,
                    'Key': {'pk': {'S': 'REVOKED#' + family}}, 'ConditionExpression': 'attribute_not_exists(pk)'}}]
                def commit():
                    # Concurrent meetings in one login share the revocation fence.
                    # DynamoDB may briefly conflict even when both checks are reads.
                    # Retry the identical transaction, never re-encrypt or weaken
                    # the version/revocation conditions. Other failures stay fatal.
                    for attempt in range(3):
                        try:
                            return self._db().transact_write_items(TransactItems=transaction)
                        except Exception as error:
                            response = getattr(error, 'response', {})
                            codes = {r.get('Code') for r in response.get('CancellationReasons', [])}
                            retryable = (response.get('Error', {}).get('Code') == 'TransactionCanceledException'
                                and 'TransactionConflict' in codes and codes <= {'None', 'TransactionConflict'})
                            if not retryable or attempt == 2:
                                raise
                            time.sleep(.02 * 3**attempt + secrets.randbelow(20)/1000)
                await asyncio.to_thread(commit)
            else:
                await asyncio.to_thread(lambda: self._db().put_item(**put))
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('ConditionalCheckFailedException', 'TransactionCanceledException'):
                raise StoreConflict() from None
            raise

    async def take(self, key):
        row = await self.get(key)
        if not row or int(row.get('expiresAt', 0)) <= time.time() or row.get('used'):
            raise StoreConflict()
        await self.put(key, {**row, 'used': True, 'version': int(row['version']) + 1}, version=row['version'])
        return row

    async def limit(self, user, action, maximum, seconds):
        now = int(time.time())
        key = f'USER#{user}#LIMIT#{action}#{now // seconds}'
        try:
            await asyncio.to_thread(lambda: self._db().update_item(TableName=settings.auth_table_name, Key={'pk': {'S': key}},
                UpdateExpression='SET expiresAt = :ttl ADD #n :one',
                ConditionExpression='attribute_not_exists(#n) OR #n < :max',
                ExpressionAttributeNames={'#n': 'count'},
                ExpressionAttributeValues={':ttl': {'N': str(now + seconds * 2)}, ':one': {'N': '1'}, ':max': {'N': str(maximum)}}))
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
                from app.core.auth import failure
                raise failure('MI-AUTH-102', 429) from None
            raise

    def _key_service(self):
        if not settings.integration_key_id:
            raise RuntimeError('Integration encryption is not configured')
        with self._sdk_lock:
            if self._kms is None:
                self._kms = boto3.client('kms', region_name=settings.aws_region,
                    config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))
        return self._kms

    async def encrypt(self, owner, provider, token):
        context = {'user': owner, 'provider': provider, 'purpose': 'integration-v1'}
        aad = json.dumps(context, sort_keys=True).encode()
        key = await asyncio.to_thread(lambda: self._key_service().generate_data_key(
            KeyId=settings.integration_key_id, KeySpec='AES_256', EncryptionContext=context))
        nonce = secrets.token_bytes(12)
        cipher = AESGCM(key['Plaintext']).encrypt(nonce, json.dumps(token).encode(), aad)
        return {'key': base64.b64encode(key['CiphertextBlob']).decode(),
                'nonce': base64.b64encode(nonce).decode(), 'data': base64.b64encode(cipher).decode()}

    async def decrypt(self, owner, provider, payload):
        context = {'user': owner, 'provider': provider, 'purpose': 'integration-v1'}
        key = await asyncio.to_thread(lambda: self._key_service().decrypt(KeyId=settings.integration_key_id,
            CiphertextBlob=base64.b64decode(payload['key']), EncryptionContext=context))
        raw = AESGCM(key['Plaintext']).decrypt(base64.b64decode(payload['nonce']),
            base64.b64decode(payload['data']), json.dumps(context, sort_keys=True).encode())
        return json.loads(raw)


class CredentialStore:
    def __init__(self, db, user, provider, family=None):
        self.db, self.user, self.provider = db, user, provider
        self.key = f'USER#{user}#CONNECTION#{provider}'
        self.lease = None
        self.version = None
        self.base_version = None
        self.family = family
        self.callback_version = None
        self._local_lock = asyncio.Lock()

    async def read(self):
        row = await self.db.get(self.key)
        self.base_version = int(row['version']) if row else None
        return await self.db.decrypt(self.user, self.provider, row['cipher']) if row and row.get('cipher') else None

    async def write(self, token):
        row = await self.db.get(self.key)
        version = int(row['version']) if row else None
        expected = self.callback_version if self.callback_version is not None else self.version if self.lease else self.base_version
        if version != expected or (self.lease and row.get('lease') != self.lease):
            raise StoreConflict()
        cipher = await self.db.encrypt(self.user, self.provider, token)
        new = {'version': (version or 0) + 1, 'cipher': cipher}
        if self.lease:
            new.update(lease=self.lease, leaseUntil=row['leaseUntil'])
        await self.db.put(self.key, new, version=version, absent=row is None, family=self.family)
        self.base_version = self.version = new['version']
        self.callback_version = None

    async def clear(self):
        row = await self.db.get(self.key)
        if self.lease and (not row or row.get('lease') != self.lease or row['version'] != self.version):
            raise StoreConflict()
        if not self.lease and self.base_version is not None and (not row or int(row['version']) != self.base_version):
            # Do not erase a reconnect completed during provider revocation.
            raise StoreConflict()
        await self.db.put(self.key, {'version': int(row['version']) + 1 if row else 1},
                          version=row['version'] if row else None, absent=row is None)
        self.base_version = self.version = int(row['version']) + 1 if row else 1

    @asynccontextmanager
    async def refresh_lock(self):
        async with self._local_lock:
            async with self._distributed_lock():
                yield

    @asynccontextmanager
    async def _distributed_lock(self):
        lease = secrets.token_hex(16)
        deadline = time.monotonic() + 12
        while True:
            row = await self.db.get(self.key)
            if not row or not row.get('cipher'):
                raise StoreConflict()
            if float(row.get('leaseUntil', 0)) <= time.time():
                try:
                    next_version = int(row['version']) + 1
                    await self.db.put(self.key, {**row, 'version': next_version, 'lease': lease, 'leaseUntil': int(time.time()) + 45}, version=row['version'])
                    self.lease, self.version = lease, next_version
                    break
                except StoreConflict:
                    pass
            if time.monotonic() >= deadline:
                raise StoreConflict()
            await asyncio.sleep(.1 + secrets.randbelow(100) / 1000)
        try:
            yield
        finally:
            row = await self.db.get(self.key)
            if row and row.get('lease') == lease:
                try:
                    await self.db.put(self.key, {k: v for k, v in row.items() if k not in ('lease', 'leaseUntil')}, version=row['version'])
                except StoreConflict:
                    pass
            self.lease = self.version = None
