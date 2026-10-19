import asyncio
from types import SimpleNamespace as NS
import unittest

from app.services.streaming_stt import AmazonStreamingTranscription, StreamingTranscriptionError
from stt_compare import alignment, tokens, score, summarise


class Input:
    def __init__(self): self.audio=[]; self.ended=0
    async def send_audio_event(self, *, audio_chunk): self.audio.append(audio_chunk)
    async def end_stream(self): self.ended+=1


class Output:
    def __init__(self, events): self.events=events
    async def __aiter__(self):
        for event in self.events: yield NS(transcript=NS(results=[event]))


def event(id='a',text='Ship only after approval',partial=False):
    return NS(result_id=id, alternatives=[NS(transcript=text)], start_time=0,end_time=1,is_partial=partial)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def make(self,events=()):
        input=Input(); output=Output(events); arguments=[]
        class Client:
            async def start_stream_transcription(self,**kwargs):
                arguments.append(kwargs)
                return NS(input_stream=input,output_stream=output)
        session=await AmazonStreamingTranscription(client=Client()).start()
        return session,input,arguments

    async def test_one_connection_emits_replaceable_partials_and_each_final_once(self):
        session,input,args=await self.make([event(text='Ship',partial=True),event(),event(),event('b','Do not ship')])
        await session.send(bytes(6400));await session.send(bytes(6400))
        updates=[e async for e in session.updates()]
        self.assertEqual([e.final for e in updates],[False,True,True])
        self.assertEqual([e.text for e in updates if e.final],['Ship only after approval','Do not ship'])
        self.assertEqual(len(args),1);self.assertEqual(args[0]['media_encoding'],'pcm')
        self.assertEqual(session.audio_seconds,.4)
        await session.finish();await session.finish()
        self.assertEqual(input.ended,1)
        with self.assertRaises(StreamingTranscriptionError):await session.send(bytes(2))

    async def test_invalid_frames_are_rejected_before_provider_submission(self):
        session,input,_=await self.make()
        for value in [b'',b'x',bytes(6402)]:
            with self.assertRaises(StreamingTranscriptionError):await session.send(value)
        self.assertEqual(input.audio,[])
        await session.finish()

    async def test_provider_exception_is_replaced_by_stable_support_code(self):
        class Client:
            async def start_stream_transcription(self,**kwargs):raise RuntimeError('private token and URL')
        with self.assertRaises(StreamingTranscriptionError) as error:
            await AmazonStreamingTranscription(client=Client()).start()
        self.assertEqual(str(error.exception),'MI-STT-001')

    async def test_invalid_timestamps_do_not_reach_meeting_context(self):
        bad=event();bad.end_time=-1
        session,_,_=await self.make([bad])
        with self.assertRaises(StreamingTranscriptionError):[e async for e in session.updates()]
        await session.finish()


class ComparisonTests(unittest.TestCase):
    def test_wer_counts_omissions_and_hallucinations(self):
        stats,_=alignment(tokens('do not ship until Friday'),tokens('do ship until Monday please'))
        self.assertEqual(stats['deletions'],1)
        self.assertEqual(stats['substitutions'],1)
        self.assertEqual(stats['insertions'],1)
        self.assertEqual(stats['wer'],.6)

    def test_number_formatting_is_normalized_but_negation_is_preserved(self):
        self.assertEqual(tokens('14,000 dollars.'),tokens('fourteen thousand dollars'))
        self.assertEqual(tokens('$14,000'),tokens('fourteen thousand dollars'))
        self.assertEqual(tokens('$40,000.'),tokens('forty thousand dollars'))
        self.assertEqual(tokens('November 21st'),tokens('November twenty first'))
        self.assertEqual(tokens('Nov. 21'),tokens('November twenty first'))
        self.assertNotEqual(tokens('do ship'),tokens('do not ship'))

    def test_latency_includes_capture_wait_and_reports_missing_endings(self):
        clip={'utterances':[{'text':'send the plan','start':0,'end':1},
            {'text':'hold the release','start':2,'end':3}]}
        result=score(clip,[{'text':'send the plan','final':True,'received_s':4},
            {'text':'hold the meeting','final':True,'received_s':6}])
        self.assertEqual(result['final_utterance_lags_s'],[3])
        self.assertEqual(result['latency_coverage'],1)
        self.assertEqual(result['utterance_count'],2)


if __name__=='__main__':unittest.main()
