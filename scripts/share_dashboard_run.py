#!/usr/bin/env python3
"""Publish a small, verified run summary to dashboard/runs (never to the contest)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_BRANCH = 'dashboard/runs'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    # The supplied scorer writes a JSON object followed by a readable segment table.
    value, _ = json.JSONDecoder().raw_decode(Path(path).read_text().lstrip())
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def git(root, *args, check=True):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, timeout=90)
    if check and result.returncode:
        # Git stderr can include credential-bearing URLs: do not echo it.
        raise ValueError('Git operation failed: ' + args[0] + '; check repository access')
    return result


def provenance(root, report):
    branch = git(root, 'branch', '--show-current').stdout.strip() or 'detached'
    commit = report.get('source_commit')
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        commit = None
    # A working HEAD is not necessarily the code used by an older or frozen run.
    # Only attach it if every recorded module still matches and the tree is clean.
    hashes = report.get('source_hashes', {})
    if commit is None and hashes and not git(root, 'status', '--porcelain').stdout.strip():
        same = True
        for name, expected in hashes.items():
            relative = Path(name) if '/' in name else Path('recon') / name
            path = root / relative
            if not path.is_file() or digest(path) != expected:
                same = False
                break
        if same:
            commit = git(root, 'rev-parse', 'HEAD').stdout.strip()
    return branch, commit


def number(value, name, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    if value < 0 or (upper is not None and value > upper):
        raise ValueError(name + ' is out of range')
    return value


def build_summary(report_path, author, label='', evaluation_path=None, root=ROOT):
    report_path = Path(report_path).resolve()
    report = read_json(report_path)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,38}', author):
        raise ValueError('Author must be a GitHub username or short identifier')
    if len(label) > 120 or any(ord(c) < 32 for c in label):
        raise ValueError('Label must be a single line of at most 120 characters')
    validation = report.get('validation', {})
    if validation.get('ok') is not True or validation.get('missing', 0) != 0:
        raise ValueError('Run has not passed validation or has missing transactions')
    if report.get('source_changed_during_run') is True or report.get('checked_inputs_changed'):
        raise ValueError('Source or input data changed during this run')
    temporal = bool(report.get('start') and report.get('end'))
    phase = str(report.get('phase', 'train' if temporal else '')).lower()
    if phase not in {'sample', 'train', 'live', 'final'}:
        raise ValueError('Unknown run phase')
    official_ok = report.get('official_structure', {}).get('ok') is True
    if not official_ok and not (temporal and report.get('score', {}).get('ok') is True):
        raise ValueError('Solver report must pass the official format check')
    artifact = report.get('artifacts', {}).get('submission')
    if artifact:
        if Path(artifact).name != artifact:
            raise ValueError('Submission artifact must be a sibling filename')
        csv_path = report_path.parent / artifact
    elif report_path.name.endswith('.report.json'):
        csv_path = Path(str(report_path)[:-len('.report.json')])
    elif temporal:
        csv_path = report_path.parent / 'submission.csv.gz'
    else:
        raise ValueError('Cannot identify submission artifact')
    expected = report.get('sha256') or report.get('output_sha256')
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Report must include a submission SHA-256')
    if not csv_path.is_file() or digest(csv_path) != expected:
        raise ValueError('Submission is missing or its SHA-256 does not match the report')
    rows = int(number(validation.get('rows'), 'rows'))
    transactions = int(number(validation.get('transactions'), 'transactions'))
    score = precision = recall = benchmark = None
    evaluation = read_json(evaluation_path) if evaluation_path else report
    score_record = evaluation.get('score')
    if isinstance(score_record, (int, float)):
        score_record = evaluation
    if evaluation_path or isinstance(score_record, dict):
        if phase in {'live', 'final'}:
            raise ValueError('Local evaluations cannot be attached to LIVE or FINAL runs')
        if not isinstance(score_record, dict) or score_record.get('ok') is not True:
            raise ValueError('Evaluation did not pass scoring')
        evaluation_sha = evaluation.get('submission_sha256') or evaluation.get('output_sha256') or evaluation.get('sha256')
        if evaluation_sha and evaluation_sha != expected:
            raise ValueError('Evaluation refers to a different submission SHA-256')
        if evaluation_path and not evaluation_sha and Path(evaluation_path).resolve().parent != report_path.parent:
            raise ValueError('External evaluation requires a matching submission SHA-256')
        evaluation_phase = str(evaluation.get('phase', phase)).lower()
        if evaluation_phase != phase:
            raise ValueError('Evaluation benchmark phase does not match the run')
        if temporal:
            for key in ('start', 'end'):
                if evaluation.get(key, report[key]) != report[key]:
                    raise ValueError('Evaluation temporal window does not match')
            benchmark = 'TRAIN ' + report['start'] + ' → ' + report['end']
        elif phase == 'sample':
            benchmark = 'SAMPLE'
        else:
            raise ValueError('TRAIN scores require an explicit start and end date')
        metrics = score_record.get('metrics', {})
        if metrics.get('rows') != rows or metrics.get('txns_total') != transactions:
            raise ValueError('Evaluation row/transaction counts do not match the run')
        score = number(score_record.get('score'), 'local_score', 100)
        precision = number(metrics.get('precision', metrics.get('allocation_precision')), 'precision', 1)
        recall = number(metrics.get('recall', metrics.get('allocation_recall')), 'recall', 1)
    branch, commit = provenance(Path(root), report)
    date = report.get('finished_at') or report.get('started_at')
    if not date:
        date = datetime.fromtimestamp(report_path.stat().st_mtime, timezone.utc).isoformat()
    datetime.fromisoformat(date.replace('Z', '+00:00'))
    base_id = report.get('run_id') or expected[:24]
    if not isinstance(base_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', base_id):
        raise ValueError('Invalid run identifier')
    # An evaluated view is a distinct record from an earlier unscored share.
    evidence = json.dumps([score, benchmark, precision, recall, label], sort_keys=True).encode()
    run_id = author.lower() + '-' + base_id + '-' + hashlib.sha256(evidence).hexdigest()[:8]
    return {'schema_version': 1, 'run_id': run_id, 'author': author, 'branch': branch,
            'commit': commit, 'phase': phase, 'created_at': date, 'local_score': score,
            'benchmark': benchmark, 'precision': precision, 'recall': recall,
            'validation': {'ok': True, 'official_structure_ok': official_ok},
            'sha256': expected, 'transactions': transactions, 'rows': rows,
            'seconds': number(report.get('seconds', report.get('seconds_total', 0)), 'seconds'),
            'label': label}


def publish(summary, root=ROOT):
    root = Path(root)
    remote = git(root, 'remote', 'get-url', 'origin').stdout.strip()
    # Repo identity is fixed: do not accidentally send team summaries elsewhere.
    if remote.rstrip('/').removesuffix('.git') not in {
            'https://github.com/vitalio-sh/CLOSE-THE-BOOK',
            'git@github.com:vitalio-sh/CLOSE-THE-BOOK'}:
        raise ValueError('Origin must be vitalio-sh/CLOSE-THE-BOOK')
    content = json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    relative = Path('runs') / summary['author'].lower() / (summary['run_id'] + '.json')
    with tempfile.TemporaryDirectory(prefix='dashboard-share-') as folder:
        checkout = Path(folder)
        result = subprocess.run(['git', 'clone', '--quiet', '--depth', '1', '--single-branch',
                                 '--branch', PUBLISH_BRANCH, remote, str(checkout)],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise ValueError('Cannot access dashboard/runs. Check GitHub access and branch setup')
        for attempt in range(3):
            target = checkout / relative
            if target.exists():
                if target.read_text() == content:
                    return {'status': 'already_shared', 'run_id': summary['run_id'], 'branch': PUBLISH_BRANCH}
                raise ValueError('Run identifier already exists with different metadata; preserve existing history')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            git(checkout, 'add', '--', relative.as_posix())
            git(checkout, '-c', 'user.name=' + summary['author'], '-c',
                'user.email=' + summary['author'] + '@users.noreply.github.com',
                'commit', '--quiet', '-m', 'Share dashboard run ' + summary['run_id'])
            pushed = git(checkout, 'push', 'origin', 'HEAD:refs/heads/' + PUBLISH_BRANCH, check=False)
            if pushed.returncode == 0:
                return {'status': 'shared', 'run_id': summary['run_id'], 'branch': PUBLISH_BRANCH,
                        'workflow': 'https://github.com/vitalio-sh/CLOSE-THE-BOOK/actions/workflows/dashboard-runs.yml'}
            # A teammate may have published concurrently. Refresh the temporary clone only.
            git(checkout, 'fetch', '--depth', '1', 'origin', PUBLISH_BRANCH)
            git(checkout, 'reset', '--hard', 'FETCH_HEAD')
        raise ValueError('Push did not succeed after three attempts; check GitHub access and retry')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--repo', default='.', help='Repository checkout (default: current directory)')
    parser.add_argument('--author', required=True, help='Your GitHub username')
    parser.add_argument('--label', default='', help='Short description; visible on the public dashboard')
    parser.add_argument('--evaluation', help='Optional scorer JSON/score.txt for this exact run')
    parser.add_argument('--dry-run', action='store_true', help='Print the public summary without pushing')
    args = parser.parse_args()
    try:
        root = Path(git(Path(args.repo).resolve(), 'rev-parse', '--show-toplevel').stdout.strip())
        report_path = Path(args.report) if Path(args.report).is_absolute() else root / args.report
        evaluation_path = (Path(args.evaluation) if Path(args.evaluation).is_absolute()
                           else root / args.evaluation) if args.evaluation else None
        summary = build_summary(report_path, args.author, args.label, evaluation_path, root=root)
        result = summary if args.dry_run else publish(summary, root=root)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
