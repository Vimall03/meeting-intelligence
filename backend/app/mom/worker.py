"""SQS recap worker and scheduled finalizer/cleanup, built from the same source.

SES has no idempotency token: a persisted sending intent plus an uncertain
outcome is never blindly sent again. Definite rejections can retry; ambiguous
outcomes become delivery_unknown. No transcript, email body or recipient is logged.
"""
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config

from .repository import Repository, MomError, Conflict, TERMINAL
from .processing import generate_minutes
from .email_template import render_minutes

logger = logging.getLogger(__name__)
MAX_TRANSCRIPT_CHARS = 300_000
MAX_OBJECTS = 8000
MAX_ATTEMPTS = 3


def failure_metric(code):
    # Embedded Metric Format: content-free alarms without CloudWatch API calls.
    print(json.dumps({'_aws': {'Timestamp': int(time.time() * 1000), 'CloudWatchMetrics': [{
        'Namespace': 'MeetingIntelligence/MOM', 'Dimensions': [[]],
        'Metrics': [{'Name': 'Failures', 'Unit': 'Count'}]}]}, 'Failures': 1, 'supportCode': code}))


@lru_cache(maxsize=1)
def repository():
    # Retain SDK connection pools across warm Lambda invocations. Recreating
    # them every minute forces DynamoDB to rebuild its KMS table-key cache.
    # Repository holds configuration and clients only, never meeting content.
    required = ['MOM_TABLE_NAME', 'MOM_BUCKET_NAME', 'MOM_KEY_ARN', 'MOM_QUEUE_URL', 'AUTH_TABLE_NAME']
    if any(not os.environ.get(name) for name in required): raise MomError()
    return Repository(table=os.environ['MOM_TABLE_NAME'], bucket=os.environ['MOM_BUCKET_NAME'],
        key_arn=os.environ['MOM_KEY_ARN'], queue=os.environ['MOM_QUEUE_URL'], region=os.environ['AWS_REGION'],
        recovery_table=os.environ['AUTH_TABLE_NAME'], grace_seconds=int(os.environ.get('MOM_GRACE_SECONDS', '600')))


def verified_email(cognito, pool, row):
    user = cognito.admin_get_user(UserPoolId=pool, Username=row['username'])
    attributes = {item['Name']: item['Value'] for item in user['UserAttributes']}
    email = attributes.get('email', '')
    if user.get('Enabled', True) is False or attributes.get('sub') != row['userId'] or attributes.get('email_verified') != 'true' or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise MomError('MI-MOM-008')
    return email


def transcript(repo, row):
    capture_order = {capture: n for n, capture in enumerate(row['captures'])}
    prefix = repo.prefix(row)
    keys = [key for key in repo.content_keys(row) if '/captures/' in key and key.endswith('.json')]
    if len(keys) > MAX_OBJECTS: raise MomError('MI-MOM-007')
    parsed = []
    for key in keys:
        name = key[len(prefix):]
        parts = name.split('/')
        if len(parts) != 3 or parts[1] not in capture_order or not re.fullmatch(r'\d{8}\.json', parts[2]): raise MomError('MI-MOM-010')
        parsed.append((capture_order[parts[1]], int(parts[2][:-5]), name))
    parsed.sort()
    repo.client('s3')  # Construct the shared client before entering the thread pool.
    output, size = [], 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        for start in range(0, len(parsed), 64):
            names = [entry[2] for entry in parsed[start:start+64]]
            for body in executor.map(lambda name: repo.read_content(row, name, 65536), names):
                payload = json.loads(body)
                for segment in payload['segments']:
                    text = str(segment['text']).strip()
                    size += len(text)
                    if size > MAX_TRANSCRIPT_CHARS: raise MomError('MI-MOM-007')
                    if text:
                        # Retain the capture date across rejoins/midnight for relative deadlines.
                        captured = datetime.fromtimestamp(payload.get('recordedAt', row['startedAt']), timezone.utc).astimezone(ZoneInfo(row['timezone']))
                        output.append(f'[{captured.isoformat()}] {text}')
    return '\n'.join(output)


