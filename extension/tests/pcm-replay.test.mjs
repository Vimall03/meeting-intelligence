import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import * as messages from '../lib/user-messages.ts';
const exports = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(new URL('../lib/pcm-replay.ts',import.meta.url),'utf8'), {
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}
}).outputText,{exports,require:()=>messages});
const {PcmReplay}=exports;

test('durable mid-frame acknowledgment retains exactly the unfinished samples',async()=>{
  const replay=new PcmReplay();
  const samples=Int16Array.from({length:3200},(_,i)=>i);
  replay.append(new Blob([samples]));replay.append(new Blob([samples]));
  assert.equal(replay.acknowledge(2400),true);
  assert.equal(replay.frames.length,2);assert.equal(replay.frames[0].start,2400);
  assert.equal(replay.frames[0].end,3200);assert.equal(replay.frames[0].data.size,1600);
  const suffix=new Int16Array(await replay.frames[0].data.arrayBuffer());
  assert.deepEqual([...suffix],[...samples.slice(2400)]);
  assert.equal(replay.acknowledge(6400),true);assert.equal(replay.frames.length,0);
});

test('invalid and stale acknowledgments cannot erase uncommitted audio',()=>{
  const replay=new PcmReplay();replay.append(new Blob([new Uint8Array(6400)]));
  replay.acknowledge(100);
  for(const cursor of [99,3201,-1,NaN,Infinity,1.5,'3200',null])assert.equal(replay.acknowledge(cursor),false);
  assert.equal(replay.frames[0].start,100);assert.equal(replay.end,3200);
});

test('outages retain at most ninety seconds and reject malformed frames',()=>{
  const replay=new PcmReplay();const frame=new Blob([new Uint8Array(6400)]);
  for(let n=0;n<450;n++)replay.append(frame);
  assert.equal(replay.end,90*16000);
  assert.throws(()=>replay.append(frame),/MI-CON-005/);
  for(const size of [0,1,6402])assert.throws(()=>new PcmReplay().append(new Blob([new Uint8Array(size)])),/MI-CON-005/);
});
