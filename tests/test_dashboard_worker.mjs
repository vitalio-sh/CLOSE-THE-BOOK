import assert from 'node:assert/strict';
import test from 'node:test';
import {createWorker} from '../dashboard/worker.mjs';

const secret = 'test-only-publisher-secret';
function snapshot(id = 'run-1', started = '2026-11-07T09:00:00Z', phase = 'live') {
  return {schema_version: 1, pipeline: {phase, run_id: id, started_at: started, sha256: 'a'.repeat(64),
    hash_verified: true, source_changed_during_run: false, official_structure: {ok: true},
    validation: {ok: true, transactions: 1, covered: 1, missing: 0, rows: 1}},
  summary: {transactions: 1, submission_transactions: 1, submission_rows: 1,
    outcomes: {matched: 1, categorized: 0, unidentified: 0},
    currencies: [{currency: 'EUR', transactions: 1, inflow: '1.00', outflow: '0.00', net: '1.00'}],
    exceptions: [], categories: [], modules: []},
  export_checks: {missing_submission_transactions: 0, submission_ids_absent_from_bank: 0,
    audit_ids_missing: 0, module_counts_match_report: true},
  sources: {submission: {path: 'outputs/test.csv.gz'}}, definitions: {module_descriptions: {}},
  sample: {size: 1, population: 1}, transactions: [{id: 'B1', date: '2026-11-07', entity: 'E1', currency: 'EUR',
    bank_amount: '1.00', module: 'matching', outcome: 'matched', refs: {}, exception_codes: [],
    allocations: [{item_id: 'I1', amount_applied: '1.00'}]}]};
}

class Bucket {
  current = null;
  writes = 0;
  fails = false;
  conflict = false;
  async head() {
    if (this.fails) throw Error('Storage outage');
    return this.current ? {httpEtag: this.current.httpEtag, customMetadata: {...this.current.customMetadata}} : null;
  }
  async get() {
    const info = await this.head();
    return info ? {...info, body: this.current.body} : null;
  }
  async put(key, body, options) {
    if (this.fails) throw Error('Storage outage');
    assert.equal(key, 'dashboard/latest.json');
    assert.ok(options.onlyIf instanceof Headers);
    // Yield before the atomic comparison, letting concurrent readers share an old version.
    await new Promise(resolve => setTimeout(resolve, 1));
    if (this.conflict) return null;
    const match = options.onlyIf.get('if-match'), absent = options.onlyIf.get('if-none-match');
    if ((absent === '*' && this.current) || (match && this.current?.httpEtag !== match)) return null;
    this.current = {body, httpEtag: `"etag-${++this.writes}"`, customMetadata: {...options.customMetadata}};
    return this.current;
  }
}

function fixture() {
  const bucket = new Bucket();
  const seed = snapshot('seed', '2026-11-07T08:00:00Z');
  const worker = createWorker({'/index.html': '<html>Dashboard</html>', '/app.js': '/* app */', '/style.css': 'body{}'}, seed);
  const env = {BUCKET: bucket, DASHBOARD_PUBLISH_TOKEN: secret};
  const request = (path, options = {}) => worker.fetch(new Request(`https://dashboard.example${path}`, options), env);
  const publish = (data, extra = {}) => request('/api/snapshot', {method: 'POST', headers: {
    'content-type': 'application/json', authorization: `Bearer ${secret}`, ...extra}, body: JSON.stringify(data)});
  return {bucket, seed, request, publish, worker, env};
}

test('seed fallback, static allowlist and read-only public access', async () => {
  const f = fixture();
  assert.equal((await f.request('/')).status, 200);
  const fallback = await (await f.request('/data.json')).json();
  assert.equal(fallback.pipeline.run_id, 'seed');
  assert.equal(fallback.hosting.auto_refresh, true);
  for (const path of ['/worker.mjs', '/site.json', '/outputs/', '/api/snapshot']) assert.equal((await f.request(path)).status, 404);
  assert.equal((await f.request('/data.json', {method: 'DELETE'})).status, 405);
  assert.equal((await f.request('/data.json', {method: 'HEAD'})).status, 200);
});

test('authentication rejects bad and missing secrets without writes', async () => {
  const f = fixture();
  for (const token of ['', 'Bearer wrong', secret]) {
    assert.equal((await f.publish(snapshot(), {authorization: token})).status, 401);
  }
  delete f.env.DASHBOARD_PUBLISH_TOKEN;
  assert.equal((await f.publish(snapshot())).status, 401);
  assert.equal(f.bucket.writes, 0);
});

test('publishing embedded seed bootstraps durable R2 object', async () => {
  const f = fixture();
  assert.equal((await f.publish(f.seed)).status, 201);
  assert.equal(f.bucket.writes, 1);
  assert.equal((await f.publish(f.seed)).status, 200);
  assert.equal(f.bucket.writes, 1);
});

