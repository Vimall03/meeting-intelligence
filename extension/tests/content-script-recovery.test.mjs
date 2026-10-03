import assert from 'node:assert/strict';
import test from 'node:test';

import {
  meetContentScriptFiles,
  probeWithContentScriptRecovery,
} from '../lib/content-script-recovery.ts';

const manifest = {
  content_scripts: [{
    matches: ['https://meet.google.com/*'],
    js: ['content-scripts/meet.js'],
  }],
};

test('finds the generated Meet content script in the extension manifest', () => {
  assert.deepEqual(meetContentScriptFiles(manifest), ['content-scripts/meet.js']);
});

test('injects the current Meet script and retries a failed probe', async () => {
  let probeCount = 0;
  const injected = [];
  const result = await probeWithContentScriptRecovery({
    manifest,
    probe: async () => {
      probeCount += 1;
      if (probeCount === 1) throw new Error('Could not establish connection. Receiving end does not exist.');
      return { status: 'in-meeting' };
    },
    inject: async (files) => { injected.push(files); },
  });

  assert.deepEqual(result, { status: 'in-meeting' });
  assert.equal(probeCount, 2);
  assert.deepEqual(injected, [['content-scripts/meet.js']]);
});

test('does not inject when the first probe succeeds', async () => {
  let injected = false;
  const result = await probeWithContentScriptRecovery({
    manifest,
    probe: async () => ({ status: 'not-in-meeting' }),
    inject: async () => { injected = true; },
  });

  assert.deepEqual(result, { status: 'not-in-meeting' });
  assert.equal(injected, false);
});
