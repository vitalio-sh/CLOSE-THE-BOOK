// Official scores are fetched server-side; browser clients never receive credentials.
const OFFICIAL_KEY = 'dashboard/official-progress.json';
const RUN_PREFIX = 'dashboard/runs/';
const API = 'https://hackathon.usetalky.com/api';
const TEAM_ID = 't_527b070f3ee6f60a';
const object = x => x && typeof x === 'object' && !Array.isArray(x);
const finite = x => typeof x === 'number' && Number.isFinite(x);
const score = x => finite(x) && x >= 0 && x <= 100;
const fraction = x => x === null || (finite(x) && x >= 0 && x <= 1);
const id = x => typeof x === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(x);
const short = (x, max = 200) => typeof x === 'string' && x.length > 0 && x.length <= max && !/[\x00-\x1f]/.test(x);
const date = x => typeof x === 'string' && /^\d{4}-\d\d-\d\dT/.test(x) && Number.isFinite(Date.parse(x));
const whole = x => Number.isSafeInteger(x) && x >= 0;
const hash = x => typeof x === 'string' && /^[a-f0-9]{64}$/.test(x);

export function cleanRun(d) {
  if (!object(d) || d.schema_version !== 1 || !id(d.run_id) || !short(d.author, 80) ||
      !short(d.branch) || !(d.commit === null || /^[a-f0-9]{40}$/.test(d.commit || '')) ||
      !['sample', 'train', 'live', 'final'].includes(d.phase) || !date(d.created_at) ||
      Date.parse(d.created_at) > Date.now() + 300000 || !hash(d.sha256) ||
      !whole(d.transactions) || d.transactions < 1 || !whole(d.rows) || d.rows < d.transactions ||
      !finite(d.seconds) || d.seconds < 0 || d.validation?.ok !== true ||
      typeof d.validation.official_structure_ok !== 'boolean' ||
      !(d.local_score === null || score(d.local_score)) ||
      !(d.benchmark === null || short(d.benchmark)) || !fraction(d.precision) || !fraction(d.recall) ||
      (d.label !== undefined && d.label !== '' && !short(d.label)) ||
      (['live', 'final'].includes(d.phase) && (d.local_score !== null || !d.validation.official_structure_ok)) ||
      (d.local_score !== null && !d.benchmark)) return null;
  // Copy only the public schema: never reflect arbitrary fields, paths or credentials.
  return {schema_version: 1, run_id: d.run_id, author: d.author, branch: d.branch, commit: d.commit,
    phase: d.phase, created_at: new Date(d.created_at).toISOString(), local_score: d.local_score,
    benchmark: d.benchmark, precision: d.precision, recall: d.recall,
    validation: {ok: true, official_structure_ok: d.validation.official_structure_ok},
    sha256: d.sha256, transactions: d.transactions, rows: d.rows, seconds: d.seconds,
    ...(d.label ? {label: d.label} : {})};
}

async function jsonObject(bucket, key) {
  const stored = await bucket.get(key);
  return stored ? stored.json() : null;
}

async function digest(value) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');
}

export async function storeRun(bucket, raw) {
  const run = cleanRun(raw);
  if (!run) return {status: 422, body: {error: 'Run validation failed'}};
  // Identity independent of timestamp: corrections cannot create duplicate rows.
  const key = RUN_PREFIX + await digest(run.author + '\n' + run.run_id) + '.json';
  const body = JSON.stringify(run);
  const written = await bucket.put(key, body, {onlyIf: new Headers({'If-None-Match': '*'}),
    httpMetadata: {contentType: 'application/json'}, customMetadata: {created_at: run.created_at, run: body}});
  if (written) return {status: 201, body: {status: 'published', run_id: run.run_id}};
  const previous = await jsonObject(bucket, key);
  return JSON.stringify(previous) === body
    ? {status: 200, body: {status: 'unchanged', run_id: run.run_id}}
    : {status: 409, body: {error: 'Run ID already has different metadata'}};
}

export async function recordSnapshotRun(bucket, snapshot) {
  const p = snapshot.pipeline;
  return storeRun(bucket, {schema_version: 1, run_id: p.run_id, author: 'Team pipeline',
    branch: 'solution/vitalii', commit: null, phase: p.phase,
    created_at: p.finished_at || p.started_at, local_score: null, benchmark: null,
    precision: null, recall: null, validation: {ok: true, official_structure_ok: true},
    sha256: p.sha256, transactions: p.validation.transactions, rows: p.validation.rows,
    seconds: Number(p.seconds || 0), label: 'Verified full run'});
}

