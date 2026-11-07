"""Best-effort publication of verified LIVE/FINAL dashboard snapshots.

Configuration is opt-in: ignored .dashboard-publish.json containing enabled,
url and token. DASHBOARD_PUBLISH_ENABLED/URL/TOKEN override individual settings.
Secrets are sent to a bounded child process via stdin, never command arguments,
status files, snapshot files or error logs. Redirects are never followed.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STANDARD_MODULES = ['batches', 'documents', 'matching']
MAX_BODY = 2 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.' + path.name,
                dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_config(root, env=None):
    env = os.environ if env is None else env
    path = Path(root) / '.dashboard-publish.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    for key in ('enabled', 'url', 'token'):
        name = 'DASHBOARD_PUBLISH_' + key.upper()
        if name in env:
            config[key] = env[name]
    config['enabled'] = config.get('enabled') is True or str(config.get('enabled', '')).lower() in ('1', 'true', 'yes')
    return config


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('missing run timestamp')
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('run timestamps must include timezone')
    return date


def check_snapshot(snapshot):
    pipeline = snapshot.get('pipeline', {})
    summary = snapshot.get('summary', {})
    checks = snapshot.get('export_checks', {})
    validation = pipeline.get('validation') or {}
    run_id = pipeline.get('run_id')
    if snapshot.get('schema_version') != 1 or pipeline.get('phase') not in ('live', 'final'):
        raise ValueError('unsupported snapshot phase/schema')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id):
        raise ValueError('missing valid run identifier')
    if parse_time(pipeline.get('started_at')) > parse_time(pipeline.get('finished_at')):
        raise ValueError('run timestamps out of order')
    if not re.fullmatch(r'[0-9a-f]{64}', pipeline.get('sha256') or '') or pipeline.get('hash_verified') is not True:
        raise ValueError('unverified submission hash')
    if pipeline.get('audit_hash_verified') is not True:
        raise ValueError('unverified audit hash')
    if pipeline.get('source_changed_during_run') is not False:
        raise ValueError('source changed during run')
    if validation.get('ok') is not True or (pipeline.get('official_structure') or {}).get('ok') is not True:
        raise ValueError('run validation not successful')
    total = summary.get('transactions')
    if (not isinstance(total, int) or isinstance(total, bool) or total <= 0
            or summary.get('submission_transactions') != total
            or validation.get('transactions') != total or validation.get('covered') != total
            or validation.get('missing') != 0):
        raise ValueError('incomplete transaction coverage')
    if any(checks.get(key) != 0 for key in ('missing_submission_transactions', 'submission_ids_absent_from_bank', 'audit_ids_missing')):
        raise ValueError('missing export evidence')
    if checks.get('module_counts_match_report') is not True:
        raise ValueError('module counts do not agree')
    sample = snapshot.get('sample', {})
    transactions = snapshot.get('transactions', [])
    if not transactions or sample.get('size') != len(transactions) or sample.get('population') != total:
        raise ValueError('sample inconsistent with population')
    if pipeline.get('enabled_modules') != STANDARD_MODULES:
        raise ValueError('nonstandard module run')


def snapshot_order(snapshot):
    pipeline = snapshot['pipeline']
    return (1 if pipeline['phase'] == 'final' else 0, parse_time(pipeline['started_at']))


def update_local(path, snapshot):
    """Avoid replacing a newer local snapshot when retrying an old outbox entry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            try:
                old = json.loads(path.read_text())
                if snapshot_order(old) > snapshot_order(snapshot):
                    return False
                if snapshot_order(old) == snapshot_order(snapshot) and old['pipeline']['sha256'] != snapshot['pipeline']['sha256']:
                    return False
            except (ValueError, KeyError, TypeError):
                pass  # Older pre-automation snapshots may lack ordering metadata.
        atomic_json(path, snapshot)
        return True


