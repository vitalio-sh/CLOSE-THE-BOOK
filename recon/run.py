"""Run reconciliation and publish a validated, reproducible artifact bundle."""
import argparse
from collections import Counter
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
import tempfile
import time

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
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='data/workspace')
    p.add_argument('--phase', choices=['sample', 'train', 'live', 'final'], default='sample')
    p.add_argument('--out', required=True)
    p.add_argument('--modules', default='batches,documents,matching')
    p.add_argument('--pdf-facts', action='store_true', help='Enable budgeted optional invoice-PDF facts')
    p.add_argument('--pdf-max-documents', type=int, default=100)
    p.add_argument('--pdf-budget-seconds', type=float, default=30)
    a = p.parse_args(argv)
    start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid4())
    initial_hashes = source_hashes()
    ctx = Context(a.data, a.phase)
    print(f'Loaded {len(ctx.items)} items in {time.monotonic()-start:.1f}s', flush=True)
    print('Master coverage: ' + json.dumps(ctx.master_coverage), flush=True)
    print('Opening coverage: ' + json.dumps(ctx.opening_coverage), flush=True)
    txns = list(read_csv(ctx.phase_dir / 'bank_transactions.csv.gz'))
    if not txns:
        raise ValueError('Bank transaction input is missing or empty')
    txns.sort(key=lambda t: (t['booking_date'], t['bank_txn_id']))
    from .history import enrich_referenced_closed
    history_report = enrich_referenced_closed(ctx, txns)
    print('Historical references: ' + json.dumps(history_report), flush=True)
    if a.pdf_facts:
        from .pdf_facts import PdfFacts
        ctx.pdf_facts = PdfFacts(ctx, max_documents=a.pdf_max_documents, max_seconds=a.pdf_budget_seconds)
    enabled = a.modules.split(',')
    handlers = []
    document_stats = None
    if 'batches' in enabled:
        from .batches import BatchMatcher
        handlers.append(('batches', BatchMatcher(ctx).reconcile))
    if 'documents' in enabled:
        from .documents import DocumentMatcher
        document_matcher = DocumentMatcher(ctx)
        document_stats = document_matcher.stats
        print('Document parsing: ' + json.dumps(document_stats), flush=True)
        handlers.append(('documents', document_matcher.reconcile))
    if 'matching' in enabled:
        from .matching import reconcile, classify
        handlers.append(('category', lambda t: classify(t, ctx)))
        handlers.append(('matching', lambda t: reconcile(t, ctx)))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    rejected = Counter()
    # Same parent guarantees the candidate and destination are on one filesystem.
    with tempfile.TemporaryDirectory(prefix=f'.{out.name}.', dir=out.parent) as staging:
        candidate = Path(staging) / out.name
        audit_path = Path(str(candidate) + '.audit.jsonl')
        with output_stream(candidate) as f, audit_path.open('w', encoding='utf-8') as audit:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n')
            w.writeheader()
            for i, txn in enumerate(txns):
                rows = None
                source = 'unidentified'
                for name, handler in handlers:
                    rows = handler(txn)
                    if rows and not ctx.proposal_valid(txn, rows):
                        rejected[name] += 1
                        rows = None
                    if rows:
                        source = name
                        break
                if not rows:
                    rows = [dict(zip(FIELDS, [txn['bank_txn_id'], '', '', 'UNIDENTIFIED', 'UNIDENTIFIED']))]
                rows = [{k: str(r.get(k, '') or '') for k in FIELDS} for r in rows]
                w.writerows(rows)
                ctx.apply(txn, rows)
                counts[source] += 1
                audit.write(json.dumps({'bank_txn_id': txn['bank_txn_id'], 'module': source,
                                        'items': [r['item_id'] for r in rows]}) + '\n')
                if (i+1) % 1000 == 0:
                    print(f'{i+1}/{len(txns)} {dict(counts)} elapsed={time.monotonic()-start:.1f}s', flush=True)
        report = {
            'phase': a.phase, 'started_at': started_at, 'run_id': run_id, 'enabled_modules': enabled, 'master_coverage': ctx.master_coverage, 'opening_coverage': ctx.opening_coverage,
            'document_parsing': document_stats, 'historical_references': history_report,
            'pdf_facts': getattr(getattr(ctx, 'pdf_facts', None), 'stats', None),
            'modules': dict(counts), 'rejected_proposals': dict(rejected), 'source_hashes': initial_hashes,
            'seconds': round(time.monotonic()-start, 2), 'validation': validate(candidate, ctx),
            'official_structure': official_structure(candidate, ctx),
            'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
            'audit_sha256': hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            'artifacts': {'submission': out.name, 'audit': out.name + '.audit.jsonl',
                          'report': out.name + '.report.json'},
        }
        report['source_changed_during_run'] = initial_hashes != source_hashes()
        valid = report['validation']['ok'] and report['official_structure']['ok'] and not report['source_changed_during_run']
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
