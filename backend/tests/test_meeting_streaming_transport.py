import asyncio
import importlib
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from app.core.auth import Principal, issue_ticket
from app.services.streaming_stt import TranscriptUpdate
from test_auth_integration import MemoryStore
from test_meeting_stream_timeline import scoped_provider, FakeDrive, FakeJev, FakeLlm

route=importlib.import_module('app.api.routes.meeting_stream')


class Stream:
    instances=[]
    def __init__(self,**kwargs):
        self.frames=[];self.closed=False;self.queue=asyncio.Queue();self.audio_seconds=0
        self.instances.append(self)
    async def start(self):return self
    async def send(self,pcm):
        self.frames.append(pcm);self.audio_seconds+=len(pcm)/32000
        if len(self.frames)==1:await self.queue.put(TranscriptUpdate('one','Please send',0,.2,False))
    async def finish(self):
        if self.closed:return
        self.closed=True
        if self.frames:await self.queue.put(TranscriptUpdate('one','Please send the launch plan to Maya.',0,self.audio_seconds,True))
        await self.queue.put(None)
    async def updates(self):
        while (event:=await self.queue.get()) is not None:yield event


class Socket:
    def __init__(self,*,size=6400,bad_ticket=False,finish=False,wait_for_cards=False):
        self.app=NS(state=NS(users=MemoryStore()))
        self.app.state.users.test_drive=FakeDrive()
        self.sent=[];self.queue=asyncio.Queue();self.size=size;self.bad_ticket=bad_ticket
        self.done=asyncio.Event();self.closed=None
        self.finish=finish
        self.wait_for_cards=wait_for_cards
    async def accept(self):pass
    async def receive_text(self):
        ticket=await issue_ticket(Principal('A','family-A',int(time.time())+900),self.app.state.users)
        return json.dumps({'ticket':'bad' if self.bad_ticket else ticket,'meetingId':'streaming-test',
            'lastAck':0,'audioFormat':'pcm_s16le_16000','roomId':'abc-defg-hij'})
    async def receive(self):return await self.queue.get()
    async def close(self,**kwargs):self.closed=kwargs;self.done.set()
    async def send_json(self,message):
        self.sent.append(message)
        if message['type']=='auth_ready':
            for sequence in range(1,7):
                await self.queue.put({'text':json.dumps({'type':'audio','sequence':sequence})})
                await self.queue.put({'bytes':bytes(self.size)})
            if not self.wait_for_cards:
                await self.queue.put({'text':json.dumps({'type':'finish' if self.finish else 'end','lastSequence':6})})
        if self.wait_for_cards and message['type']=='timeline_snapshot' and message['revision']>=2:
            await self.queue.put({'text':json.dumps({'type':'end','lastSequence':6})})
        if message['type']=='meeting_segment_ended':self.done.set()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_socket(self,socket,enabled=True,repo=None,stream_class=Stream,live=False):
        Stream.instances=[]
        with patch.object(route.settings,'streaming_stt_enabled',enabled), \
             patch.object(route.settings,'mom_enabled',False), \
             patch.object(route,'mom_available',return_value=repo is not None), \
             patch.object(route,'mom_repository',return_value=repo), \
             patch.object(route.settings,'openrouter_api_key','test-key' if live else ''), \
             patch.object(route,'JevService',FakeJev),patch.object(route,'LlmService',FakeLlm), \
             patch.object(route,'provider_session',scoped_provider), \
             patch.object(route,'AmazonStreamingTranscription',stream_class):
            await asyncio.wait_for(route.meeting_stream(socket),3)

    async def test_persistent_frames_final_drain_and_partial_is_not_saved(self):
        socket=Socket();await self.run_socket(socket)
        self.assertEqual(len(Stream.instances),1)
        self.assertEqual(len(Stream.instances[0].frames),6)
        self.assertTrue(Stream.instances[0].closed)
        self.assertTrue(any(m['type']=='transcription_partial' for m in socket.sent))
        self.assertEqual([m['transcript'] for m in socket.sent if m['type']=='transcription_state'],
            ['Please send the launch plan to Maya.'])
        ended=[m for m in socket.sent if m['type']=='meeting_segment_ended']
        self.assertEqual(ended,[{'type':'meeting_segment_ended','ack':6,'minutesFinalizing':False}])
        row=socket.app.state.users.rows['USER#A#MEETING#streaming-test']
        saved=await socket.app.state.users.decrypt('A','meeting-state',row['cipher'])
        self.assertEqual(saved['transcript'],'Please send the launch plan to Maya.')
        self.assertEqual(saved['transport'],'pcm_s16le_16000')
        self.assertAlmostEqual(saved['audioEnd'],1.2)

    async def test_disabled_transport_and_bad_auth_never_open_provider(self):
        for enabled,bad in [(False,False),(True,True)]:
            with self.subTest(enabled=enabled,bad=bad):
                socket=Socket(bad_ticket=bad);await self.run_socket(socket,enabled)
                self.assertEqual(Stream.instances,[])
                self.assertIsNotNone(socket.closed)

    async def test_oversized_pcm_is_rejected_and_provider_closed(self):
        socket=Socket(size=6402);await self.run_socket(socket)
        self.assertEqual(Stream.instances[0].frames,[])
        self.assertTrue(Stream.instances[0].closed)
        self.assertEqual(socket.closed['reason'],'MI-STT-001')

    async def test_stream_receipt_does_not_allow_legacy_replay(self):
        socket=Socket();await self.run_socket(socket)
        socket.sent=[];socket.done=asyncio.Event()
        await self.run_socket(socket)
        self.assertEqual(Stream.instances,[])
        self.assertEqual(socket.closed['code'],4409)

    async def test_recap_finishes_after_provider_final_and_excludes_partial_text(self):
        from test_mom import MemoryRepository,transcript
        repo=MemoryRepository();repo.set_preferences('A','mom_only','UTC','verified-A')
        socket=Socket(finish=True)
        await self.run_socket(socket,repo=repo)
        row=next(v for k,v in repo.rows.items() if k.startswith('MOM#'))
        text=transcript(repo,row)
        self.assertEqual(text.count('Please send'),1)
        self.assertIn('Please send the launch plan to Maya.',text)
        self.assertEqual(row['state'],'ready')
        self.assertFalse(row.get('incomplete',False))
        self.assertTrue(any(m.get('minutesFinalizing') is True for m in socket.sent))

    async def test_final_streamed_speech_drives_existing_live_task_and_search_pipeline(self):
        class LiveStream(Stream):
            async def send(self,pcm):
                await super().send(pcm)
                if len(self.frames)==6:
                    await self.queue.put(TranscriptUpdate('one','Please send the launch plan to Maya.',0,self.audio_seconds,True))
            async def finish(self):
                if not self.closed:self.closed=True;await self.queue.put(None)
        socket=Socket(wait_for_cards=True)
        await self.run_socket(socket,stream_class=LiveStream,live=True)
        cards=[m for m in socket.sent if m['type']=='timeline_snapshot'][-1]['items']
        self.assertEqual({c['kind'] for c in cards},{'task_suggestion','search_results'})
        self.assertTrue(any(m['type']=='meeting_segment_ended' for m in socket.sent))


if __name__=='__main__':unittest.main()
