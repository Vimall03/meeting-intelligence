"""Durable MOM state, fenced room ownership, and private temporary transcripts.

No audio is retained. Queue messages contain only an opaque session key. The
separate table holds preferences and minimal workflow metadata, never transcript
text or email bodies. All content objects use the dedicated encrypted bucket.
"""
import hashlib
import json
import re
import secrets
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config

MODES = {'live_only', 'mom_only', 'both'}
ROOM = re.compile(r'(?:[a-z0-9]{2,4}-[a-z0-9]{2,4}-[a-z0-9]{2,4}|zoom:[0-9]{9,11})')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}')
TERMINAL = {'sent', 'empty', 'expired'}


class MomError(Exception):
    def __init__(self, code='MI-MOM-001'):
        self.code = code
        super().__init__(code)


class Conflict(MomError):
    def __init__(self): super().__init__('MI-MOM-004')


def encode(value):
    if isinstance(value, float): return Decimal(str(value))
    if isinstance(value, dict): return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list): return [encode(v) for v in value]
    return value


def decode(value):
    # boto3 returns DynamoDB numbers as Decimal. Session clocks and datetime
    # consume ordinary Python numbers; keep integer timestamps/versions exact.
    if isinstance(value, Decimal): return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict): return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list): return [decode(v) for v in value]
    return value


