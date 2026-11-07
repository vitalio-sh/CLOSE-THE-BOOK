// Packaging appends: export default createWorker(ASSETS, SEED);
// R2 conditional writes: https://developers.cloudflare.com/r2/api/workers/workers-api-reference/
const SNAPSHOT_KEY = 'dashboard/latest.json';
const MAX_BYTES = 2 * 1024 * 1024;
const encoder = new TextEncoder();
const iso = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
const integer = value => Number.isSafeInteger(value) && value >= 0;
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string';
const money = value => text(value) && /^-?\d+(?:\.\d+)?$/.test(value);

function validSnapshot(d) {
  if (!record(d) || d.schema_version !== 1) return false;
  const p = d.pipeline, s = d.summary, c = d.export_checks, v = p?.validation;
  if (!record(p) || !['live', 'final'].includes(p.phase) || !iso(p.started_at) ||
      !text(p.run_id) || !/^[A-Za-z0-9_.:-]{1,128}$/.test(p.run_id) ||
      !/^[a-fA-F0-9]{64}$/.test(p.sha256 || '') || p.hash_verified !== true ||
      p.source_changed_during_run !== false || v?.ok !== true || p.official_structure?.ok !== true) return false;
  if (!record(s) || !integer(s.transactions) || s.transactions === 0 ||
      s.submission_transactions !== s.transactions || v.transactions !== s.transactions ||
      v.covered !== s.transactions || v.missing !== 0 || !integer(s.submission_rows) ||
      s.submission_rows < s.transactions || v.rows !== s.submission_rows) return false;
  if (!record(c) || c.missing_submission_transactions !== 0 || c.submission_ids_absent_from_bank !== 0 ||
      c.audit_ids_missing !== 0 || c.module_counts_match_report !== true) return false;
  if (!record(s.outcomes) || !['matched', 'categorized', 'unidentified'].every(k => integer(s.outcomes[k])) ||
      s.outcomes.matched + s.outcomes.categorized + s.outcomes.unidentified !== s.transactions) return false;
  if (!Array.isArray(s.currencies) || !s.currencies.length || !s.currencies.every(c => record(c) &&
      /^[A-Z]{3}$/.test(c.currency || '') && integer(c.transactions) && ['inflow', 'outflow', 'net'].every(k => money(c[k]))) ||
      s.currencies.reduce((n, c) => n + c.transactions, 0) !== s.transactions ||
      new Set(s.currencies.map(c => c.currency)).size !== s.currencies.length) return false;
  if (!Array.isArray(s.exceptions) || !Array.isArray(s.categories) || !Array.isArray(s.modules) ||
      !record(d.sources?.submission) || !text(d.sources.submission.path) ||
      !record(d.definitions?.module_descriptions)) return false;
  if (!record(d.sample) || !Array.isArray(d.transactions) || d.transactions.length === 0 ||
      d.sample.size !== d.transactions.length || d.sample.population !== s.transactions || d.sample.size > s.transactions) return false;
  if (new Set(d.transactions.map(t => t?.id)).size !== d.transactions.length) return false;
  return d.transactions.every(t => record(t) && text(t.id) && t.id.length > 0 && text(t.date) &&
    text(t.entity) && text(t.currency) && money(t.bank_amount) && text(t.module) &&
    ['matched', 'categorized', 'unidentified'].includes(t.outcome) && record(t.refs) &&
    Array.isArray(t.exception_codes) && t.exception_codes.every(text) && Array.isArray(t.allocations) &&
    t.allocations.every(a => record(a) && text(a.item_id) && money(a.amount_applied)));
}

function versionOf(snapshot, publishedAt = null) {
  return {phase: snapshot?.pipeline?.phase ?? null, run_id: snapshot?.pipeline?.run_id ?? null,
    started_at: snapshot?.pipeline?.started_at ?? null, published_at: publishedAt ?? snapshot?.hosting?.published_at ?? null,
    hash: snapshot?.pipeline?.sha256 ?? null};
}

function validVersion(v) {
  return v && ['live', 'final'].includes(v.phase) && text(v.run_id) && v.run_id.length > 0 &&
    iso(v.started_at) && /^[a-fA-F0-9]{64}$/.test(v.hash || '');
}

function order(next, previous) {
  if (next.run_id === previous.run_id) return next.hash === previous.hash && next.phase === previous.phase ? 'duplicate' : 'conflict';
  const rank = phase => phase === 'final' ? 2 : 1;
  if (rank(next.phase) !== rank(previous.phase)) return rank(next.phase) > rank(previous.phase) ? 'newer' : 'older';
  return Date.parse(next.started_at) > Date.parse(previous.started_at) ? 'newer' : 'older';
}

