#!/usr/bin/env python3
"""Submit a locally improved, provenance-checked candidate to LIVE only."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
API = 'https://hackathon.usetalky.com/api'
TEAM_ID = 't_527b070f3ee6f60a'
TERMINAL = {'scored', 'received', 'invalid', 'error'}
METRICS = ('precision', 'recall', 'txn_accuracy', 'exception_macro_f1')


class SubmissionError(ValueError):
    """A safe operational error whose message never contains credentials."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise SubmissionError(message)


def number(value, upper=100):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= upper


def git_bytes(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=30)
    require(result.returncode == 0, 'Candidate Git reference or source cannot be verified')
    return result.stdout


def resolve(root, ref):
    require(isinstance(ref, str) and ref and not ref.startswith('-'), 'Invalid candidate reference')
    return git_bytes(root, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()


def normalized_score(value):
    require(isinstance(value, dict) and number(value.get('score')), 'Missing valid local score')
    metrics = value.get('metrics', value)
    require(all(number(metrics.get(k), 1) for k in METRICS), 'Missing local quality metrics')
    return {'score': value['score'], **{k: metrics[k] for k in METRICS}}


def verify_evidence(path, candidate, report, root=ROOT):
    path = Path(path).resolve()
    raw = path.read_bytes()
    evidence = json.loads(raw)
    require(evidence.get('schema_version') == 1, 'Unsupported evidence schema')
    commit = resolve(root, candidate)
    require(resolve(root, evidence.get('candidate')) == commit, 'Evidence is for another candidate')
    require(evidence.get('candidate_commit') == commit, 'Candidate tag differs from pinned evidence commit')
    baseline_commit = resolve(root, evidence.get('baseline_candidate'))
    require(evidence.get('baseline_commit') == baseline_commit, 'Baseline tag differs from pinned evidence commit')
    if evidence.get('parent_evidence_path'):
        parent = Path(evidence['parent_evidence_path'])
        if not parent.is_absolute():
            parent = root / parent
        require(file_digest(parent) == evidence.get('parent_evidence_sha256'), 'Parent evidence changed')
    require(evidence.get('validation') == {'reviewed': True, 'no_label_leakage': True}, 'Evidence review is incomplete')
    hashes = report.get('source_hashes')
    require(isinstance(hashes, dict) and hashes, 'Run has no source manifest')
    require(evidence.get('candidate_source_hashes') == hashes, 'Evidence and LIVE source manifests differ')
    for name in ('context.py', 'matching.py', 'batches.py', 'documents.py', 'run.py', 'validate.py'):
        require(name in hashes, 'LIVE source manifest is incomplete')
    for name, expected in hashes.items():
        require(re.fullmatch(r'[A-Za-z0-9_.-]+\.py', name) is not None, 'Unsafe source manifest filename')
        require(digest(git_bytes(root, 'show', commit + ':recon/' + name)) == expected,
                'Tagged code differs from source used by LIVE run: ' + name)
    checks = evidence.get('checks')
    require(isinstance(checks, list) and len(checks) >= 2, 'SAMPLE and temporal comparisons are required')
    have_sample = have_temporal = gained = False
    seen, seen_windows, seen_inputs = set(), set(), set()
    for check in checks:
        dataset = check.get('dataset')
        require(isinstance(dataset, str) and dataset not in seen, 'Missing or duplicate benchmark')
        seen.add(dataset)
        have_sample |= dataset.lower() == 'sample'
        have_temporal |= dataset.lower().startswith('train:')
        require(dataset.lower() == 'sample' or dataset.lower().startswith('train:'), 'Unsupported comparison benchmark')
        if dataset.lower().startswith('train:'):
            require(re.fullmatch(r'train:\d{4}-\d\d-\d\d:\d{4}-\d\d-\d\d', dataset.lower()) is not None,
                    'Temporal comparison needs an explicit date range')
            _, start, end = dataset.split(':')
            require(datetime.fromisoformat(start) <= datetime.fromisoformat(end), 'Invalid temporal range')
        source_path = Path(check['evidence_path'])
        if not source_path.is_absolute():
            source_path = root / source_path
        source_bytes = source_path.read_bytes()
        require(digest(source_bytes) == check.get('evidence_sha256'), 'Comparison report changed')
        comparison = json.loads(source_bytes)
        require(comparison.get('candidate_sha') == commit and comparison.get('before_sha') == baseline_commit,
                'Comparison is not pinned to candidate and baseline commits')
        window = comparison.get('windows', {}).get(check.get('evidence_window'))
        require(isinstance(window, dict), 'Comparison window is missing')
        window_identity = (str(source_path.resolve()), check.get('evidence_window'))
        require(window_identity not in seen_windows, 'Comparison window reused as another benchmark')
        seen_windows.add(window_identity)
        require((check.get('evidence_window') == 'sample') == (dataset.lower() == 'sample'), 'Comparison dataset label is inconsistent')
        require(window.get('tag_source_hashes_verified') is True, 'Comparison did not verify tagged source')
        require(resolve(root, window.get('after_tag')) == commit, 'Comparison used another candidate')
        require(resolve(root, window.get('before_tag')) == baseline_commit, 'Comparison used another baseline')
        baseline = normalized_score(window.get('before_score'))
        after = normalized_score(window.get('after_score'))
        require(normalized_score(check.get('baseline')) == baseline and normalized_score(check.get('candidate')) == after,
                'Evidence score does not match comparison report')
        inputs = window.get('file_hashes', {})
        require(all(re.fullmatch(r'[a-f0-9]{64}', inputs.get(k, '')) for k in ('before', 'after', 'bank', 'golden', 'categories', 'exceptions')),
                'Comparison input/output hashes are incomplete')
        manifest = {k: v for k, v in inputs.items() if k not in ('before', 'after')}
        expected_manifest = digest(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
        require(check.get('input_manifest_sha256') == expected_manifest, 'Benchmark manifest differs')
        require(expected_manifest not in seen_inputs, 'Same inputs reused as independent benchmark')
        seen_inputs.add(expected_manifest)
        scorer = Path(window.get('scorer', ''))
        require(scorer.is_file() and file_digest(scorer) == window.get('scorer_sha256') == check.get('scorer_sha256'),
                'Reference scorer cannot be verified')
        require(after['score'] >= baseline['score'], 'Local score regressed on ' + dataset)
        require(after['precision'] + .002 + 1e-12 >= baseline['precision'], 'Allocation precision regressed materially on ' + dataset)
        gained |= after['score'] > baseline['score']
    require(have_sample and have_temporal and gained, 'No confirmed gain across SAMPLE and a temporal comparison')
    return {'path': str(path), 'sha256': digest(raw), 'candidate_commit': commit,
            'baseline_commit': baseline_commit, 'document': evidence}


def verify_artifact(submission):
    path = Path(submission).resolve()
    require(path.name.endswith(('.csv', '.csv.gz')), 'Expected a CSV or CSV.gz file')
    report_path = Path(str(path) + '.report.json')
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    payload = path.read_bytes()  # Freeze the exact bytes before any external action.
    require(report.get('phase') == 'live', 'Only LIVE artifacts are accepted')
    require(report.get('sha256') == digest(payload), 'LIVE artifact SHA-256 mismatch')
    require(report.get('source_changed_during_run') is False and not report.get('checked_inputs_changed'),
            'LIVE source or inputs changed during execution')
    validation = report.get('validation', {})
    require(validation.get('ok') is True and report.get('official_structure', {}).get('ok') is True,
            'LIVE artifact has not passed both validators')
    total = validation.get('transactions')
    require(isinstance(total, int) and total > 0 and validation.get('covered') == total and validation.get('missing') == 0,
            'LIVE transaction coverage is incomplete')
    require(isinstance(validation.get('rows'), int) and total <= validation['rows'] <= 3000000, 'Invalid submission row count')
    audit = path.parent / report.get('artifacts', {}).get('audit', path.name + '.audit.jsonl')
    require(audit.parent.resolve() == path.parent, 'Audit must be next to the submission')
    require(file_digest(audit) == report.get('audit_sha256'), 'Audit SHA-256 mismatch')
    stream = gzip.GzipFile(fileobj=io.BytesIO(payload)) if path.name.endswith('.gz') else io.BytesIO(payload)
    length = 0
    with stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            length += len(chunk)
            require(length <= 60 * 1024 * 1024, 'Uncompressed file exceeds 60 MiB')
    require(length == validation.get('uncompressed_bytes'), 'Validated uncompressed size differs')
    return {'path': path, 'report_path': report_path, 'report': report, 'payload': payload,
            'sha256': digest(payload), 'report_sha256': digest(report_bytes), 'uncompressed_bytes': length, 'audit_path': audit}


def archive_artifact(artifact, root):
    """Keep the bytes sent even when a later solver run overwrites its output path."""
    # Distinct runs may produce identical CSV bytes but different valid reports.
    folder = root / 'outputs/platform/artifacts' / artifact['sha256'] / artifact['report_sha256']
    folder.mkdir(parents=True, exist_ok=True)
    original_report = folder / 'original_report.json'
    if original_report.exists():
        require(file_digest(original_report) == artifact['report_sha256'], 'Archived original report was modified')
    else:
        original_bytes = artifact['report_path'].read_bytes()
        require(digest(original_bytes) == artifact['report_sha256'], 'Original report changed during archiving')
        temporary = folder / ('original-report-' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(original_bytes)
        temporary.replace(original_report)
    name = 'submission.csv.gz' if artifact['path'].name.endswith('.gz') else 'submission.csv'
    output = folder / name
    if output.exists():
        require(file_digest(output) == artifact['sha256'], 'Archived artifact was modified')
    else:
        temporary = folder / ('payload-' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(artifact['payload'])
        temporary.replace(output)
    audit = folder / (name + '.audit.jsonl')
    if not audit.exists():
        temporary = folder / ('audit-' + uuid.uuid4().hex + '.tmp')
        shutil.copyfile(artifact['audit_path'], temporary)
        require(file_digest(temporary) == artifact['report']['audit_sha256'], 'Audit changed during archiving')
        temporary.replace(audit)
    require(file_digest(audit) == artifact['report']['audit_sha256'], 'Archived audit was modified')
    report = {**artifact['report'], 'artifacts': {'submission': name, 'audit': audit.name, 'report': name + '.report.json'}}
    report_path = folder / (name + '.report.json')
    body = (json.dumps(report, sort_keys=True, indent=2) + '\n').encode()
    if report_path.exists():
        require(report_path.read_bytes() == body, 'Archived report conflicts with existing artifact')
    else:
        temporary = folder / ('report-' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(body)
        temporary.replace(report_path)
    return {**artifact, 'original_path': artifact['path'], 'path': output, 'report_path': report_path,
            'report': report, 'report_sha256': digest(body), 'audit_path': audit,
            'original_report_path': original_report, 'original_report_sha256': artifact['report_sha256']}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def api(self, path, body=None):
        require(path.startswith('/') and '://' not in path, 'Invalid API route')
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json', 'User-Agent': 'ValtermiaTeam10/1.0'}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with self.opener.open(urllib.request.Request(API + path, data=data, headers=headers), timeout=60) as response:
                value = json.load(response)
                require(isinstance(value, dict), 'Unexpected API response')
                return value
        except urllib.error.HTTPError as error:
            raise SubmissionError('Organizer API returned HTTP ' + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise SubmissionError('Organizer API request failed; recover persisted attempt before retrying') from None

    def upload(self, upload, filename, payload):
        url = urllib.parse.urlsplit(upload['url'])
        require(url.scheme == 'https' and url.hostname and not url.username and not url.password and not url.fragment,
                'Invalid upload destination')
        boundary = '----Valtermia' + uuid.uuid4().hex
        parts = []
        for name, value in upload['fields'].items():
            require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', name), 'Invalid upload form field')
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        parts.extend([payload, f'\r\n--{boundary}--\r\n'.encode()])
        request = urllib.request.Request(upload['url'], data=b''.join(parts), headers={
            'Content-Type': 'multipart/form-data; boundary=' + boundary, 'User-Agent': 'ValtermiaTeam10/1.0'})
        try:
            with self.opener.open(request, timeout=180) as response:
                require(response.status in (200, 201, 204), 'Upload was not accepted')
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SubmissionError('Upload outcome uncertain; reserved submission ID is preserved') from None


def load_token(root):
    token = os.environ.get('PUBLISHTOKEN')
    if not token:
        for line in (root / '.env').read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?PUBLISHTOKEN\s*=\s*(.*?)\s*$', line)
            if match:
                token = match.group(1).strip().strip('\"\'')
                break
    require(bool(token), 'PUBLISHTOKEN is unavailable')
    return token


@contextmanager
def submission_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SubmissionError('Another LIVE submission is already running') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def safe_response(sub):
    # Scoring metadata only. Never persist arbitrary server upload credentials.
    keys = ('sub_id', 'phase', 'filename', 'created_at', 'status', 'score', 'metrics', 'errors', 'rows', 'file_sha256')
    return {k: sub[k] for k in keys if k in sub}


def execute(artifact, candidate, evidence, ledger, transport, root=ROOT, sleep=time.sleep, clock=time.time):
    require(artifact['report'].get('phase') == 'live', 'Only LIVE is supported')
    require(file_digest(artifact['path']) == artifact['sha256'] and file_digest(artifact['report_path']) == artifact['report_sha256'],
            'Artifact or report changed after preflight')
    artifact = archive_artifact(artifact, root)
    registered = ledger.register_artifact(artifact['path'], artifact['report_path'])
    require(registered['sha256'] == artifact['sha256'], 'Registered artifact differs from frozen payload')
    candidate_id = 'live:' + evidence['candidate_commit'] + ':' + evidence['sha256'][:12]
    ledger.register_candidate(candidate_id=candidate_id, tag=candidate, owner='live-coordinator',
                              source_manifest=artifact['report']['source_hashes'],
                              evidence={'path': evidence['path'], 'sha256': evidence['sha256'], 'commit': evidence['candidate_commit']}, status='validated')
    ledger.event('submission_preflight_verified', artifact['sha256'], {'source': str(artifact.get('original_path')),
        'archive': str(artifact['path']), 'evidence_sha256': evidence['sha256'], 'candidate_commit': evidence['candidate_commit']})
    state = transport.api('/state')
    require(artifact['uncompressed_bytes'] <= min(60, state['limits']['max_upload_mb']) * 1024 * 1024,
            'Uncompressed file exceeds current platform limit')
    me = transport.api('/me')
    require(me.get('team_id') == TEAM_ID and me.get('approval') == 'APPROVED', 'Unexpected or unapproved team')
    history = transport.api('/submissions').get('submissions')
    require(isinstance(history, list), 'Submission history is unavailable')
    existing = ledger.get_submission(artifact['sha256'])
    if existing is not None:
        return recover(existing, artifact, candidate, evidence, ledger, transport, root, sleep, clock,
                       may_queue='LIVE' in state.get('accepting', []) and not state.get('frozen'))
    require('LIVE' in state.get('accepting', []) and not state.get('frozen'), 'LIVE is not accepting submissions')
    # A gain must be against the latest scored source, not a selectively weaker old baseline.
    prior = next((s for s in ledger.status()['submissions'] if s['status'] == 'scored'), None)
    if prior:
        baseline = ledger.get_artifact(prior['artifact_sha'])
        baseline_hashes = baseline.get('source_hashes_json') or {}
        require(bool(baseline_hashes), 'Latest scored source manifest is unavailable')
        for name, expected in baseline_hashes.items():
            require(re.fullmatch(r'[A-Za-z0-9_.-]+\.py', name) is not None, 'Unsafe baseline source filename')
            require(digest(git_bytes(root, 'show', evidence['baseline_commit'] + ':recon/' + name)) == expected,
                    'Comparison baseline is not the latest scored source')
    now = datetime.fromtimestamp(clock(), timezone.utc)
    if existing is None:
        duplicate = next((s for s in history if s.get('phase') == 'LIVE' and
                          (s.get('file_sha256') or s.get('sha256')) == artifact['sha256']), None)
        if duplicate:
            attempt = ledger.begin_submission(artifact['sha256'], duplicate.get('filename', 'existing.csv.gz'), candidate_id=candidate_id)
            return finish(attempt, duplicate, ledger, artifact, candidate, evidence, root)
        today = now.date().isoformat()
        live_today = [s for s in history if s.get('phase') == 'LIVE' and str(s.get('created_at', '')).startswith(today)]
        require(len(live_today) < state['limits']['daily_per_phase'], 'Daily LIVE submission limit reached')
        dates = []
        for s in history:
            try:
                dates.append(datetime.fromisoformat(s['created_at'].replace('Z', '+00:00')).timestamp())
            except (KeyError, ValueError, TypeError):
                pass
        if dates:
            delay = max(0, max(dates) + state['limits']['min_interval_s'] - clock())
            require(delay <= 600, 'Latest platform submission time is unexpectedly far ahead')
            while delay > 0:
                sleep(min(30, delay))
                delay = max(0, max(dates) + state['limits']['min_interval_s'] - clock())
        suffix = '.csv.gz' if artifact['path'].name.endswith('.gz') else '.csv'
        filename = 'team10_' + artifact['sha256'][:12] + '_' + uuid.uuid4().hex[:16] + suffix
        attempt = ledger.begin_submission(artifact['sha256'], filename, candidate_id=candidate_id)
        ledger.event('reservation_intent', attempt['local_attempt_id'], {'filename': filename, 'artifact_sha': artifact['sha256']})
        try:
            reserved = transport.api('/submissions/upload-url', {'phase': 'LIVE', 'filename': filename})
            sub_id = reserved['sub_id']
            require(re.fullmatch(r'[A-Za-z0-9_-]+', sub_id) is not None, 'Invalid returned submission ID')
            attempt = ledger.update_submission(attempt['local_attempt_id'], 'reserved', sub_id=sub_id)
        except Exception:
            ledger.update_submission(attempt['local_attempt_id'], 'reservation_unknown')
            return recover(attempt, artifact, candidate, evidence, ledger, transport, root, sleep, clock)
        ledger.update_submission(attempt['local_attempt_id'], 'uploading')
        try:
            transport.upload(reserved['upload'], filename, artifact['payload'])
        except Exception:
            ledger.update_submission(attempt['local_attempt_id'], 'upload_unknown')
            raise SubmissionError('Upload outcome uncertain; resume this artifact to inspect its existing submission') from None
        attempt = ledger.update_submission(attempt['local_attempt_id'], 'uploaded')
    return queue_and_poll(attempt, artifact, candidate, evidence, ledger, transport, root, sleep, clock)


def recover(attempt, artifact, candidate, evidence, ledger, transport, root, sleep, clock, may_queue=True):
    sub_id = attempt.get('sub_id')
    if not sub_id:
        history = transport.api('/submissions').get('submissions', [])
        found = [s for s in history if s.get('phase') == 'LIVE' and s.get('filename') == attempt['filename']]
        require(len(found) == 1, 'Reservation outcome unresolved; do not create another slot for this artifact')
        sub_id = found[0]['sub_id']
        attempt = ledger.update_submission(attempt['local_attempt_id'], 'reservation_unknown', sub_id=sub_id)
    require(re.fullmatch(r'[A-Za-z0-9_-]+', sub_id) is not None, 'Invalid persisted submission ID')
    sub = transport.api('/submissions/' + sub_id)
    if sub.get('status') in TERMINAL:
        return finish(attempt, sub, ledger, artifact, candidate, evidence, root)
    if sub.get('status') in {'queued', 'scoring'}:
        return poll(attempt, sub, artifact, candidate, evidence, ledger, transport, root, sleep, clock)
    if attempt['status'] == 'uploaded':
        require(may_queue, 'LIVE is closed; the confirmed upload is retained without queueing')
        return queue_and_poll(attempt, artifact, candidate, evidence, ledger, transport, root, sleep, clock)
    raise SubmissionError('Existing slot has no confirmed upload; preserve it and inspect manually, do not reserve another')


def queue_and_poll(attempt, artifact, candidate, evidence, ledger, transport, root, sleep, clock):
    try:
        sub = transport.api('/submissions/' + attempt['sub_id'] + '/score', {})
    except SubmissionError:
        # The queue request may have arrived. GET first, never reserve another slot.
        sub = transport.api('/submissions/' + attempt['sub_id'])
        require(sub.get('status') in TERMINAL | {'queued', 'scoring'},
                'Scoring request outcome is unresolved; confirmed upload is preserved for resume')
    return poll(attempt, sub, artifact, candidate, evidence, ledger, transport, root, sleep, clock)


def poll(attempt, sub, artifact, candidate, evidence, ledger, transport, root, sleep, clock):
    deadline = clock() + 600
    while sub.get('status') not in TERMINAL and clock() < deadline:
        ledger.update_submission(attempt['local_attempt_id'], 'scoring', response=safe_response(sub))
        sleep(5)
        sub = transport.api('/submissions/' + attempt['sub_id'])
    return finish(attempt, sub, ledger, artifact, candidate, evidence, root)


def finish(attempt, sub, ledger, artifact, candidate, evidence, root):
    response = safe_response(sub)
    require(response.get('phase', 'LIVE') == 'LIVE', 'Unexpected submission phase in response')
    if attempt.get('sub_id'):
        require(response.get('sub_id') == attempt['sub_id'], 'Scoring response refers to another submission')
    if response.get('file_sha256'):
        require(response['file_sha256'] == artifact['sha256'], 'Server reports a different artifact hash')
    if response.get('status') == 'scored':
        require(number(response.get('score')), 'Scored response has no valid official score')
    if response.get('score') is not None:
        require(number(response['score']), 'Invalid official score')
    status = response.get('status', 'scoring')
    if status not in TERMINAL:
        status = 'scoring'
    result = ledger.update_submission(attempt['local_attempt_id'], status, sub_id=response.get('sub_id'),
                                      response=response, official_score=response.get('score'))
    record = {'phase': 'LIVE', 'team_id': TEAM_ID, 'candidate': candidate,
              'candidate_commit': evidence['candidate_commit'], 'evidence_sha256': evidence['sha256'],
              'source': str(artifact['path']), 'sha256': artifact['sha256'],
              'run_id': artifact['report'].get('run_id'), 'local_attempt_id': attempt['local_attempt_id'],
              'sub_id': result.get('sub_id'), 'stage': status, 'filename': attempt['filename'],
              'response': response, 'finished_at': datetime.now(timezone.utc).isoformat()}
    folder = root / 'reports'
    folder.mkdir(exist_ok=True)
    response_hash = digest(json.dumps(response, sort_keys=True, separators=(',', ':')).encode())[:12]
    path = folder / ('live_official_' + attempt['local_attempt_id'] + '_' + response_hash + '.json')
    if not path.exists():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(path)
    else:
        saved = json.loads(path.read_text())
        require(saved.get('sha256') == artifact['sha256'] and saved.get('response') == response,
                'An immutable official response record was changed')
    return {'status': status, 'sub_id': result.get('sub_id'), 'score': response.get('score'),
            'artifact_sha': artifact['sha256'], 'report': str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submission', required=True)
    parser.add_argument('--candidate', required=True, help='Existing immutable candidate Git tag or commit')
    parser.add_argument('--evidence', required=True, help='Hashed, comparable local improvement evidence')
    parser.add_argument('--db', type=Path, default=ROOT / 'outputs/platform/progress.sqlite3')
    parser.add_argument('--dry-run', action='store_true', help='Verify locally; do not access organizer API or reserve a slot')
    args = parser.parse_args()
    try:
        artifact = verify_artifact(args.submission)
        evidence = verify_evidence(args.evidence, args.candidate, artifact['report'])
        if args.dry_run:
            result = {'status': 'verified', 'phase': 'LIVE', 'candidate': args.candidate,
                      'candidate_commit': evidence['candidate_commit'], 'sha256': artifact['sha256'],
                      'evidence_sha256': evidence['sha256'], 'uncompressed_bytes': artifact['uncompressed_bytes']}
        else:
            from recon.ledger import Ledger
            with submission_lock(ROOT / 'outputs/platform/live-submit.lock'):
                ledger = Ledger(args.db)
                result = execute(artifact, args.candidate, evidence, ledger, Transport(load_token(ROOT)))
        print(json.dumps(result, indent=2))
    except (SubmissionError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        # File errors may include paths, but no requests/credentials are ever printed.
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
