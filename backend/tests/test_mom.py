import asyncio
import copy
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from botocore.exceptions import ClientError
from fastapi import FastAPI

from app.mom.repository import Repository, Conflict, MomError, RECOVERY_ACTIVITY_KEY
from app.mom.worker import Processor, sweep_repository, transcript, verified_email
from app.mom import worker as mom_worker
from app.mom.processing import generate_minutes
from app.api.routes import mom
from app.core.auth import Principal
from app.core.config import settings
from test_auth_integration import MemoryStore
from test_meeting_recovery import RecoverySocket, window
from streaming_fakes import transcription_stream, DefaultStream
from test_meeting_stream_timeline import route, FakeJev, FakeLlm


def rejected(code): return ClientError({'Error': {'Code': code}}, 'test')


class S3:
    def __init__(self): self.objects = {}; self.arguments = []; self.fail_delete = False
    def put_object(self, **args):
        self.arguments.append(args)
        if args.get('IfNoneMatch') == '*' and args['Key'] in self.objects: raise rejected('PreconditionFailed')
        self.objects[args['Key']] = bytes(args['Body'])
    def get_object(self, **args):
        if args['Key'] not in self.objects: raise rejected('NoSuchKey')
        return {'Body': io.BytesIO(self.objects[args['Key']])}
    def list_objects_v2(self, **args):
        return {'Contents': [{'Key': key} for key in sorted(self.objects) if key.startswith(args['Prefix'])]}
    def delete_objects(self, **args):
        if self.fail_delete: return {'Errors': [{'Code': 'Unavailable'}]}
        for obj in args['Delete']['Objects']: self.objects.pop(obj['Key'], None)
        return {}


class Dynamo:
    def __init__(self): self.deletions = []
    def delete_item(self, **args): self.deletions.append(args)


class Queue:
    def __init__(self): self.messages = []; self.fail = False
    def send_message(self, **args):
        if self.fail: raise RuntimeError('offline')
        self.messages.append(args)


class MemoryRepository(Repository):
    def __init__(self):
        self.now = [int(time.time())]
        self.s3, self.queue_client, self.dynamo = S3(), Queue(), Dynamo()
        super().__init__(table='mom', bucket='private', key_arn='key', queue='queue', region='region', recovery_table='recovery',
            clock=lambda: self.now[0], clients={'s3': self.s3, 'sqs': self.queue_client, 'dynamodb': self.dynamo})
        self.rows = {}; self.lock = threading.Lock()
    def get(self, key): return copy.deepcopy(self.rows.get(key))
    def put(self, row, *, version=None, activity_delta=0):
        with self.lock:
            current = self.rows.get(row['pk'])
            if version is None and current or version is not None and (not current or current['version'] != version): raise Conflict()
            if activity_delta:
                count = self.rows.get(RECOVERY_ACTIVITY_KEY, {}).get('activeCount', 0)
                if count + activity_delta < 0: raise Conflict()
                self.rows[RECOVERY_ACTIVITY_KEY] = {**self.rows.get(RECOVERY_ACTIVITY_KEY, {}),
                    'pk': RECOVERY_ACTIVITY_KEY, 'activeCount': count + activity_delta}
            self.rows[row['pk']] = copy.deepcopy(row)
            return copy.deepcopy(row)
    def create_room(self, pointer, row, prior):
        with self.lock:
            current = self.rows.get(pointer['pk'])
            if prior and (not current or current['version'] != prior['version']) or not prior and current: raise Conflict()
            self.rows[pointer['pk']] = copy.deepcopy(pointer); self.rows[row['pk']] = copy.deepcopy(row)
            self.rows[RECOVERY_ACTIVITY_KEY] = {**self.rows.get(RECOVERY_ACTIVITY_KEY, {}),
                'pk': RECOVERY_ACTIVITY_KEY, 'activeCount': self.rows.get(RECOVERY_ACTIVITY_KEY, {}).get('activeCount', 0) + 1}
    def unlink_room(self, row):
        key=f"ROOM#{row['userId']}#{row.get('roomId')}"
        with self.lock:
            if self.rows.get(key,{}).get('sessionKey')==row['pk']:self.rows.pop(key)
    def due(self, state, now, limit=100):
        return [copy.deepcopy(row) for row in self.rows.values() if row.get('state') == state and row.get('dueAt', now+1) <= now][:limit]


class Ses:
    def __init__(self, error=None): self.calls = []; self.error = error
    def send_email(self, **args):
        self.calls.append(args)
        if self.error: raise self.error
        return {'MessageId': 'receipt'}


