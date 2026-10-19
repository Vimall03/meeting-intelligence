"""Create a deterministic local benchmark corpus, without private recordings.

Requires macOS `say` for synthetic voices and FFmpeg. Four public human-speech
clips come from LibriSpeech test-clean (CC BY 4.0); downloads and generated voice
files belong outside the repository. The manifest records provenance + hashes.
"""
import argparse
import hashlib
import json
import subprocess
import urllib.request
import wave
from pathlib import Path

import numpy as np


CASES = [
    ('plain-us', 'synthetic_clean', 'Samantha (English (US))', [
        'Please send the revised launch plan to Maya before Friday afternoon.',
        'The design is approved, but the payment tests still need an owner.',
        'We will review the results together at the next meeting.']),
    ('technical-uk', 'synthetic_clean', 'Daniel', [
        'The authentication callback fails when the session token expires.',
        'Add exponential backoff and an idempotency key before retrying the request.',
        'Keep the release blocked until the integration tests pass.']),
    ('numbers-india', 'synthetic_clean', 'Rishi', [
        'The budget is fourteen thousand dollars, not forty thousand dollars.',
        'We need seventeen licenses for the engineering team by November twenty first.',
        'Move the deadline from Tuesday to Thursday and keep the same owner.']),
    ('corrections-australia', 'synthetic_clean', 'Karen', [
        'Do not create another task for the checkout issue because it already exists.',
        'I said the client approved the design, not the deployment.',
        'Actually, hold the release until Priya confirms that the database migration is safe.']),
    ('technical-noise', 'synthetic_noisy', 'Daniel', [
        'The authentication callback fails when the session token expires.',
        'Add exponential backoff and an idempotency key before retrying the request.',
        'Keep the release blocked until the integration tests pass.']),
    ('alternating-voices', 'synthetic_clean', ['Rishi', 'Daniel', 'Karen'], [
        'Can we ship the updated dashboard on Friday?',
        'Only if the accessibility review is complete and the search issue is fixed.',
        'Then let us keep Monday as the delivery date and tell the client today.']),
]


def convert(source):
    return subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),
        '-ar','16000','-ac','1','-f','s16le','pipe:1'],capture_output=True,check=True).stdout


def voiced_range(pcm):
    audio = np.frombuffer(pcm,dtype='<i2')
    indices = np.flatnonzero(np.abs(audio.astype(float)) > 100)
    return (indices[0]/16000, (indices[-1]+1)/16000) if len(indices) else (0,len(audio)/16000)


def write_clip(folder, clip, pcm):
    name = clip['id']+'.wav'
    with wave.open(str(folder/name),'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(pcm)
    return {**clip,'audio':name,'sha256':hashlib.sha256((folder/name).read_bytes()).hexdigest(),
        'duration_s':len(pcm)/32000}


def main(folder):
    folder.mkdir(parents=True,exist_ok=True)
    clips=[]
    for case,category,voices,texts in CASES:
        pcm=bytes(16000);utterances=[]
        for index,text in enumerate(texts):
            voice=voices[index] if isinstance(voices,list) else voices
            temp=folder/'voice.aiff'
            subprocess.run(['say','-v',voice,'-r','165','-o',str(temp),text],check=True)
            speech=convert(temp);temp.unlink()
            start,end=voiced_range(speech)
            utterances.append({'text':text,'start':len(pcm)/32000+start,'end':len(pcm)/32000+end})
            pcm+=speech+bytes(25600)
        pcm+=bytes(32000)
        if category=='synthetic_noisy':
            audio=np.frombuffer(pcm,dtype='<i2').astype(float)
            rng=np.random.default_rng(42)
            rms=np.sqrt(np.mean(audio**2))
            audio+=rng.normal(0,rms/(10**(12/20)),len(audio))
            pcm=np.clip(audio,-32768,32767).astype('<i2').tobytes()
        clips.append(write_clip(folder,{'id':case,'category':category,'utterances':utterances,
            'voices':voices,'noise_snr_db':12 if category=='synthetic_noisy' else None},pcm))
    for offset in [0,40,120,300]:
        url=f'https://datasets-server.huggingface.co/rows?dataset=openslr%2Flibrispeech_asr&config=clean&split=test&offset={offset}&length=1'
        with urllib.request.urlopen(url,timeout=30) as response: row=json.load(response)['rows'][0]['row']
        with urllib.request.urlopen(row['audio'][0]['src'],timeout=30) as response: content=response.read()
        source=folder/'downloaded.audio';source.write_bytes(content)
        pcm=convert(source);source.unlink()
        start,end=voiced_range(pcm)
        pcm+=bytes(32000)
        clips.append(write_clip(folder,{'id':'librispeech-'+row['id'],'category':'human_read_speech',
            'utterances':[{'text':row['text'],'start':start,'end':end}],
            'source_id':row['id'],'speaker_id':row['speaker_id'],'license':'CC BY 4.0',
            'source':'https://www.openslr.org/12/'},pcm))
    manifest={'provenance':{'synthetic':'Locally generated macOS voices; no private meeting content.',
        'human':'LibriSpeech test-clean, CC BY 4.0, https://www.openslr.org/12/',
        'limitations':'Synthetic meeting scripts + human read speech; no real conversational overlap or live meeting capture.'},'clips':clips}
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'clips':len(clips),'seconds':sum(c['duration_s'] for c in clips),'manifest':str(folder/'manifest.json')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    main(parser.parse_args().output)