function response(body, status = 200, type = 'application/json; charset=utf-8', extra = {}) {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {status, headers: {
    'content-type': type, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
    'referrer-policy': 'no-referrer', 'x-frame-options': 'DENY', ...extra}});
}

async function authorized(request, secret) {
  if (!text(secret) || !secret.length) return false;
  const supplied = request.headers.get('authorization') || '';
  // Hash both sides to fixed length before constant-work comparison. Never log either value.
  const [actual, expected] = await Promise.all([supplied, `Bearer ${secret}`].map(value => crypto.subtle.digest('SHA-256', encoder.encode(value))));
  const a = new Uint8Array(actual), b = new Uint8Array(expected);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

async function boundedJSON(request) {
  const length = request.headers.get('content-length');
  if (length && (!/^\d+$/.test(length) || Number(length) > MAX_BYTES)) throw 413;
  if (!request.body) throw 400;
  const reader = request.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES) { await reader.cancel(); throw 413; }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const body = new Uint8Array(size);
  let position = 0;
  for (const chunk of chunks) { body.set(chunk, position); position += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(body)); }
  catch { throw 400; }
}

function publishedSnapshot(snapshot, publishedAt) {
  return {...snapshot, hosting: {...snapshot.hosting, public_snapshot: true, auto_refresh: true, published_at: publishedAt}};
}

export function createWorker(assets, seed) {
  const seedVersion = versionOf(seed);
  const fallback = publishedSnapshot(seed, seedVersion.published_at);
  return {
    async fetch(request, env) {
      const path = new URL(request.url).pathname;
      if (request.method === 'POST' && path === '/api/snapshot') {
        if (!(await authorized(request, env.DASHBOARD_PUBLISH_TOKEN))) return response({error: 'Unauthorized'}, 401);
        if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return response({error: 'JSON required'}, 415);
        let snapshot;
        try { snapshot = await boundedJSON(request); }
        catch (status) { return response({error: status === 413 ? 'Snapshot too large' : 'Invalid JSON'}, status === 413 ? 413 : 400); }
        if (!validSnapshot(snapshot)) return response({error: 'Snapshot validation failed'}, 422);
        const next = versionOf(snapshot);
        try {
          for (let attempt = 0; attempt < 3; attempt++) {
            const current = await env.BUCKET.head(SNAPSHOT_KEY);
            const previous = current ? current.customMetadata : seedVersion;
            if (current && !validVersion(previous)) return response({error: 'Snapshot storage unavailable'}, 503);
            if (validVersion(previous)) {
              const decision = order(next, previous);
              if (decision === 'duplicate' && current) return response({...previous, status: 'unchanged'});
              // A matching seed still needs its first durable R2 write.
              if (!['newer', 'duplicate'].includes(decision)) return response({error: 'Snapshot superseded or conflicts with current run'}, 409);
            }
            const publishedAt = new Date().toISOString();
            const version = versionOf(snapshot, publishedAt);
            const onlyIf = new Headers(current ? {'If-Match': current.httpEtag} : {'If-None-Match': '*'});
            const written = await env.BUCKET.put(SNAPSHOT_KEY, JSON.stringify(publishedSnapshot(snapshot, publishedAt)), {
              onlyIf, httpMetadata: {contentType: 'application/json; charset=utf-8'}, customMetadata: version});
            if (written !== null) return response({...version, status: 'published'}, 201);
          }
          return response({error: 'Concurrent update; retry publication'}, 409);
        } catch { return response({error: 'Snapshot storage unavailable'}, 503); }
      }
      if (!['GET', 'HEAD'].includes(request.method)) return response({error: 'Method not allowed'}, 405, undefined, {Allow: path === '/api/snapshot' ? 'POST' : 'GET, HEAD'});
      let result;
      if (path === '/data.json' || path === '/api/version') {
        try {
          if (path === '/api/version') {
            const current = await env.BUCKET.head(SNAPSHOT_KEY);
            if (current && !validVersion(current.customMetadata)) throw Error('Invalid metadata');
            result = response(current ? current.customMetadata : seedVersion);
          } else {
            const current = await env.BUCKET.get(SNAPSHOT_KEY);
            if (!current) result = response(fallback);
            else {
              if (!validVersion(current.customMetadata)) throw Error('Invalid metadata');
              // The R2 body is a stream; pass it directly without JSON-stringifying.
              result = new Response(current.body, {status: 200, headers: response('').headers});
            }
          }
        } catch { result = response({error: 'Snapshot storage unavailable'}, 503); }
      } else {
        const route = path === '/' ? '/index.html' : path;
        const types = {'/index.html': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'};
        result = types[route] && typeof assets[route] === 'string' ? response(assets[route], 200, types[route]) : response({error: 'Not found'}, 404);
      }
      return request.method === 'HEAD' ? new Response(null, {status: result.status, headers: result.headers}) : result;
    },
  };
}
