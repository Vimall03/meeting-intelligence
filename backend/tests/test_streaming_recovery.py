import asyncio
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from fastapi import WebSocketDisconnect
from app.core.auth import Principal, issue_ticket
from app.services.streaming_stt import TranscriptUpdate
from test_auth_integration import MemoryStore
from test_meeting_stream_timeline import route, scoped_provider, FakeDrive
from streaming_fakes import transcription_stream, DefaultStream


def audio(start, pcm):
    return [{'text': json.dumps({'type': 'audio', 'startSample': start})}, {'bytes': pcm}]


class Socket:
    def __init__(self, db, messages=(), *, last_ack=0, break_on=None, room=False):
        self.app=NS(state=NS(users=db));self.sent=[];self.queue=asyncio.Queue();self.messages=messages
        self.last_ack=last_ack;self.break_on=break_on;self.room=room;self.closed=None
    async def accept(self):pass
    async def receive_text(self):
        principal=Principal('A','family-A',int(time.time())+900)
        return json.dumps({'ticket':await issue_ticket(principal,self.app.state.users),
            'meetingId':'recovery','audioFormat':'pcm_s16le_16000_v2','lastAck':self.last_ack,
            **({'roomId':'abc-defg-hij'} if self.room else {})})
    async def receive(self):return await self.queue.get()
    async def close(self,**kwargs):self.closed=kwargs
    async def send_json(self,message):
        if message['type']==self.break_on:
            raise WebSocketDisconnect()
        self.sent.append(message)
        if message['type']=='auth_ready':
            for control in self.messages:await self.queue.put(control)


class StreamingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.db=MemoryStore();self.db.test_drive=FakeDrive()
    async def run_socket(self,ws,provider=DefaultStream,repo=None):
        with patch.object(route,'provider_session',scoped_provider),patch.object(route.settings,'openrouter_api_key',''),\
             patch.object(route,'AmazonStreamingTranscription',provider),patch.object(route,'mom_available',return_value=repo is not None),\
             patch.object(route,'mom_repository',return_value=repo):
            await asyncio.wait_for(route.meeting_stream(ws),3)
    async def saved(self):
        row=self.db.rows['USER#A#MEETING#recovery']
        return await self.db.decrypt('A','meeting-state',row['cipher'])

    async def test_lost_ack_reconnect_skips_already_committed_audio(self):
        first=Socket(self.db,audio(0,bytes(6400)),break_on='audio_ack')
        await self.run_socket(first)
        self.assertEqual((await self.saved())['ack'],3200)
        calls=[]
        async def speech(pcm):
            calls.append(pcm)
            return {'audio_duration':.2,'segments':[{'start':0,'end':.2,'text':'Next topic.'}]}
        second=Socket(self.db,audio(0,bytes(6400))+audio(3200,bytes([1])*6400)+[
            {'text':json.dumps({'type':'end','lastSequence':6400})}])
        await self.run_socket(second,transcription_stream(speech))
        self.assertEqual(next(m for m in second.sent if m['type']=='auth_ready')['ack'],3200)
        self.assertEqual(calls,[bytes([1])*6400])
        state=await self.saved();self.assertEqual(state['ack'],6400)
        self.assertEqual(state['transcript'].count('Please send'),1)
        self.assertIn('Next topic.',state['transcript'])

    async def test_mid_frame_commit_replays_exact_suffix_and_keeps_absolute_time(self):
        original=bytes([1])*4800+bytes([2])*1600
        class PartialStream(DefaultStream):
            async def send(self,pcm):
                self.audio_seconds+=len(pcm)/32000
                await self.events.put(TranscriptUpdate('one','First sentence.',0,.15,True))
                await self.events.put(TranscriptUpdate('next','unsaved preview',.15,.2,False))
        first=Socket(self.db,audio(0,original),break_on='transcription_partial')
        await self.run_socket(first,PartialStream)
        self.assertEqual((await self.saved())['ack'],2400)
        calls=[]
        async def speech(pcm):
            calls.append(pcm)
            return {'audio_duration':len(pcm)/32000,'segments':[] if len(calls)==1 else [
                {'start':0,'end':.2,'text':'Second sentence.'}]}
        second=Socket(self.db,audio(2400,original[4800:])+audio(3200,bytes([3])*6400)+[
            {'text':json.dumps({'type':'end','lastSequence':6400})}],last_ack=2400)
        await self.run_socket(second,transcription_stream(speech))
        self.assertEqual(calls,[original[4800:],bytes([3])*6400])
        state=await self.saved()
        self.assertEqual(state['transcript'],'First sentence. Second sentence.')
        self.assertEqual(state['ack'],6400)
        self.assertAlmostEqual(state['context']['segments'][1]['start'],.2)
        self.assertAlmostEqual(state['context']['segments'][1]['end'],.4)

    async def test_recap_outbox_survives_append_failure_without_duplicate_sentences(self):
        from test_mom import MemoryRepository, transcript
        repo=MemoryRepository();repo.set_preferences('A','mom_only','UTC','verified-A')
        original=repo.append
        def fail_after_append(*args):
            original(*args)
            raise RuntimeError('write response lost')
        first=Socket(self.db,audio(0,bytes(6400)),room=True)
        with patch.object(repo,'append',fail_after_append):await self.run_socket(first,repo=repo)
        second=Socket(self.db,[{'text':json.dumps({'type':'finish','lastSequence':3200})}],room=True)
        await self.run_socket(second,repo=repo)
        row=next(v for k,v in repo.rows.items() if k.startswith('MOM#'))
        self.assertEqual(transcript(repo,row).count('Please send'),1)
        self.assertEqual(row['state'],'ready')
        self.assertFalse(row['incomplete'])

    async def test_failed_checkpoint_never_acknowledges_uncommitted_words(self):
        ws=Socket(self.db,audio(0,bytes(6400)))
        original=self.db.encrypt
        async def fail_changed(user,purpose,state):
            if state.get('ack',0)>0:raise RuntimeError('storage unavailable')
            return await original(user,purpose,state)
        with patch.object(self.db,'encrypt',fail_changed):await self.run_socket(ws)
        self.assertFalse(any(m['type']=='audio_ack' for m in ws.sent))
        self.assertEqual((await self.saved())['ack'],0)

    async def test_raw_frames_do_not_trigger_per_frame_encryption(self):
        from unittest.mock import AsyncMock
        from test_meeting_streaming_transport import Stream
        frames=[]
        for n in range(12):frames+=audio(n*3200,bytes(6400))
        frames+=[{'text':json.dumps({'type':'end','lastSequence':38400})}]
        ws=Socket(self.db,frames)
        encryption=AsyncMock(wraps=self.db.encrypt)
        with patch.object(self.db,'encrypt',encryption):await self.run_socket(ws,Stream)
        self.assertEqual(encryption.await_count,2)  # initial state and one final result
        self.assertEqual((await self.saved())['ack'],38400)

    async def test_audio_gap_is_rejected_before_transcription(self):
        gap=Socket(self.db,audio(3200,bytes(6400)));await self.run_socket(gap)
        self.assertEqual(gap.closed['code'],4401)
        self.assertEqual((await self.saved())['ack'],0)

    async def test_old_audio_protocol_never_opens_a_paid_stream(self):
        class OldSocket(Socket):
            async def receive_text(self):
                payload=json.loads(await super().receive_text())
                payload.pop('audioFormat')
                return json.dumps(payload)
        def forbidden(**kwargs):raise AssertionError('Paid stream started for obsolete audio')
        ws=OldSocket(self.db);await self.run_socket(ws,forbidden)
        self.assertEqual(ws.closed['reason'],'MI-STT-001')


if __name__=='__main__':unittest.main()