class RecoveryConnectionTests(unittest.TestCase):
    def test_idle_sweep_performs_one_consistent_read_and_no_job_queries(self):
        environment = {'MOM_TABLE_NAME': 'mom', 'MOM_BUCKET_NAME': 'private',
            'MOM_KEY_ARN': 'key', 'MOM_QUEUE_URL': 'queue', 'AUTH_TABLE_NAME': 'recovery',
            'AWS_REGION': 'ap-south-1', 'MOM_RECOVERY_IDLE_GATE': 'true'}
        reads = []
        def get_item(**kwargs):
            reads.append(kwargs)
            return {'Item': {'pk': {'S': RECOVERY_ACTIVITY_KEY}, 'initialized': {'BOOL': True}, 'activeCount': {'N': '0'}}}
        client = SimpleNamespace(get_item=get_item)
        mom_worker.repository.cache_clear()
        try:
            with patch.dict('os.environ', environment), patch('app.mom.repository.boto3.client', return_value=client):
                for _ in range(60): self.assertEqual(mom_worker.sweep({}, None), {'ok': True})
            self.assertEqual(len(reads), 60)
            self.assertTrue(all(read['ConsistentRead'] for read in reads))
        finally:
            mom_worker.repository.cache_clear()

    def test_work_and_activity_counter_commit_in_one_transaction(self):
        transactions = []
        client = SimpleNamespace(transact_write_items=lambda **args: transactions.append(args))
        repo = Repository(table='mom', bucket='private', key_arn='key', queue='queue', region='region', clients={'dynamodb': client})
        repo.put({'pk': 'MOM#test', 'version': 2, 'state': 'sent'}, version=1, activity_delta=-1)
        items = transactions[0]['TransactItems']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['Put']['ConditionExpression'], '#v = :v')
        self.assertEqual(items[1]['Update']['Key']['pk']['S'], RECOVERY_ACTIVITY_KEY)
        self.assertEqual(items[1]['Update']['ExpressionAttributeValues'][':delta']['N'], '-1')
        self.assertIn('ConditionExpression', items[1]['Update'])

    def test_warm_sweeps_reuse_connection_pool_without_caching_work(self):
        environment = {'MOM_TABLE_NAME': 'mom', 'MOM_BUCKET_NAME': 'private',
            'MOM_KEY_ARN': 'key', 'MOM_QUEUE_URL': 'queue', 'AUTH_TABLE_NAME': 'recovery',
            'AWS_REGION': 'ap-south-1'}
        requests = []

        class EmptyDynamo:
            def query(self, **kwargs):
                requests.append(kwargs)
                return {'Items': []}

        client = EmptyDynamo()
        mom_worker.repository.cache_clear()
        try:
            with patch.dict('os.environ', environment), patch('app.mom.repository.boto3.client', return_value=client) as factory:
                mom_worker.repository().clock = lambda: 1234567890
                self.assertEqual(mom_worker.sweep({}, None), {'ok': True})
                self.assertEqual(mom_worker.sweep({}, None), {'ok': True})
                factory.assert_called_once()
                self.assertEqual(factory.call_args.args, ('dynamodb',))
                # Both invocations still query every workflow state for due work.
                self.assertEqual(len(requests), 14)
                self.assertEqual(requests[:7], requests[7:])
        finally:
            mom_worker.repository.cache_clear()


class MomTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.repo.set_preferences('A', 'both', 'Asia/Kolkata', 'verified-A')
    def begin(self, capture='capture-1', room='abc-defg-hij', user='A'):
        return self.repo.begin(user, room, capture)
    def record(self, row, text='Please send the proposal after approval.', capture='capture-1', sequence=1):
        self.repo.append(row, capture, sequence, [{'start': 0, 'end': 3, 'text': text}])
    def processor(self, ses=None, summarize=None, resolve_email=None):
        return Processor(self.repo, summarize=summarize or (lambda text, row: 'Next steps\n• Send proposal after approval.'),
            resolve_email=resolve_email or (lambda row: 'verified@example.com'), ses=ses or Ses(), sender='no-reply@example.com', configuration_set='mom')

    def test_idle_gate_wakes_on_create_and_stays_awake_until_cleanup(self):
        self.repo.rows[RECOVERY_ACTIVITY_KEY] = {'pk': RECOVERY_ACTIVITY_KEY, 'initialized': True, 'activeCount': 0}
        self.assertTrue(self.repo.recovery_idle())
        first = self.begin()
        second = self.begin('other', room='xyz-abcd-efg')
        self.assertFalse(self.repo.recovery_idle())
        self.assertEqual(self.repo.rows[RECOVERY_ACTIVITY_KEY]['activeCount'], 2)
        self.repo.detach(first, finish=True)
        self.processor().process(first['pk'])
        self.assertEqual(self.repo.rows[RECOVERY_ACTIVITY_KEY]['activeCount'], 1)
        self.repo.detach(second, finish=True)
        self.processor().process(second['pk'])
        self.assertTrue(self.repo.recovery_idle())
        self.processor().process(second['pk'])
        self.assertEqual(self.repo.rows[RECOVERY_ACTIVITY_KEY]['activeCount'], 0)

    def test_missing_gate_never_suppresses_legacy_work_and_is_enabled_explicitly(self):
        self.assertFalse(self.repo.recovery_idle())
        row = self.begin()
        self.repo.rows[row['pk']].pop('recoveryTracked')
        self.repo.rows.pop(RECOVERY_ACTIVITY_KEY)
        self.repo.detach(self.repo.get(row['pk']))
        self.assertTrue(self.repo.get(row['pk'])['recoveryTracked'])
        self.assertEqual(self.repo.get(RECOVERY_ACTIVITY_KEY)['activeCount'], 1)
        self.assertFalse(self.repo.recovery_idle())

    def test_failed_cleanup_and_uncertain_email_keep_recovery_enabled(self):
        self.repo.rows[RECOVERY_ACTIVITY_KEY] = {'pk': RECOVERY_ACTIVITY_KEY, 'initialized': True, 'activeCount': 0}
        row = self.begin(); self.record(row); self.repo.detach(row, finish=True)
        self.processor(Ses(TimeoutError())).process(row['pk'])
        self.assertFalse(self.repo.recovery_idle())
        self.repo.now[0] += 86401
        self.repo.s3.fail_delete = True
        sweep_repository(self.repo)
        self.assertEqual(self.repo.get(RECOVERY_ACTIVITY_KEY)['activeCount'], 1)
        self.repo.s3.fail_delete = False
        sweep_repository(self.repo)
        self.assertTrue(self.repo.recovery_idle())

    def test_duplicate_touches_in_same_second_do_not_change_deadlines_or_write_again(self):
        row = self.begin()
        version = row['version']
        for _ in range(3): self.repo.touch(row)
        self.assertEqual(self.repo.get(row['pk'])['version'], version)
        self.repo.now[0] += 1
        renewed = self.repo.touch(row)
        self.assertEqual(renewed['version'], version + 1)
        self.assertEqual(renewed['finalizeAfter'], self.repo.now[0] + 600)

    def test_new_users_are_off_without_a_preference_record(self):
        self.assertEqual(self.repo.preferences('new')['mode'], 'live_only')
        self.assertIsNone(self.repo.begin('new', 'abc-defg-hij', 'capture'))
        self.assertFalse(any(key.startswith('MOM#') for key in self.repo.rows))

    def test_same_user_room_rejoins_before_grace_without_duplicate_email_session(self):
        row = self.begin(); self.record(row); self.repo.detach(row)
        self.repo.now[0] += 599
        joined = self.begin('capture-2')
        self.assertEqual(row['sessionId'], joined['sessionId'])
        self.assertEqual(joined['captures'], ['capture-1', 'capture-2'])
        self.repo.append(joined, 'capture-2', 1, [{'text':'A second segment.', 'start':0,'end':3}])
        self.assertEqual([line.split('] ',1)[1] for line in transcript(self.repo, joined).splitlines()], ['Please send the proposal after approval.', 'A second segment.'])
        sweep_repository(self.repo)
        self.assertEqual(self.repo.queue_client.messages, [])

    def test_zoom_rejoin_and_recaps_use_isolated_platform_room_identity(self):
        zoom = self.begin(room='zoom:12345678901')
        self.record(zoom, 'Review the synthetic Zoom follow-up.')
        self.repo.detach(zoom)
        self.repo.now[0] += 30
        joined = self.begin('zoom-rejoin', room='zoom:12345678901')
        self.assertEqual(joined['sessionId'], zoom['sessionId'])
        self.assertNotEqual(self.begin('meet-capture')['sessionId'], zoom['sessionId'])
        self.assertNotEqual(self.begin('other-zoom', room='zoom:98765432101')['sessionId'], zoom['sessionId'])
        self.repo.detach(joined, finish=True)
        ses = Ses()
        self.processor(ses).process(joined['pk'])
        self.assertEqual(self.repo.get(joined['pk'])['state'], 'sent')
        self.assertEqual(len(ses.calls), 1)
        self.assertFalse(any(key.startswith(self.repo.prefix(joined)) for key in self.repo.s3.objects))

    def test_zoom_room_validation_rejects_passcodes_and_untrusted_identifiers(self):
        for room in ['zoom:123', 'zoom:123456789012', 'zoom:12345678901?pwd=secret',
                     'zoom:12345678901#other', 'https://zoom.us/j/12345678901', 'teams:12345678901']:
            with self.subTest(room=room), self.assertRaises(MomError) as error:
                self.begin(room=room)
            self.assertEqual(error.exception.code, 'MI-MOM-003')

    def test_grace_boundary_creates_new_session_and_old_is_ready(self):
        first = self.begin(); self.repo.detach(first)
        self.repo.now[0] += 600
        second = self.begin('next-capture')
        self.assertNotEqual(first['sessionId'], second['sessionId'])
        self.assertEqual(self.repo.get(first['pk'])['state'], 'ready')
        sweep_repository(self.repo)
        self.assertEqual(json.loads(self.repo.queue_client.messages[0]['MessageBody']), {'sessionKey': first['pk']})
        self.repo.cleanup(self.repo.get(first['pk']),'expired')
        self.assertEqual(self.repo.get('ROOM#A#abc-defg-hij')['sessionKey'],second['pk'])

    def test_different_users_and_rooms_never_share_content_or_status(self):
        a = self.begin(); self.record(a)
        self.repo.set_preferences('B', 'mom_only', 'UTC', 'verified-B')
        b = self.begin(user='B')
        self.assertNotEqual(a['sessionId'], b['sessionId'])
        self.assertEqual(transcript(self.repo, b), '')
        with self.assertRaises(MomError): self.repo.status('B', a['sessionId'])
        self.assertNotEqual(a['sessionId'], self.begin(room='xyz-abcd-efg')['sessionId'])

    def test_active_owner_and_stale_writer_are_fenced(self):
        row = self.begin()
        with self.assertRaises(Conflict): self.begin('second')
        with self.assertRaises(Conflict): self.repo.finish('A', row['sessionId'])
        self.repo.detach(row); joined = self.begin('second')
        with self.assertRaises(Conflict): self.record(row)
        self.repo.detach(row)  # Old detach cannot shorten/extend the new connection.
        self.assertEqual(self.repo.get(row['pk'])['owner'], joined['owner'])

    def test_segments_replay_idempotently_and_checkpoints_stay_out_of_transcript(self):
        row = self.begin(); self.record(row); self.record(row, 'Different retry wording')
        self.repo.save_checkpoint(row, 'capture-1', {'transcript':'rolling private text','timeline':{'items':[]}})
        self.assertTrue(transcript(self.repo,row).endswith('Please send the proposal after approval.'))
        self.assertEqual(self.repo.load_checkpoint(row,'capture-1')['transcript'],'rolling private text')
        self.assertTrue(all(args['ServerSideEncryption']=='aws:kms' and args['BucketKeyEnabled'] for args in self.repo.s3.arguments))

    def test_preferences_change_applies_to_new_session_not_existing_rejoin(self):
        first=self.begin(); self.repo.detach(first)
        self.repo.set_preferences('A','live_only','UTC')
        self.assertEqual(self.begin('rejoin')['mode'],'both')

    def test_finish_is_idempotent_and_queue_failure_is_recovered(self):
        row=self.begin(); self.record(row); self.repo.queue_client.fail=True
        self.repo.detach(row,finish=True)
        self.assertEqual(self.repo.get(row['pk'])['state'],'ready')
        self.repo.queue_client.fail=False; sweep_repository(self.repo)
        self.assertEqual(len(self.repo.queue_client.messages),1)
        self.assertEqual(self.repo.finish('A',row['sessionId'])['state'],'ready')

    def test_send_cleanup_deletes_all_content_checkpoints_and_keeps_only_receipt(self):
        row=self.begin(); self.record(row)
        self.repo.save_checkpoint(row,'capture-1',{'transcript':'private','timeline':{'items':[]}})
        self.repo.detach(row,finish=True)
        ses=Ses(); worker=self.processor(ses)
        worker.process(row['pk']); worker.process(row['pk'])
        self.assertEqual(len(ses.calls),1)
        self.assertFalse(list(self.repo.content_keys(row)))
        receipt=self.repo.get(row['pk'])
        self.assertEqual(receipt['state'],'sent');self.assertEqual(receipt['messageId'],'receipt')
        for field in ['username','roomId','captures','purgeAt','owner','dueAt']:self.assertNotIn(field,receipt)
        self.assertEqual(self.repo.dynamo.deletions[0]['Key']['pk']['S'],'USER#A#MEETING#capture-1')
        self.assertNotIn('private',json.dumps(receipt))
        self.assertNotIn('ROOM#A#abc-defg-hij',self.repo.rows)

    def test_cleanup_failure_retries_cleanup_without_resending(self):
        row=self.begin();self.record(row);self.repo.detach(row,finish=True)
        ses=Ses();worker=self.processor(ses);self.repo.s3.fail_delete=True
        with self.assertRaises(MomError):worker.process(row['pk'])
        self.assertEqual(self.repo.get(row['pk'])['state'],'cleanup')
        self.repo.s3.fail_delete=False;worker.process(row['pk'])
        self.assertEqual(len(ses.calls),1);self.assertFalse(self.repo.s3.objects)

    def test_ambiguous_send_never_retries_and_expired_content_is_purged(self):
        row=self.begin();self.record(row);self.repo.detach(row,finish=True)
        ses=Ses(TimeoutError('uncertain SES result'));worker=self.processor(ses)
        worker.process(row['pk']);worker.process(row['pk'])
        self.assertEqual(len(ses.calls),1)
        self.assertEqual(self.repo.get(row['pk'])['state'],'delivery_unknown')
        self.assertTrue(self.repo.s3.objects)
        self.repo.now[0]+=86401;sweep_repository(self.repo)
        self.assertFalse(self.repo.s3.objects);self.assertEqual(len(ses.calls),1)

    def test_crash_after_send_does_not_produce_second_email(self):
        row=self.begin();self.record(row);self.repo.detach(row,finish=True)
        real=self.repo.replace
        def failing(row,**fields):
            if fields.get('state')=='cleanup' and fields.get('sentAt'):raise RuntimeError('write outage after SES accepted')
            return real(row,**fields)
        ses=Ses()
        with patch.object(self.repo,'replace',failing):
            with self.assertRaises(RuntimeError):self.processor(ses).process(row['pk'])
        self.repo.now[0]+=121;sweep_repository(self.repo)
        self.processor(ses).process(row['pk'])
        self.assertEqual(len(ses.calls),1);self.assertEqual(self.repo.get(row['pk'])['state'],'delivery_unknown')

    def test_definite_throttle_can_retry_and_reuses_generated_minutes(self):
        row=self.begin();self.record(row);self.repo.detach(row,finish=True)
        generated=[]
        def summary(text,row):generated.append(text);return 'Key points\nOne point.'
        ses=Ses(rejected('TooManyRequestsException'));worker=self.processor(ses,summarize=summary)
        worker.process(row['pk']);self.assertEqual(self.repo.get(row['pk'])['state'],'failed')
        self.repo.now[0]+=301;sweep_repository(self.repo);ses.error=None;worker.process(row['pk'])
        self.assertEqual(len(generated),1);self.assertEqual(len(ses.calls),2)
        self.assertEqual(self.repo.get(row['pk'])['state'],'sent')

    def test_no_words_does_not_send_email_and_expired_recording_is_deleted(self):
        row=self.begin();self.repo.detach(row,finish=True);ses=Ses();self.processor(ses).process(row['pk'])
        self.assertEqual(ses.calls,[]);self.assertEqual(self.repo.get(row['pk'])['state'],'empty')
        row=self.begin('later');self.record(row,capture='later');self.repo.now[0]+=86401;sweep_repository(self.repo)
        self.assertFalse(self.repo.s3.objects)

    def test_failed_generation_is_bounded_and_never_sends_raw_transcript(self):
        row=self.begin();self.record(row,'sensitive words');self.repo.detach(row,finish=True)
        def broken(*args):raise RuntimeError('provider body contains secret')
        ses=Ses();worker=self.processor(ses,summarize=broken)
        for _ in range(3):
            worker.process(row['pk']);self.repo.now[0]+=301;sweep_repository(self.repo)
        self.assertEqual(self.repo.get(row['pk'])['attempts'],3)
        self.assertEqual(ses.calls,[])
        self.assertNotIn('secret',json.dumps(self.repo.get(row['pk'])))

    def test_recipient_comes_from_matching_verified_cognito_user(self):
        row=self.begin()
        cognito=SimpleNamespace(admin_get_user=lambda **args:{'UserAttributes':[
            {'Name':'sub','Value':'A'},{'Name':'email','Value':'verified@example.com'},{'Name':'email_verified','Value':'true'}]})
        self.assertEqual(verified_email(cognito,'pool',row),'verified@example.com')
        for sub,verified in [('B','true'),('A','false')]:
            cognito.admin_get_user=lambda **args:{'UserAttributes':[{'Name':'sub','Value':sub},{'Name':'email_verified','Value':verified},{'Name':'email','Value':'other@example.com'}]}
            with self.assertRaises(MomError):verified_email(cognito,'pool',row)

    def test_gemma_prompt_is_concise_text_and_no_diarization_tools_or_schema(self):
        captured=[]
        def open_request(request,timeout):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'model':'google/gemma-4-26b-a4b-it','choices':[{'finish_reason':'stop','message':{'content':'Key points\n• A decision.'}}]}).encode())
        output=generate_minutes('untrusted meeting words',self.begin(),'test-key',opener=open_request)
        self.assertIn('Key points',output)
        payload=captured[0];self.assertEqual(payload['reasoning'],{'effort':'none'})
        self.assertNotIn('response_format',payload);self.assertNotIn('tools',payload)
        self.assertIn('200–300 words',payload['messages'][0]['content'])
        self.assertIn('conditions',payload['messages'][0]['content'])


class MomRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo=MemoryRepository();self.user=Principal('A','family-A',int(time.time())+900)
        self.app=FastAPI();self.app.state.mom=self.repo;self.app.include_router(mom.router)
        self.app.dependency_overrides[mom.current_user]=lambda:self.user
        self.patches=[patch.object(settings,key,value) for key,value in {
            'mom_enabled':True,'mom_table_name':'mom','mom_bucket_name':'private','mom_key_arn':'key','mom_queue_url':'queue','auth_table_name':'recovery'}.items()]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
    async def test_defaults_optin_verified_identity_and_no_arbitrary_recipient(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/mom/settings')).json()['mode'],'live_only')
            self.assertEqual((await client.post('/api/mom/settings',json={'mode':'both','recipient':'attacker@example.com'})).status_code,422)
            with patch.object(mom,'verified_username',return_value='verified-A'):
                response=await client.post('/api/mom/settings',json={'mode':'both','timezone':'Asia/Kolkata'})
                self.assertEqual(response.status_code,200);self.assertEqual(response.json()['mode'],'both')
            self.assertEqual(self.repo.preferences('A')['username'],'verified-A')
            self.user=Principal('B','family-B',self.user.expires)
            self.assertEqual((await client.get('/api/mom/settings')).json()['mode'],'live_only')
    async def test_cross_user_status_finish_and_uncertain_retry_rejected_safely(self):
        self.repo.set_preferences('A','both','UTC','verified-A');row=self.repo.begin('A','abc-defg-hij','capture')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
            self.user=Principal('B','family-B',self.user.expires)
            for suffix in ['', '/finish']:
                request=client.get if not suffix else client.post
                response=await request('/api/mom/sessions/'+row['sessionId']+suffix)
                self.assertEqual(response.status_code,404);self.assertIn('MI-MOM-005',response.text)
            self.user=Principal('A','family-A',self.user.expires)
            self.repo.replace(self.repo.get(row['pk']),state='delivery_unknown',dueAt=row['purgeAt'])
            response=await client.post('/api/mom/sessions/'+row['sessionId']+'/retry')
            self.assertIn('MI-MOM-006',response.text)
    async def test_routes_require_signin_and_unconfigured_feature_is_off(self):
        self.app.dependency_overrides.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/mom/settings')).status_code,401)
        self.app.dependency_overrides[mom.current_user]=lambda:self.user
        with patch.object(settings,'mom_enabled',False):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
                data=(await client.get('/api/mom/settings')).json()
                self.assertEqual(data['mode'],'live_only');self.assertFalse(data['minutes_available'])
                response=await client.post('/api/mom/settings',json={'mode':'both'})
                self.assertEqual(response.status_code,503);self.assertIn('MI-MOM-001',response.text)


class MomStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_recaps_keep_ownership_without_reuploading_checkpoints(self):
        db = MemoryStore(); repo = MemoryRepository()
        repo.set_preferences('A', 'mom_only', 'UTC', 'verified-A')
        class Socket(RecoverySocket):
            async def receive_text(self):
                payload = json.loads(await super().receive_text())
                return json.dumps({**payload, 'roomId': 'opt-test-room'})
            async def receive(self):
                await asyncio.sleep(5.4)
                return {'type': 'websocket.receive', 'text': json.dumps({'type': 'end', 'lastSequence': 0})}
        ws = Socket(db); ws.app.state.mom = repo
        with patch.object(route, 'mom_available', return_value=True), patch.object(route, 'mom_repository', return_value=repo), patch.object(route, 'AmazonStreamingTranscription', DefaultStream):
            await asyncio.wait_for(route.meeting_stream(ws), 7)
        self.assertEqual(len(repo.s3.arguments), 1)
        self.assertEqual(len(db.crypto.keys), 1)
        self.assertTrue(any(message['type'] == 'heartbeat' for message in ws.sent))
        self.assertTrue(any(message['type'] == 'meeting_segment_ended' for message in ws.sent))

    async def test_mom_only_does_not_open_connectors_or_run_live_models_and_drains_end(self):
        await self.check_mom_stream('abc-defg-hij')

    async def test_zoom_stream_authenticates_drains_audio_and_saves_a_recoverable_recap(self):
        await self.check_mom_stream('zoom:12345678901')

    async def check_mom_stream(self, room):
        db=MemoryStore();repo=MemoryRepository();repo.set_preferences('A','mom_only','UTC','verified-A')
        class Socket(RecoverySocket):
            async def receive_text(self):
                payload=json.loads(await super().receive_text());payload['roomId']=room;return json.dumps(payload)
        frames=window(1,'first')+window(2,'second')+[{'type':'websocket.receive','text':json.dumps({'type':'finish','lastSequence':6400})}]
        ws=Socket(db,frames);ws.app.state.mom=repo
        async def transcribe_audio(audio):
            await asyncio.sleep(.01)
            return {'audio_duration':3,'segments':[{'text':audio.decode().strip(),'start':0,'end':3}]}
        def forbidden(*args,**kwargs):raise AssertionError('Live provider invoked in MOM-only mode')
        with patch.object(route,'mom_available',return_value=True),patch.object(route,'mom_repository',return_value=repo),\
             patch.object(route,'provider_session',forbidden),patch.object(route,'JevService',forbidden),patch.object(route,'LlmService',forbidden),\
             patch.object(route,'AmazonStreamingTranscription',transcription_stream(transcribe_audio)),patch.object(route.settings,'openrouter_api_key','test-key'):
            await asyncio.wait_for(route.meeting_stream(ws),3)
        session=next(row for row in repo.rows.values() if row.get('sessionId'))
        self.assertEqual(session['state'],'ready')
        self.assertEqual(session['roomId'], room)
        self.assertEqual([line.split('] ',1)[1] for line in transcript(repo,session).splitlines()],['first','second'])
        self.assertTrue(any(message['type']=='meeting_segment_ended' and message['ack']==6400 for message in ws.sent))
        recovery=db.rows['USER#A#MEETING#meeting-1']
        checkpoint=await db.decrypt('A','meeting-state',recovery['cipher'])
        self.assertEqual(checkpoint['transcript'],'');self.assertEqual(checkpoint['context'],{})
        saved=repo.load_checkpoint(session,'meeting-1');self.assertEqual(saved['ack'],6400)
        self.assertEqual(saved['transcript'],'first second')

class AdditionalMomTests(unittest.TestCase):
    def test_recovering_one_capture_preserves_other_missing_audio(self):
        repo=MemoryRepository();repo.set_preferences('A','both','UTC','verified-A')
        first=repo.begin('A','abc-defg-hij','first')
        repo.detach(first,incomplete=True,capture='first')
        second=repo.begin('A','abc-defg-hij','second')
        repo.detach(second,incomplete=False,capture='second')
        row=repo.get(first['pk'])
        self.assertTrue(row['incomplete']);self.assertEqual(row['incompleteCaptures'],['first'])


    def test_concurrent_room_creation_cannot_replace_a_new_active_pointer(self):
        from concurrent.futures import ThreadPoolExecutor
        repo=MemoryRepository();repo.set_preferences('A','both','UTC','verified-A')
        def join(capture):
            try:return repo.begin('A','abc-defg-hij',capture)
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(join,['first','second']))
        accepted=[row for row in results if row]
        self.assertEqual(len(accepted),1)
        pointer=repo.get('ROOM#A#abc-defg-hij')
        self.assertEqual(pointer['sessionKey'],accepted[0]['pk'])
        self.assertEqual(len([key for key in repo.rows if key.startswith('MOM#')]),1)

    def test_disabled_cognito_user_is_not_a_recipient(self):
        repo=MemoryRepository();repo.set_preferences('A','both','UTC','verified-A')
        row=repo.begin('A','abc-defg-hij','capture')
        cognito=SimpleNamespace(admin_get_user=lambda **args:{'Enabled':False,'UserAttributes':[
            {'Name':'sub','Value':'A'},{'Name':'email','Value':'verified@example.com'},{'Name':'email_verified','Value':'true'}]})
        with self.assertRaises(MomError):verified_email(cognito,'pool',row)


