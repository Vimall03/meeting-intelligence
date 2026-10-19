import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function worklet() {
  let Processor;
  const frames=[];
  class Base {port={postMessage(message){frames.push(message);}};}
  vm.runInNewContext(fs.readFileSync(new URL('../lib/pcm-worklet.js',import.meta.url),'utf8'),{
    AudioWorkletProcessor:Base,registerProcessor(name,value){assert.equal(name,'meeting-pcm');Processor=value;},
  });
  return {processor:new Processor(),frames};
}

test('one continuous worklet emits 200 ms PCM frames and flushes the final sub-frame',()=>{
  const {processor,frames}=worklet();
  const output=new Float32Array(128).fill(1);
  for(let n=0;n<26;n++)assert.equal(processor.process([[new Float32Array(128).fill(.5)]],[[output]]),true);
  assert.ok(output.every(v=>v===0),'mixed microphone audio must not play back');
  assert.equal(frames.length,1);
  assert.equal(frames[0].pcm.byteLength,6400);
  assert.equal(new Int16Array(frames[0].pcm)[0],16384);
  processor.port.onmessage({data:'finish'});
  assert.equal(frames[1].pcm.byteLength,256);
  assert.equal(frames[2].finished,true);
  assert.equal(processor.process([],[]),false);
});

test('PCM clips safely at both bounds without wrapping loud audio',()=>{
  const {processor,frames}=worklet();
  processor.process([[Float32Array.from([-2,-1,0,1,2])]],[]);
  processor.port.onmessage({data:'finish'});
  assert.deepEqual(Array.from(new Int16Array(frames[0].pcm)),[-32768,-32768,0,32767,32767]);
});