class Processor:
    def __init__(self, repo, *, summarize, resolve_email, ses, sender, configuration_set):
        self.repo, self.summarize, self.resolve_email = repo, summarize, resolve_email
        self.ses, self.sender, self.configuration_set = ses, sender, configuration_set

    def process(self, key):
        if not re.fullmatch(r'MOM#[a-f0-9]{32}', key): raise MomError('MI-MOM-005')
        row = self.repo.get(key)
        if not row or row['state'] in TERMINAL: return
        if row['state'] == 'cleanup':
            self.repo.cleanup(row, row.get('cleanupFinalState', 'sent'))
            return
        if row['state'] != 'ready': return
        now = int(self.repo.clock())
        if row['purgeAt'] <= now:
            row = self.repo.replace(row, state='cleanup', dueAt=now, cleanupFinalState='expired')
            self.repo.cleanup(row, 'expired')
            return
        if row.get('attempts', 0) >= MAX_ATTEMPTS:
            self.repo.replace(row, state='failed', dueAt=row['purgeAt'], supportCode='MI-MOM-010')
            return
        row = self.repo.replace(row, state='processing', leaseUntil=now + 300, dueAt=min(now + 300, row['purgeAt']), attempts=row.get('attempts', 0) + 1)
        try:
            try: minutes = self.repo.read_content(row, 'minutes.txt', 24000).decode()
            except Exception as exc:
                if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in ('NoSuchKey', '404'): raise
                text = transcript(self.repo, row)
                if not text:
                    row = self.repo.replace(row, state='cleanup', dueAt=now, cleanupFinalState='empty')
                    self.repo.cleanup(row, 'empty')
                    return
                minutes = self.summarize(text, row)
                # Re-fence after slow inference and reserve enough time for content I/O.
                row = self.repo.replace(row, leaseUntil=int(self.repo.clock()) + 90,
                    dueAt=min(int(self.repo.clock()) + 90, row['purgeAt']))
                self.repo.write_content(row, 'minutes.txt', minutes.encode(), once=True)
            email = self.resolve_email(row)  # Server-verified identity, never an SQS/client recipient.
            if self.repo.clock() >= row['purgeAt']: raise MomError('MI-MOM-007')
        except Exception as exc:
            code = exc.code if isinstance(exc, MomError) else 'MI-MOM-010'
            failure_metric(code)
            self.repo.replace(row, state='failed', leaseUntil=None, dueAt=min(int(self.repo.clock()) + 300, row['purgeAt']), supportCode=code,
                retryable=code not in ('MI-MOM-007', 'MI-MOM-008'))
            return
        # Persist BEFORE calling SES. A crash/timeout after this point is ambiguous.
        row = self.repo.replace(row, state='sending', sendingAt=int(self.repo.clock()),
            leaseUntil=int(self.repo.clock()) + 120, dueAt=min(int(self.repo.clock()) + 120, row['purgeAt']))
        try:
            sent = self.ses.send_email(FromEmailAddress=self.sender, Destination={'ToAddresses': [email]},
                ConfigurationSetName=self.configuration_set, EmailTags=[{'Name': 'minutes', 'Value': 'mom'}],
                Content={'Simple': {'Subject': {'Data': 'Your meeting recap · Meeting Intelligence', 'Charset': 'UTF-8'},
                    'Body': {'Text': {'Data': minutes + '\n\nBased on the part of your meeting captured by Meeting Intelligence.', 'Charset': 'UTF-8'},
                        'Html': {'Data': render_minutes(minutes, row), 'Charset': 'UTF-8'}}}})
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
            failure_metric('MI-MOM-011' if code else 'MI-MOM-006')
            definite_rejection = code in ('TooManyRequestsException', 'ThrottlingException', 'MessageRejected', 'MailFromDomainNotVerifiedException', 'AccountSuspendedException', 'SendingPausedException', 'BadRequestException')
            self.repo.replace(row, state='failed' if definite_rejection else 'delivery_unknown', leaseUntil=None,
                dueAt=min(int(self.repo.clock()) + 300, row['purgeAt']) if definite_rejection else row['purgeAt'],
                retryable=code in ('TooManyRequestsException', 'ThrottlingException'), supportCode='MI-MOM-011' if definite_rejection else 'MI-MOM-006')
            return
        row = self.repo.replace(row, state='cleanup', sentAt=int(self.repo.clock()), messageId=sent['MessageId'],
            dueAt=int(self.repo.clock()), leaseUntil=None, cleanupFinalState='sent', supportCode=None)
        self.repo.cleanup(row, 'sent')


