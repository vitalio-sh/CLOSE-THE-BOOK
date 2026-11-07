#!/usr/bin/env python3
"""Read official status into the local registry. This command never submits files."""
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recon.ledger import Ledger, DEFAULT_DB

API = 'https://hackathon.usetalky.com/api'
TEAM = 't_527b070f3ee6f60a'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def token():
    value = os.environ.get('PUBLISHTOKEN')
    if not value:
        for line in (ROOT / '.env').read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?PUBLISHTOKEN\s*=\s*(.*?)\s*$', line)
            if match:
                value = match.group(1).strip().strip('\"\'')
                break
    if not value:
        raise ValueError('PUBLISHTOKEN is not configured')
    return value


@contextmanager
def monitor_lock(ledger):
    with ledger.path.with_suffix('.monitor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def check(ledger, fetch=None):
    with monitor_lock(ledger):
        return _check(ledger, fetch)


def _check(ledger, fetch=None):
    key = token() if fetch is None else None
    def request(path, authenticated=False):
        headers = {'User-Agent': 'ValtermiaTeam10/1.0', 'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + key
        req = urllib.request.Request(API + path, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ValueError('Official API returned HTTP ' + str(error.code)) from None
    get = fetch or request
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(get, path, auth) for path, auth in
                   [('/state', False), ('/me', True), ('/submissions', True), ('/leaderboard', False)]]
        state, me, history, board = [f.result() for f in futures]
    if me.get('team_id') != TEAM:
        raise ValueError('Unexpected team identity')
    rows = board.get('rows', [])
    team = next((r for r in rows if r.get('team_name') == me['team_name']), {})
    scored_teams = [r for r in rows if isinstance(r.get('best_live'), (int, float))]
    leader = max(scored_teams, key=lambda r: r['best_live']) if scored_teams else {}
    submissions = []
    previous = ledger.checkpoint('platform:status') or {}
    prior_subs = {s['sub_id']: s for s in previous.get('submissions', [])}
    for raw in history.get('submissions', []):
        if raw.get('phase') != 'LIVE':
            continue
        item = {k: raw.get(k) for k in ('sub_id', 'phase', 'filename', 'created_at', 'status', 'score')}
        before = prior_subs.get(item['sub_id'])
        if before != item:
            detail = item
            if item['status'] == 'scored' and re.fullmatch(r'[A-Za-z0-9_-]+', item['sub_id'] or ''):
                detail = get('/submissions/' + item['sub_id'], True)
            observation = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
            ledger.observe_once('platform:submission:' + observation,
                                'platform.submission_observed', item['sub_id'], detail)
        submissions.append(item)
    submissions.sort(key=lambda s: s.get('created_at') or '', reverse=True)
    checked = datetime.now(timezone.utc).isoformat()
    result = {'checked_at': checked, 'team_id': TEAM, 'team_name': me['team_name'],
              'phase': state.get('phase'), 'accepting': state.get('accepting', []),
              'frozen': state.get('frozen'), 'limits': state.get('limits'),
              'deadlines': state.get('deadlines'), 'best_live': team.get('best_live'),
              'rank': team.get('rank'), 'leader': leader.get('team_name'),
              'leader_score': leader.get('best_live'), 'submissions': submissions,
              'final_used': history.get('final', {}).get('used', False)}
    result['latest_live'] = next((s['score'] for s in submissions if s.get('status') == 'scored'), None)
    result['gap'] = round(result['leader_score'] - result['best_live'], 2) if result['best_live'] is not None and result['leader_score'] is not None else None
    ledger.checkpoint('platform:status', result, write=True)
    ledger.event('platform.checked', TEAM, {k: result[k] for k in ('checked_at', 'best_live', 'latest_live', 'rank', 'gap')})
    path = ROOT / 'outputs/platform/live_monitor_state.json'
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing['platform'] = result
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        stream.write(json.dumps(existing, indent=2) + '\n')
        temp = Path(stream.name)
    temp.replace(path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    try:
        result = check(Ledger(args.db))
    except (ValueError, OSError, urllib.error.URLError) as error:
        parser.exit(1, 'Status check failed: ' + type(error).__name__ + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