class MinutesEmailTests(unittest.TestCase):
    def test_html_escapes_model_text_and_uses_only_simple_inline_presentation(self):
        from app.mom.email_template import render_minutes
        row={'startedAt':1700000000,'timezone':'Asia/Kolkata'}
        content=render_minutes('Key points\n• <script>alert("x")</script> & details\n\nNext steps:\n- Omar sends after approval.',row)
        self.assertIn('&lt;script&gt;',content);self.assertNotIn('<script>',content)
        self.assertIn('Omar sends after approval.',content)
        self.assertIn('role="presentation"',content)
        self.assertNotIn('<style',content);self.assertNotIn('<img',content)
        self.assertNotIn('display:flex',content);self.assertNotIn('display:grid',content)

    def test_successful_send_includes_html_and_plain_text_fallback(self):
        repo=MemoryRepository();repo.set_preferences('A','both','UTC','verified-A')
        row=repo.begin('A','abc-defg-hij','capture');repo.append(row,'capture',1,[{'text':'Send after approval.'}]);repo.detach(row,finish=True)
        ses=Ses();processor=Processor(repo,summarize=lambda text,row:'Next steps\n• Send after approval.',
            resolve_email=lambda row:'verified@example.com',ses=ses,sender='no-reply@example.com',configuration_set='mom')
        processor.process(row['pk'])
        body=ses.calls[0]['Content']['Simple']['Body']
        self.assertIn('Send after approval.',body['Text']['Data'])
        self.assertIn('<h2',body['Html']['Data']);self.assertIn('Next steps',body['Html']['Data'])
        self.assertFalse(repo.s3.objects)