def http_attempt(url, token, body, timeout):
    """Killable HTTP operation; timeout includes DNS, TLS and response reads."""
    request = json.dumps({'url': url, 'token': token, 'body': body.decode('utf-8')})
    process = subprocess.run([sys.executable, '-m', 'recon.dashboard_publish', '--transport'],
        input=request, text=True, capture_output=True, cwd=ROOT, timeout=timeout, check=False)
    if process.returncode != 0:
        return {'code': 0, 'error': 'transport_failed'}
    try:
        result = json.loads(process.stdout)
        return result if isinstance(result, dict) else {'code': 0, 'error': 'invalid_response'}
    except ValueError:
        return {'code': 0, 'error': 'invalid_response'}


def send_snapshot(config, snapshot, attempt=http_attempt):
    url = str(config.get('url', ''))
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path != '/api/snapshot':
        return {'state': 'pending', 'reason': 'invalid_endpoint', 'attempts': 0}
    body = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(body) > MAX_BODY:
        return {'state': 'pending', 'reason': 'snapshot_too_large', 'attempts': 0}
    deadline = time.monotonic() + 20
    attempts = 0
    result = {'code': 0, 'error': 'network_timeout'}
    while attempts < 2 and deadline - time.monotonic() > 0.05:
        attempts += 1
        try:
            result = attempt(url, config['token'], body, min(9.5, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, TimeoutError):
            result = {'code': 0, 'error': 'network_timeout'}
        except Exception:
            result = {'code': 0, 'error': 'transport_failed'}
        code = result.get('code', 0)
        if 200 <= code < 300 and result.get('run_id') == snapshot['pipeline']['run_id'] and result.get('hash') == snapshot['pipeline']['sha256'] and result.get('status') in ('published', 'unchanged'):
            return {'state': 'published', 'reason': 'accepted', 'attempts': attempts, 'http_status': code}
        if code == 409:
            return {'state': 'superseded', 'reason': 'server_has_newer_snapshot', 'attempts': attempts, 'http_status': code}
        if code and code < 500 and code != 429:
            break
    return {'state': 'pending', 'reason': 'network_timeout' if result.get('error') == 'network_timeout' else 'publish_failed',
        'attempts': attempts, 'http_status': result.get('code', 0)}


def publish_dashboard(submission, data_root='data/workspace', *, enabled_modules=None,
                      root=None, env=None, attempt=http_attempt):
    """Never raise publication failures into the reconciliation pipeline."""
    root = Path(root) if root is not None else ROOT
    submission = Path(submission)
    status_path = Path(str(submission) + '.dashboard-status.json')
    status = {'updated_at': utc_now(), 'state': 'skipped', 'reason': 'not_configured', 'attempts': 0}
    outbox_status = None
    try:
        report = json.loads(Path(str(submission) + '.report.json').read_text())
        status['run_id'] = report.get('run_id')
        status['phase'] = report.get('phase')
        modules = enabled_modules if enabled_modules is not None else report.get('enabled_modules')
        if modules is None and all(k in (report.get('modules') or {}) for k in STANDARD_MODULES):
            modules = STANDARD_MODULES
        if report.get('phase') not in ('live', 'final'):
            status['reason'] = 'phase_not_live_or_final'
        elif modules != STANDARD_MODULES:
            status['reason'] = 'nonstandard_modules'
        else:
            config = load_config(root, env)
            if not config['enabled']:
                status['reason'] = 'not_configured'
            elif not config.get('token'):
                status['reason'] = 'missing_token'
            else:
                from scripts.build_dashboard_data import export_dashboard
                snapshot = export_dashboard(submission, data_root)
                check_snapshot(snapshot)
                run_id = snapshot['pipeline']['run_id']
                status['run_id'] = run_id
                outbox = root / 'outputs/dashboard-outbox' / (run_id + '.json')
                if outbox.exists() and json.loads(outbox.read_text())['pipeline']['sha256'] != snapshot['pipeline']['sha256']:
                    raise ValueError('run identifier collision')
                atomic_json(outbox, snapshot)
                outbox_status = outbox.with_suffix('.status.json')
                update_local(root / 'dashboard/data.json', snapshot)
                status.update({'state': 'pending', 'reason': 'awaiting_publish', 'outbox': str(outbox), 'sha256': snapshot['pipeline']['sha256'],
                    'outbox_sha256': hashlib.sha256(outbox.read_bytes()).hexdigest()})
                # Persist retry evidence before starting network activity.
                atomic_json(status_path, status)
                atomic_json(outbox_status, status)
                status.update(send_snapshot(config, snapshot, attempt))
    except Exception:
        # Exception strings can contain URLs/headers; record only a fixed category.
        status.update({'state': 'pending', 'reason': 'snapshot_or_configuration_invalid'})
    status['updated_at'] = utc_now()
    try:
        atomic_json(status_path, status)
        if outbox_status is not None:
            atomic_json(outbox_status, status)
    except OSError:
        status.update({'state': 'pending', 'reason': 'status_write_failed'})
    return status


def publish_outbox(snapshot_path, *, root=None, env=None, attempt=http_attempt):
    """Retry a durable snapshot even when a later run replaced original CSVs."""
    root = Path(root) if root is not None else ROOT
    path = Path(snapshot_path)
    status_path = path.with_suffix('.status.json')
    status = {'updated_at': utc_now(), 'state': 'pending', 'reason': 'invalid_outbox', 'attempts': 0}
    try:
        prior = json.loads(status_path.read_text())
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != prior.get('outbox_sha256'):
            raise ValueError('outbox content changed')
        snapshot = json.loads(raw)
        check_snapshot(snapshot)
        if prior.get('run_id') != snapshot['pipeline']['run_id']:
            raise ValueError('outbox identity changed')
        status.update({k: prior[k] for k in ('run_id', 'phase', 'outbox', 'sha256', 'outbox_sha256') if k in prior})
        config = load_config(root, env)
        if not config['enabled']:
            status.update({'state': 'skipped', 'reason': 'not_configured'})
        elif not config.get('token'):
            status.update({'state': 'skipped', 'reason': 'missing_token'})
        else:
            update_local(root / 'dashboard/data.json', snapshot)
            status.update(send_snapshot(config, snapshot, attempt))
    except Exception:
        status.update({'state': 'pending', 'reason': 'invalid_outbox'})
    status['updated_at'] = utc_now()
    # Preserve the original retry proof if validation failed.
    if status['reason'] != 'invalid_outbox':
        try:
            atomic_json(status_path, status)
        except OSError:
            status.update({'state': 'pending', 'reason': 'status_write_failed'})
    return status


def transport_main():
    """Private subprocess entry: prints only sanitized HTTP metadata."""
    from urllib.error import HTTPError
    from urllib.request import Request, build_opener, HTTPRedirectHandler

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise HTTPError(req.full_url, code, 'redirect forbidden', headers, fp)
    try:
        values = json.load(sys.stdin)
        request = Request(values['url'], data=values['body'].encode('utf-8'), method='POST',
            headers={'Authorization': 'Bearer ' + values['token'], 'Content-Type': 'application/json',
                     'User-Agent': 'ValtermiaDashboardPublisher/1.0'})
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(16384)  # Bounded and contained by parent timeout.
            reply = json.loads(raw)
            result = {'code': response.status, **{k: reply.get(k) for k in ('run_id', 'hash', 'status')}}
    except HTTPError as exc:
        result = {'code': exc.code}
    except Exception:
        result = {'code': 0, 'error': 'transport_failed'}
    print(json.dumps(result))


if __name__ == '__main__':
    if sys.argv[1:] == ['--transport']:
        transport_main()
    else:
        raise SystemExit('Use scripts/publish_dashboard.py to publish an existing run')
