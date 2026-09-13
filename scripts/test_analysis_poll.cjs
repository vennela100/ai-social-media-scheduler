// Exercise the actual template poll function with controlled fetch/timeouts.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/video_detail.html', 'utf8');
const begin = html.indexOf('          async function poll(retry)');
const end = html.indexOf('          analysisPromise = poll(true)', begin);
assert(begin >= 0 && end > begin);
const source = html.slice(begin, end);

async function run(responses) {
  let timeout;
  let observed;
  const requests = [];
  const sandbox = {
    AbortController, Promise, Error, Date,
    deadline: Date.now() + 600000,
    grid: {dataset: {analyzeUrl: '/video/1/analyze/', analysisStatus: 'pending'}},
    csrftoken: 'test', analysisStatus: 'pending',
    showAnalysis(value) { observed = value; },
    setTimeout(fn, ms) {
      if (ms === 120000) timeout = fn;
      else queueMicrotask(fn);
      return 1;
    },
    clearTimeout() {},
    async fetch(url, options) {
      requests.push(options.body);
      assert(responses.length, 'unexpected extra poll');
      const next = responses.shift();
      if (next === 'abort') {
        timeout();
        // Browsers can reject with TypeError rather than AbortError.
        throw new TypeError('signal is aborted without reason');
      }
      return {ok: next.status !== 403, status: next.status || 200, json: async () => next};
    },
  };
  vm.runInNewContext(source + '\nthis.start = poll;', sandbox);
  let failure;
  try { await sandbox.start(true); } catch (error) { failure = error; }
  return {requests, observed, failure};
}

(async () => {
  const done = {ok: true, status: 'done', analysis: 'Entire video observed.'};
  const resumed = await run(['abort', {ok: true, status: 'processing'}, done]);
  assert.equal(resumed.failure, undefined);
  assert.equal(resumed.observed, done.analysis);
  assert.deepEqual(resumed.requests, ['retry=1', '', '']);
  const failed = await run(['abort', {ok: false, error: 'Quota reached', error_code: 'daily_quota'}]);
  assert.equal(failed.failure.message, 'Quota reached');
  assert.equal(failed.failure.code, 'daily_quota');
  assert.deepEqual(failed.requests, ['retry=1', '']);
  const session = await run([{status: 403}]);
  assert.match(session.failure.message, /session expired/);
  assert.equal(session.requests.length, 1);
  console.log('PASS: timeout recovery, no automatic analysis restarts, quota propagation, session expiry.');
})().catch(error => { console.error(error); process.exitCode = 1; });
