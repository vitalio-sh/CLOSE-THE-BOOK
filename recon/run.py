"""Run reconciliation and publish a validated, reproducible artifact bundle."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import resource
import sys
import tempfile
import time

from .preflight import check_inputs, digest
from .engine import build_handlers, execute
from .context import Context, read_csv
from .validate import FIELDS, validate, official_structure


def source_hashes():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob('*.py'))}


@contextmanager
def output_stream(path):
    """Gzip bytes do not depend on output filename or wall-clock time."""
    if path.suffix == '.gz':
        with path.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding='utf-8', newline='') as text:
                    yield text
    else:
        with path.open('w', encoding='utf-8', newline='') as text:
            yield text


def publish_bundle(candidate, destination):
    """Replace validated files, report last; rollback on a publication exception.

    Each rename is atomic on this filesystem. Readers use the report's hashes as
    the commit marker; three independent filenames cannot change atomically as a
    single filesystem operation. No existing bundle is touched before validation.
    """
    suffixes = ('', '.audit.jsonl', '.report.json')
    backups = {}
    replaced = []
    for index, suffix in enumerate(suffixes):
        target = Path(str(destination) + suffix)
        if target.exists():
            backup = candidate.parent / f'previous-{index}'
            shutil.copy2(target, backup)
            backups[suffix] = backup
    try:
        for suffix in suffixes:
            target = Path(str(destination) + suffix)
            os.replace(Path(str(candidate) + suffix), target)
            replaced.append(suffix)
    except BaseException:
        for suffix in reversed(replaced):
            target = Path(str(destination) + suffix)
            if suffix in backups:
                os.replace(backups[suffix], target)
            else:
                target.unlink(missing_ok=True)
        raise


def main(argv=None):
    from . import pdf_runtime
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='data/workspace')
    p.add_argument('--phase', choices=['sample', 'train', 'live', 'final'], default='sample')
    p.add_argument('--out', required=True)
    p.add_argument('--modules', default='batches,documents,matching')
    p.add_argument('--pdf-facts', action='store_true', help='Enable budgeted optional invoice-PDF facts')
    p.add_argument('--pdf-max-documents', type=int, default=100)
    p.add_argument('--pdf-budget-seconds', type=float, default=30)
    pdf_runtime.add_options(p, base=False)
    a = p.parse_args(argv)
    pdf_runtime.check_options(a)
    start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid4())
    stage_start = start
    timings = {}
    def stage(name):
        nonlocal stage_start
        now = time.monotonic()
        timings[name] = round(now - stage_start, 3)
        stage_start = now
    initial_hashes = source_hashes()
    enabled = a.modules.split(',')
    input_preflight = check_inputs(a.data, a.phase, enabled)
    stage('preflight')
    ctx = Context(a.data, a.phase)
    stage('load_context')
    print(f'Loaded {len(ctx.items)} items in {time.monotonic()-start:.1f}s', flush=True)
    print('Master coverage: ' + json.dumps(ctx.master_coverage), flush=True)
    print('Opening coverage: ' + json.dumps(ctx.opening_coverage), flush=True)
    txns = list(read_csv(ctx.phase_dir / 'bank_transactions.csv.gz'))
    if not txns:
        raise ValueError('Bank transaction input is missing or empty')
    txns.sort(key=lambda t: (t['booking_date'], t['bank_txn_id']))
    stage('load_transactions')
    from .history import enrich_referenced_closed
    history_report = enrich_referenced_closed(ctx, txns)
    stage('historical_references')
    print('Historical references: ' + json.dumps(history_report), flush=True)
    pdf_session = pdf_runtime.configure(ctx, a, Path(a.out).parent / (Path(a.out).name + '.pdf'))
    handlers, document_stats = build_handlers(ctx, enabled)
    stage('initialize_matchers')
    if document_stats is not None:
        print('Document parsing: ' + json.dumps(document_stats), flush=True)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Same parent guarantees the candidate and destination are on one filesystem.
    with tempfile.TemporaryDirectory(prefix=f'.{out.name}.', dir=out.parent) as staging:
        candidate = Path(staging) / out.name
        audit_path = Path(str(candidate) + '.audit.jsonl')
        with output_stream(candidate) as f, audit_path.open('w', encoding='utf-8') as audit:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n')
            w.writeheader()
            def progress(n, total, counts):
                if n % 1000 == 0:
                    print(f'{n}/{total} {dict(counts)} elapsed={time.monotonic()-start:.1f}s', flush=True)
            counts, rejected = execute(ctx, txns, handlers, w, audit, progress)
        stage('inference_and_output')
        generation_seconds = round(time.monotonic() - start, 3)
        pdf_readiness = pdf_session.finish() if pdf_session else None
        validation = validate(candidate, ctx)
        stage('independent_validation')
        official = official_structure(candidate, ctx)
        stage('official_validation')
        input_root = Path(a.data).resolve()
        changed_inputs = [name for name, info in input_preflight['tables'].items()
                          if not (input_root / name).is_file() or digest(input_root / name) != info['sha256']]
        stage('verify_checked_inputs')
        report = {
            'phase': a.phase, 'started_at': started_at, 'run_id': run_id, 'enabled_modules': enabled, 'input_preflight': input_preflight, 'master_coverage': ctx.master_coverage, 'opening_coverage': ctx.opening_coverage,
            'document_parsing': document_stats, 'historical_references': history_report,
            'pdf_facts': getattr(getattr(ctx, 'pdf_facts', None), 'stats', None),
            'pdf_readiness': pdf_readiness,
            'modules': dict(counts), 'rejected_proposals': dict(rejected), 'source_hashes': initial_hashes,
            'seconds': generation_seconds, 'validation': validation,
            'official_structure': official,
            'timings_seconds': timings,
            'seconds_through_validation': round(time.monotonic()-start, 3),
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'checked_inputs_changed': changed_inputs,
            'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
            'audit_sha256': hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            'artifacts': {'submission': out.name, 'audit': out.name + '.audit.jsonl',
                          'report': out.name + '.report.json'},
        }
        report['source_changed_during_run'] = initial_hashes != source_hashes()
        valid = (report['validation']['ok'] and report['official_structure']['ok']
                 and not report['source_changed_during_run'] and not changed_inputs)
        report['publication'] = 'validated' if valid else 'rejected'
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report_path = Path(str(candidate) + '.report.json')
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, indent=2))
        if not valid:
            # Keep diagnostic evidence without replacing any previously valid bundle.
            os.replace(report_path, Path(str(out) + '.failed.report.json'))
            raise SystemExit(1)
        publish_bundle(candidate, out)
    # Dashboard publication is best effort and never invalidates this CSV bundle.
    try:
        from .dashboard_publish import publish_dashboard
        dashboard_status = publish_dashboard(out, a.data, enabled_modules=enabled)
        print('Dashboard: ' + json.dumps(dashboard_status), flush=True)
    except Exception:
        print('Dashboard: publication hook failed; reconciliation bundle remains valid', flush=True)
    return report


if __name__ == '__main__':
    main()