test('publish, public version, snapshot consistency and idempotent retry', async () => {
  const f = fixture(), input = snapshot();
  const first = await f.publish(input);
  assert.equal(first.status, 201);
  const published = await first.json();
  assert.equal(published.status, 'published');
  assert.equal(published.run_id, input.pipeline.run_id);
  const versionResponse = await f.request('/api/version');
  assert.equal(versionResponse.headers.get('cache-control'), 'no-store');
  const version = await versionResponse.json();
  const data = await (await f.request('/data.json')).json();
  assert.equal(data.pipeline.run_id, version.run_id);
  assert.equal(data.pipeline.sha256, version.hash);
  assert.equal(data.hosting.published_at, version.published_at);
  const retry = await f.publish(input);
  assert.equal(retry.status, 200);
  assert.equal((await retry.json()).status, 'unchanged');
  assert.equal(f.bucket.writes, 1);
  input.pipeline.sha256 = 'b'.repeat(64);
  assert.equal((await f.publish(input)).status, 409);
});

test('old runs rejected and FINAL permanently outranks LIVE', async () => {
  const f = fixture();
  assert.equal((await f.publish(snapshot('new', '2026-11-07T10:00:00Z'))).status, 201);
  assert.equal((await f.publish(snapshot('old', '2026-11-07T09:00:00Z'))).status, 409);
  assert.equal((await f.publish(snapshot('tie', '2026-11-07T10:00:00Z'))).status, 409);
  assert.equal((await f.publish(snapshot('final', '2026-11-07T09:00:00Z', 'final'))).status, 201);
  assert.equal((await f.publish(snapshot('later-live', '2026-11-07T12:00:00Z'))).status, 409);
  assert.equal((await (await f.request('/api/version')).json()).run_id, 'final');
});

test('invalid snapshots preserve last good publication', async () => {
  const f = fixture();
  await f.publish(snapshot());
  const mutations = [d => d.pipeline.hash_verified = false, d => d.pipeline.source_changed_during_run = true,
    d => d.pipeline.validation.covered = 0, d => d.pipeline.official_structure.ok = false,
    d => d.pipeline.started_at = 'yesterday', d => d.pipeline.phase = 'train',
    d => d.summary.transactions = 0, d => d.export_checks.audit_ids_missing = 1,
    d => d.summary.outcomes.matched = 2, d => d.transactions[0].allocations = null,
    d => d.sample.population = 2, d => d.summary.currencies[0].net = 'NaN'];
  for (const change of mutations) {
    const invalid = snapshot('invalid', '2026-11-07T11:00:00Z');
    change(invalid);
    assert.equal((await f.publish(invalid)).status, 422);
  }
  assert.equal(f.bucket.writes, 1);
  assert.equal((await (await f.request('/api/version')).json()).run_id, 'run-1');
});

test('actual streamed body size bounded despite no or misleading Content-Length', async () => {
  const f = fixture();
  const huge = snapshot(); huge.padding = 'x'.repeat(2 * 1024 * 1024);
  assert.equal((await f.publish(huge)).status, 413);
  assert.equal((await f.publish(huge, {'content-length': '1'})).status, 413);
  const invalid = await f.request('/api/snapshot', {method: 'POST', headers: {
    authorization: `Bearer ${secret}`, 'content-type': 'application/json'}, body: '{bad'});
  assert.equal(invalid.status, 400);
  assert.equal(f.bucket.writes, 0);
});

test('concurrent older and newer publishers converge on newer snapshot', async () => {
  const f = fixture();
  const results = await Promise.all([
    f.publish(snapshot('older', '2026-11-07T10:00:00Z')),
    f.publish(snapshot('newer', '2026-11-07T11:00:00Z')),
  ]);
  assert.ok(results.every(r => [201, 409].includes(r.status)));
  assert.equal((await (await f.request('/api/version')).json()).run_id, 'newer');
  // The same atomic replacement path also protects an already-existing object.
  await Promise.all([f.publish(snapshot('third', '2026-11-07T12:00:00Z')),
    f.publish(snapshot('fourth', '2026-11-07T13:00:00Z'))]);
  assert.equal((await (await f.request('/api/version')).json()).run_id, 'fourth');
});

test('storage errors never return seed and bounded conflicts preserve current object', async () => {
  const f = fixture();
  await f.publish(snapshot());
  f.bucket.fails = true;
  for (const path of ['/data.json', '/api/version']) assert.equal((await f.request(path)).status, 503);
  assert.equal((await f.publish(snapshot('new', '2026-11-07T10:00:00Z'))).status, 503);
  f.bucket.fails = false; f.bucket.conflict = true;
  assert.equal((await f.publish(snapshot('new', '2026-11-07T10:00:00Z'))).status, 409);
  assert.equal(f.bucket.writes, 1);
});