class ExpiredOwnerTests(unittest.TestCase):
    def test_expired_owner_cannot_revive_session_or_extend_grace(self):
        repo=MemoryRepository();repo.set_preferences('A','both','UTC','verified-A')
        row=repo.begin('A','abc-defg-hij','capture')
        deadline=row['finalizeAfter'];repo.now[0]+=91
        with self.assertRaises(Conflict):repo.touch(row)
        with self.assertRaises(Conflict):repo.append(row,'capture',1,[{'text':'late write'}])
        repo.detach(row)
        self.assertEqual(repo.get(row['pk'])['finalizeAfter'],deadline)
        self.assertFalse(repo.s3.objects)

class DynamoNumericBoundaryTests(unittest.TestCase):
    def repository(self):
        from boto3.dynamodb.types import TypeSerializer
        from app.mom.repository import encode
        repo = MemoryRepository()
        # Exercise production get(), including boto3's Decimal deserialization.
        repo.dynamo.get_item = lambda **args: {'Item': {
            k: TypeSerializer().serialize(encode(v))
            for k, v in repo.rows.get(args['Key']['pk']['S'], {}).items()}}
        repo.get = lambda key: Repository.get(repo, key)
        repo.clock = lambda: repo.now[0] + 0.25
        repo.set_preferences('A', 'mom_only', 'Asia/Kolkata', 'verified-A')
        return repo

    def test_production_numeric_round_trip_allows_checkpoint_and_audio_ack(self):
        repo = self.repository()
        attachment = repo.begin('A', 'abc-defg-hij', 'capture-1')
        restored = repo.get(attachment['pk'])
        self.assertIsInstance(restored['startedAt'], int)
        self.assertIsInstance(restored['version'], int)
        repo.save_checkpoint(attachment, 'capture-1', {'ack': 0, 'timeline': {'items': []}})
        repo.append(attachment, 'capture-1', 1, [{'text': 'Synthetic follow-up', 'start': 0, 'end': 3}])
        self.assertEqual(repo.load_checkpoint(attachment, 'capture-1')['ack'], 0)
        self.assertIn('Synthetic follow-up', transcript(repo, repo.get(attachment['pk'])))

    def test_deserialized_session_dates_can_render_email_without_sending_it(self):
        from app.mom.email_template import render_minutes
        repo = self.repository()
        row = repo.begin('A', 'abc-defg-hij', 'capture-1')
        html = render_minutes('Key points\n• Synthetic follow-up', repo.get(row['pk']))
        self.assertIn('Asia/Kolkata', html)
        self.assertIn('Synthetic follow-up', html)

    def test_numeric_round_trip_preserves_fences_and_session_expiry(self):
        repo = self.repository()
        row = repo.begin('A', 'abc-defg-hij', 'capture-1')
        changed_owner = {**row, 'owner': 'old-owner'}
        with self.assertRaises(Conflict): repo.touch(changed_owner)
        repo.rows[row['pk']]['startedAt'] = repo.now[0] - repo.max_session
        with self.assertRaises(MomError) as exc: repo.touch(row)
        self.assertEqual(exc.exception.code, 'MI-MOM-007')