class Repository:
    LEASE_SECONDS = 90
    MAX_CAPTURES = 64

    def __init__(self, *, table, bucket, key_arn, queue, region, recovery_table='',
                 grace_seconds=600, retention_seconds=86400, max_session_seconds=21600,
                 clock=time.time, clients=None):
        self.table, self.bucket, self.key_arn, self.queue = table, bucket, key_arn, queue
        self.recovery_table = recovery_table
        self.grace, self.retention, self.max_session = grace_seconds, retention_seconds, max_session_seconds
        self.clock, self.clients = clock, clients or {}
        self.region = region

    def client(self, service):
        if service not in self.clients:
            self.clients[service] = boto3.client(service, region_name=self.region,
                config=Config(connect_timeout=3, read_timeout=10, retries={'max_attempts': 1}))
        return self.clients[service]

    def get(self, key):
        item = self.client('dynamodb').get_item(TableName=self.table,
            Key={'pk': {'S': key}}, ConsistentRead=True).get('Item')
        return decode({k: TypeDeserializer().deserialize(v) for k, v in item.items()}) if item else None

    def put(self, row, *, version=None):
        serializer = TypeSerializer()
        args = {'TableName': self.table, 'Item': {k: serializer.serialize(v) for k, v in encode(row).items()}}
        if version is None:
            args['ConditionExpression'] = 'attribute_not_exists(pk)'
        else:
            args.update(ConditionExpression='#v = :v', ExpressionAttributeNames={'#v': 'version'},
                ExpressionAttributeValues={':v': {'N': str(version)}})
        try: self.client('dynamodb').put_item(**args)
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') == 'ConditionalCheckFailedException': raise Conflict() from None
            raise
        return row

    def replace(self, row, **changes):
        updated = {**row, **changes, 'version': int(row['version']) + 1}
        updated = {k: v for k, v in updated.items() if v is not None}
        return self.put(updated, version=row['version'])

    def create_room(self, pointer, row, prior):
        serializer = TypeSerializer()
        room_put = {'TableName': self.table, 'Item': {k: serializer.serialize(v) for k, v in pointer.items()}}
        if prior:
            room_put.update(ConditionExpression='#v = :v', ExpressionAttributeNames={'#v': 'version'},
                ExpressionAttributeValues={':v': {'N': str(prior['version'])}})
        else: room_put['ConditionExpression'] = 'attribute_not_exists(pk)'
        try:
            self.client('dynamodb').transact_write_items(TransactItems=[{'Put': room_put}, {'Put': {
                'TableName': self.table, 'Item': {k: serializer.serialize(v) for k, v in row.items()},
                'ConditionExpression': 'attribute_not_exists(pk)'}}])
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') == 'TransactionCanceledException': raise Conflict() from None
            raise

    def preferences(self, user):
        return self.get('PREFS#' + user) or {'mode': 'live_only', 'timezone': 'UTC'}

    def set_preferences(self, user, mode, timezone, username=None):
        if mode not in MODES: raise MomError('MI-MOM-002')
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError): raise MomError('MI-MOM-002') from None
        key = 'PREFS#' + user
        row = self.get(key)
        values = {'mode': mode, 'timezone': timezone}
        if username: values['username'] = username
        return self.replace(row, **values) if row else self.put({'pk': key, 'version': 1, **values})

    @staticmethod
    def prefix(row): return f"meetings/{row['userId']}/{row['sessionId']}/"

    def begin(self, user, room, capture, expected_session=None):
        if not IDENTIFIER.fullmatch(user) or not IDENTIFIER.fullmatch(capture) or not isinstance(room, str) or not ROOM.fullmatch(room):
            raise MomError('MI-MOM-003')
        now = int(self.clock())
        pointer_key = f'ROOM#{user}#{room}'
        for _ in range(3):
            pointer = self.get(pointer_key)
            prior_pointer = pointer
            row = self.get(pointer['sessionKey']) if pointer else None
            prefs = self.preferences(user)
            if row and row['state'] == 'active' and row['finalizeAfter'] > now:
                if expected_session and expected_session != row['sessionId']: raise Conflict()
                if row.get('owner') and row.get('leaseUntil', 0) > now: raise Conflict()
                captures = list(row['captures'])
                if capture not in captures: captures.append(capture)
                if len(captures) > self.MAX_CAPTURES: raise MomError('MI-MOM-007')
                try:
                    return self.replace(row, owner=secrets.token_hex(16), leaseUntil=now + self.LEASE_SECONDS,
                        captures=captures, finalizeAfter=now + self.grace, dueAt=min(now + self.grace, row['purgeAt']))
                except Conflict: continue
            if expected_session: raise Conflict()
            if row and row['state'] == 'active':
                if row.get('leaseUntil', 0) > now: raise Conflict()
                try: self.replace(row, state='ready', dueAt=now, owner=None, leaseUntil=None)
                except Conflict: continue
            if prefs['mode'] == 'live_only': return None
            if not prefs.get('username'): raise MomError('MI-MOM-008')
            session = secrets.token_hex(16)
            row = {'pk': f'MOM#{session}', 'sessionId': session, 'userId': user, 'username': prefs['username'],
                'roomId': room, 'mode': prefs['mode'], 'timezone': prefs['timezone'], 'version': 1,
                'state': 'active', 'owner': secrets.token_hex(16), 'leaseUntil': now + self.LEASE_SECONDS,
                'startedAt': now, 'finalizeAfter': now + self.grace, 'purgeAt': now + self.retention,
                'dueAt': min(now + self.grace, now + self.retention), 'captures': [capture], 'attempts': 0}
            pointer = {'pk': pointer_key, 'sessionKey': row['pk'], 'version': int(pointer['version']) + 1 if pointer else 1,
                'expiresAt': now + 30 * 86400}
            try:
                self.create_room(pointer, row, prior_pointer)
                return row
            except Conflict: continue
        raise Conflict()

    def owned(self, attachment):
        row = self.get(attachment['pk'])
        if not row or row['state'] != 'active' or row.get('owner') != attachment['owner'] or row.get('leaseUntil', 0) <= self.clock() or row['finalizeAfter'] <= self.clock():
            raise Conflict()
        if self.clock() >= row['purgeAt'] or self.clock() - row['startedAt'] >= self.max_session:
            raise MomError('MI-MOM-007')
        return row

    def touch(self, attachment):
        row = self.owned(attachment)
        now = int(self.clock())
        return self.replace(row, leaseUntil=now + self.LEASE_SECONDS,
            finalizeAfter=now + self.grace, dueAt=min(now + self.grace, row['purgeAt']))

    def write_content(self, row, name, body, *, once=False):
        args = {'Bucket': self.bucket, 'Key': self.prefix(row) + name, 'Body': body,
            'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': self.key_arn, 'BucketKeyEnabled': True,
            'ContentType': 'text/plain; charset=utf-8'}
        if once: args['IfNoneMatch'] = '*'
        try: self.client('s3').put_object(**args)
        except Exception as exc:
            if once and getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('PreconditionFailed', '412'): return False
            raise
        return True

    def append(self, attachment, capture, sequence, segments):
        row = self.touch(attachment)  # Fences ownership and protects pending I/O from finalization.
        if capture not in row['captures'] or not isinstance(sequence, int) or sequence < 1: raise Conflict()
        payload = json.dumps({'sequence': sequence, 'recordedAt': int(self.clock()), 'segments': segments}, ensure_ascii=False).encode()
        if len(payload) > 65536: raise MomError('MI-MOM-007')
        self.write_content(row, f'captures/{capture}/{sequence:08d}.json', payload, once=True)
        self.owned(attachment)  # Do not acknowledge content after losing the session fence.

    def save_checkpoint(self, attachment, capture, state):
        row = self.touch(attachment)
        if capture not in row['captures']: raise Conflict()
        while len(json.dumps(state).encode()) > 180_000 and state['timeline']['items']:
            state['timeline']['items'].pop()
        body = json.dumps(state).encode()
        if len(body) > 180_000: raise MomError('MI-MOM-007')
        self.write_content(row, f'checkpoints/{capture}/state.json', body)
        self.owned(attachment)

    def load_checkpoint(self, attachment, capture):
        try: return json.loads(self.read_content(attachment, f'checkpoints/{capture}/state.json', 180_000))
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404'): return None
            raise

    def detach(self, attachment, *, finish=False, incomplete=False):
        row = self.get(attachment['pk'])
        if not row or row['state'] != 'active' or row.get('owner') != attachment['owner']: return
        now = int(self.clock())
        deadline = now if finish else now + self.grace if row.get('leaseUntil', 0) > now else row['finalizeAfter']
        row = self.replace(row, owner=None, leaseUntil=None, incomplete=bool(incomplete or row.get('incomplete')), state='ready' if finish else 'active',
            finalizeAfter=deadline, dueAt=min(deadline, row['purgeAt']))
        if finish:
            try: self.enqueue(row)
            except Exception: pass  # Durable ready state is retried by the scheduled sweeper.

    def finish(self, user, session):
        row = self.status(user, session)
        if row['state'] in TERMINAL or row['state'] in {'ready', 'processing', 'sending', 'cleanup', 'delivery_unknown'}: return row
        if row.get('owner') and row.get('leaseUntil', 0) > self.clock(): raise Conflict()
        if row['state'] != 'active': raise Conflict()
        now = int(self.clock())
        row = self.replace(row, state='ready', dueAt=now, owner=None, leaseUntil=None)
        try: self.enqueue(row)
        except Exception: pass
        return row

    def status(self, user, session):
        if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session): raise MomError('MI-MOM-005')
        row = self.get('MOM#' + session)
        if not row or row['userId'] != user: raise MomError('MI-MOM-005')
        return row

    def enqueue(self, row):
        self.client('sqs').send_message(QueueUrl=self.queue, MessageBody=json.dumps({'sessionKey': row['pk']}))

    def due(self, state, now, limit=100):
        items = self.client('dynamodb').query(TableName=self.table, IndexName='work',
            KeyConditionExpression='#s = :s AND dueAt <= :now', ExpressionAttributeNames={'#s': 'state'},
            ExpressionAttributeValues={':s': {'S': state}, ':now': {'N': str(now)}}, Limit=limit).get('Items', [])
        return [{k: TypeDeserializer().deserialize(v) for k, v in row.items()} for row in items]

    def content_keys(self, row):
        token = None
        while True:
            response = self.client('s3').list_objects_v2(Bucket=self.bucket, Prefix=self.prefix(row),
                **({'ContinuationToken': token} if token else {}))
            for item in response.get('Contents', []): yield item['Key']
            if not response.get('IsTruncated'): break
            token = response['NextContinuationToken']

    def read_content(self, row, name, maximum=1_000_000):
        response = self.client('s3').get_object(Bucket=self.bucket, Key=self.prefix(row) + name)
        body = response['Body']
        try:
            data = body.read(maximum + 1)
            if len(data) > maximum: raise MomError('MI-MOM-007')
            return data
        finally: body.close()

    def unlink_room(self, row):
        if not row.get('roomId'): return
        try:
            self.client('dynamodb').delete_item(TableName=self.table,
                Key={'pk': {'S': f"ROOM#{row['userId']}#{row['roomId']}"}},
                ConditionExpression='sessionKey = :key', ExpressionAttributeValues={':key': {'S': row['pk']}})
        except Exception as exc:
            # A subsequent meeting may already own this room pointer.
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') != 'ConditionalCheckFailedException': raise

    def cleanup(self, row, final_state):
        # No writers can hold an active lease at this stage. This bucket is unversioned.
        keys = list(self.content_keys(row))
        for start in range(0, len(keys), 1000):
            response = self.client('s3').delete_objects(Bucket=self.bucket,
                Delete={'Objects': [{'Key': key} for key in keys[start:start+1000]], 'Quiet': True})
            if response.get('Errors'): raise MomError('MI-MOM-009')
        if list(self.content_keys(row)): raise MomError('MI-MOM-009')
        if not self.recovery_table: raise MomError('MI-MOM-009')
        for capture in row.get('captures', []):
            if not IDENTIFIER.fullmatch(capture): raise Conflict()
            self.client('dynamodb').delete_item(TableName=self.recovery_table,
                Key={'pk': {'S': f"USER#{row['userId']}#MEETING#{capture}"}})
        self.unlink_room(row)
        # Small tombstone only. No username, transcript, summary, title or recipient remains.
        minimal = {k: row[k] for k in ['pk', 'sessionId', 'userId', 'version']}
        minimal.update(state=final_state, sentAt=row.get('sentAt'), messageId=row.get('messageId'),
            supportCode=row.get('supportCode'), expiresAt=int(self.clock()) + 30 * 86400)
        return self.replace(minimal, **{k: v for k, v in minimal.items() if k not in ('pk', 'version')})