def sweep_repository(repo):
    now = int(repo.clock())
    for state in ['active', 'ready', 'processing', 'sending', 'cleanup', 'failed', 'delivery_unknown']:
        for candidate in repo.due(state, now):
            # GSI is eventually consistent; re-read/fence the authoritative item.
            row = repo.get(candidate['pk'])
            if not row or row['state'] != state or row.get('dueAt', now + 1) > now: continue
            try:
                if row.get('leaseUntil', 0) > now: continue
                if row['purgeAt'] <= now:
                    row = repo.replace(row, state='cleanup', dueAt=now, owner=None, leaseUntil=None,
                        cleanupFinalState='sent' if row.get('sentAt') else 'expired')
                    repo.cleanup(row, row['cleanupFinalState'])
                elif state == 'sending':
                    repo.replace(row, state='delivery_unknown', dueAt=row['purgeAt'], leaseUntil=None, supportCode='MI-MOM-006')
                elif state == 'active' and row['finalizeAfter'] <= now:
                    row = repo.replace(row, state='ready', dueAt=now, owner=None, leaseUntil=None)
                    repo.enqueue(row)
                    repo.replace(row, dueAt=min(now + 300, row['purgeAt']))
                elif state == 'processing':
                    row = repo.replace(row, state='ready', dueAt=now, leaseUntil=None)
                    repo.enqueue(row)
                elif state == 'failed' and row.get('retryable') and row.get('attempts', 0) < MAX_ATTEMPTS:
                    row = repo.replace(row, state='ready', dueAt=now)
                    repo.enqueue(row)
                elif state in ('ready', 'cleanup'):
                    repo.enqueue(row)
                    repo.replace(row, dueAt=min(now + 300, row['purgeAt']))
                elif state == 'failed':
                    repo.replace(row, dueAt=row['purgeAt'])
            except Conflict: continue
            except Exception:
                failure_metric('MI-MOM-009')
                logger.warning('MOM recovery needs attention (Code: MI-MOM-009)')


def build_processor(repo):
    def summarize(text, row):
        secret = repo.client('secretsmanager').get_secret_value(SecretId=os.environ['PROVIDER_SECRET_ARN'])
        api_key = json.loads(secret['SecretString'])['OPENROUTER_API_KEY']
        return generate_minutes(text, row, api_key)
    ses = boto3.client('sesv2', region_name=repo.region,
        config=Config(connect_timeout=3, read_timeout=10, retries={'max_attempts': 0}))
    return Processor(repo, summarize=summarize,
        resolve_email=lambda row: verified_email(repo.client('cognito-idp'), os.environ['AUTH_USER_POOL_ID'], row),
        ses=ses, sender=os.environ['MOM_SENDER_EMAIL'], configuration_set=os.environ['MOM_SES_CONFIGURATION_SET'])


def handler(event, context):
    repo = repository()
    processor = build_processor(repo)
    failures = []
    for record in event.get('Records', []):
        try:
            body = json.loads(record['body'])
            processor.process(body['sessionKey'])
        except Conflict: pass
        except Exception:
            logger.warning('Meeting recap job needs attention (Code: MI-MOM-010)')
            failures.append({'itemIdentifier': record['messageId']})
    return {'batchItemFailures': failures}


def sweep(event, context):
    sweep_repository(repository())
    return {'ok': True}