export function createProgressService(fetcher = fetch) {
  let inflight = null;
  async function api(path, token) {
    const headers = {'Accept': 'application/json', 'User-Agent': 'ValtermiaDashboard/1.0'};
    if (token) headers.Authorization = `Bearer ${token}`;
    const result = await fetcher(API + path, {headers, redirect: 'manual', signal: AbortSignal.timeout(10000)});
    if (!result.ok) throw Error('Organizer HTTP ' + result.status + ' at ' + path);
    return result.json();
  }
  async function refresh(env, old) {
    const now = new Date().toISOString();
    try {
      if (!env.ORGANIZER_READ_TOKEN) throw Error('Not configured');
      const [me, history, board] = await Promise.all([
        api('/me', env.ORGANIZER_READ_TOKEN), api('/submissions', env.ORGANIZER_READ_TOKEN), api('/leaderboard')]);
      if (me.team_id !== TEAM_ID || typeof me.team_name !== 'string' || !Array.isArray(history.submissions) || !Array.isArray(board.rows)) throw Error('Unexpected organizer response');
      const team = board.rows.find(x => x.team_name === me.team_name);
      const leader = board.rows.filter(x => score(x.best_live)).sort((a,b) => b.best_live - a.best_live)[0];
      const submissions = history.submissions.filter(x => x.phase === 'LIVE').map(s => ({
        sub_id: String(s.sub_id).slice(0,128), created_at: date(s.created_at) ? s.created_at : null,
        status: String(s.status).slice(0,40), filename: String(s.filename).slice(0,200),
        score: s.status === 'scored' && score(s.score) ? s.score : null,
      })).sort((a,b) => Date.parse(b.created_at) - Date.parse(a.created_at));
      const previousMetrics = new Map((old?.official?.submissions || []).map(s => [s.sub_id, s.metrics]));
      let detailBudget = 5;
      for (const s of submissions) {
        const known = previousMetrics.get(s.sub_id);
        if (known) { s.metrics = known; continue; }
        if (s.status !== 'scored' || detailBudget-- <= 0 || !/^[A-Za-z0-9_-]+$/.test(s.sub_id)) continue;
        try {
          const detail = await api('/submissions/' + s.sub_id, env.ORGANIZER_READ_TOKEN);
          const metrics = detail.metrics;
          if (fraction(metrics?.allocation_precision) && fraction(metrics?.allocation_recall)) {
            s.metrics = {allocation_precision: metrics.allocation_precision, allocation_recall: metrics.allocation_recall};
          }
        } catch { /* Score and rank remain usable when optional detail fails. */ }
      }
      const best = score(team?.best_live) ? team.best_live : null;
      const official = {team_name: me.team_name, best_live: best,
        rank: Number.isInteger(team?.rank) && team.rank > 0 ? team.rank : null,
        leader_score: leader?.best_live ?? null, leader_name: leader?.team_name ?? null,
        gap: best !== null && leader ? Math.round((leader.best_live - best) * 100) / 100 : null,
        checked_at: now, board_updated_at: date(board.updated_at) ? board.updated_at : null,
        submissions, source: 'https://hackathon.usetalky.com/clasificacion'};
      const cached = {official, last_attempt_at: now};
      await env.BUCKET.put(OFFICIAL_KEY, JSON.stringify(cached), {httpMetadata: {contentType: 'application/json'}});
      return official;
    } catch (error) {
      const diagnostic = String(error?.message || error?.name || 'Unknown error').split(env.ORGANIZER_READ_TOKEN || '\0').join('[redacted]').slice(0,200);
      console.error('Official progress refresh failed: ' + diagnostic);
      // Preserve checked_at from the last success; never make stale data look fresh.
      const official = old?.official ? {...old.official, error: 'Official scores temporarily unavailable; showing last verified result.'}
        : {team_name: 'Team 10', best_live: null, rank: null, leader_score: null, gap: null,
          checked_at: null, submissions: [], error: 'Official scores temporarily unavailable.'};
      try { await env.BUCKET.put(OFFICIAL_KEY, JSON.stringify({official, last_attempt_at: now}), {httpMetadata: {contentType: 'application/json'}}); } catch {}
      return official;
    }
  }
  async function official(env) {
    const old = await jsonObject(env.BUCKET, OFFICIAL_KEY);
    if (old && Date.now() - Date.parse(old.last_attempt_at) < 60000) return old.official;
    if (!inflight) inflight = refresh(env, old).finally(() => {inflight = null;});
    return inflight;
  }
  async function runs(env) {
    let cursor, keys = [], truncated = false;
    // Preserve all stored runs; bound public response and storage reads.
    for (let page = 0; page < 10; page++) {
      const listed = await env.BUCKET.list({prefix: RUN_PREFIX, limit: 1000, include: ['customMetadata'], ...(cursor ? {cursor} : {})});
      keys.push(...listed.objects);
      truncated = listed.truncated;
      if (!truncated) break;
      cursor = listed.cursor;
    }
    keys.sort((a,b) => Date.parse(b.customMetadata?.created_at || 0) - Date.parse(a.customMetadata?.created_at || 0));
    const selected = keys.slice(0,200), rows = selected.map(k => {
      try { return JSON.parse(k.customMetadata?.run || 'null'); } catch { return null; }
    });
    return {runs: rows.map(cleanRun).filter(Boolean), runs_truncated: truncated || keys.length > 200};
  }
  return {async read(env) {
    const [result, history] = await Promise.all([official(env), runs(env)]);
    return {official: result, ...history, updated_at: new Date().toISOString()};
  }};
}
