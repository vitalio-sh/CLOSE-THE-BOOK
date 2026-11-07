#!/usr/bin/env python3
"""Evaluate frozen code on a production-shaped temporal TRAIN window.

Oracle allocations strictly before the window reconstruct opening balances.
Target-window label values never affect inference; scoring uses them only after
the prediction output is closed.
This is not a pure end-to-end holdout: non-bank cancellations and historical
versions of master data are unavailable.
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import csv
from datetime import date, timedelta
from decimal import Decimal
import gc
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import re
import resource
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS = ['PARTIAL','OVERPAYMENT','SHORT_DISCOUNT','SHORT_BANK_CHARGES',
              'SHORT_WITHHOLDING','SHORT_DISPUTE','DUPLICATE_PAYMENT',
              'UNIDENTIFIED','RETURNED','FX_DIFFERENCE']


def read(path):
    path = Path(path)
    with (gzip.open if path.suffix == '.gz' else open)(path, 'rt', encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def decompressed_hash(path):
    digest = hashlib.sha256()
    with gzip.open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def input_hashes(root):
    return {str(p.relative_to(root)): file_hash(p) for p in sorted(root.rglob('*')) if p.is_file()}


def prior_balance(total, operations):
    """Mirror Context.apply without retaining full historical invoice dictionaries."""
    balance = Decimal(total)
    for amount, exception in operations:
        amount = Decimal(amount)
        if exception == 'RETURNED':
            balance += amount
        elif exception != 'DUPLICATE_PAYMENT':
            balance = max(Decimal(0), balance - amount)
    return balance


def prepare_inputs(source, input_root, start, end):
    """Only prior labels enter this builder; phase documents are selected by date."""
    train, phase, history = source/'train', input_root/'live', input_root/'train'
    phase.mkdir(parents=True)
    history.mkdir()
    shutil.copytree(source/'masters', input_root/'masters', ignore=shutil.ignore_patterns('._*'))
    prior_txns, target = {}, []
    for txn in read(train/'bank_transactions.csv.gz'):
        day = txn['booking_date']
        if day < start:
            prior_txns[txn['bank_txn_id']] = day
        elif day <= end:
            target.append(txn)
    target.sort(key=lambda t: (t['booking_date'], t['bank_txn_id']))
    if not target:
        raise ValueError('Window has no transactions')
    write_csv(phase/'bank_transactions.csv.gz', target, list(target[0]))
    operations = defaultdict(list)
    previous, prior_count = ('', ''), 0
    for row in read(train/'golden_allocations.csv.gz'):
        day = prior_txns.get(row['bank_txn_id'])
        if day is None:
            continue
        key = (day, row['bank_txn_id'])
        if key < previous:
            raise ValueError('Prior golden is not chronological')
        previous = key
        if row['item_id']:
            operations[row['item_id']].append((row['amount'], row['exception']))
            prior_count += 1
    del prior_txns
    # Uniform snapshot schema retains metadata needed by the normal Context loader.
    opening_fields = ['item_id','entity','party_id','document_number','issue_date',
                      'due_date','currency','total_amount','open_amount','payment_terms']
    opening_count = 0
    with gzip.open(phase/'open_items_at_start.csv.gz', 'wt', newline='') as snapshot:
        opening_writer = csv.DictWriter(snapshot, fieldnames=opening_fields, lineterminator='\n')
        opening_writer.writeheader()
        for name in ('ar_invoices', 'ap_invoices', 'credit_notes'):
            records = read(train/(name+'.csv.gz'))
            first = next(records, None)
            if first is None:
                with gzip.open(train/(name+'.csv.gz'), 'rt', encoding='utf-8-sig', newline='') as header_stream:
                    fields = next(csv.reader(header_stream))
            else:
                fields = list(first)
            with ExitStack() as stack:
                old_stream = stack.enter_context(gzip.open(history/(name+'.csv.gz'), 'wt', newline=''))
                new_stream = stack.enter_context(gzip.open(phase/(name+'.csv.gz'), 'wt', newline=''))
                old = csv.DictWriter(old_stream, fieldnames=fields, lineterminator='\n')
                new = csv.DictWriter(new_stream, fieldnames=fields, lineterminator='\n')
                old.writeheader(); new.writeheader()
                import itertools
                for row in itertools.chain([first] if first is not None else [], records):
                    issued = row['issue_date']
                    if issued > end:
                        continue
                    if issued >= start:
                        new.writerow(row)
                        continue
                    old.writerow(row)
                    balance = prior_balance(row['total_amount'], operations.pop(row['item_id'], ()))
                    if balance > 0:
                        record = {key: row.get(key, '') for key in opening_fields}
                        record.update(open_amount=str(balance),
                                      party_id=row.get('party_id') or row.get('customer_id') or row.get('supplier_id',''),
                                      document_number=row.get('document_number') or row.get('supplier_invoice_number',''))
                        opening_writer.writerow(record)
                        opening_count += 1
    unmatched_prior_ids = len(operations)
    del operations
    gc.collect()
    for folder in ('payment_runs','direct_debit_batches','card_settlements','cheque_deposits'):
        (phase/folder).mkdir()
        for path in sorted((train/folder).glob('*.csv')):
            if path.name.startswith('._'):
                continue
            months = re.findall(r'(?:^|_)(20\d{4})(?=_|\.)', path.name)
            if months and months[0] <= end[:7].replace('-', ''):
                shutil.copy2(path, phase/folder/path.name)
    advice_root = phase/'remittance_advices'
    advice_root.mkdir()
    earliest = (date.fromisoformat(start)-timedelta(days=31)).isoformat()
    metadata = [r for r in read(train/'remittance_advices/index.csv') if earliest <= r['received_date'] <= end]
    with (advice_root/'index.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(metadata[0]) if metadata else ['file','entity','payer_name','received_date','format'])
        writer.writeheader(); writer.writerows(metadata)
    for meta in metadata:
        dest = advice_root/meta['file']; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(train/'remittance_advices'/meta['file'], dest)
    return {'start': start, 'end': end, 'prior_allocations': prior_count,
            'opening_items': opening_count, 'unmatched_prior_item_ids': unmatched_prior_ids,
            'target_transactions': len(target), 'advice_received_cutoff': end,
            'assumption': 'Oracle prior bank allocations; non-bank cancellations not reconstructed; later static masters.'}



def prepare_pdf_inputs(source, input_root, start, end):
    """Freeze date-bounded TRAIN PDF sources without reading allocation labels."""
    root = (source/'train/pdfs').resolve()
    index = root/'index.csv'
    if not index.is_file():
        raise ValueError('PDF input preparation requires TRAIN pdfs/index.csv')
    original_hash = file_hash(index)
    with index.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        if not fields or not {'file','document_date'}.issubset(fields):
            raise ValueError('PDF index requires file and document_date')
        rows = {'train':[], 'live':[]}
        copied = 0
        for row in reader:
            day = row['document_date']
            date.fromisoformat(day)
            if day > end:
                continue
            relative = Path(row['file'])
            path = (root/relative).resolve()
            if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root):
                raise ValueError('PDF index path escapes source directory')
            phase = 'train' if day < start else 'live'
            dest = input_root/phase/'pdfs'/relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            before = file_hash(path)
            shutil.copy2(path, dest)
            if before != file_hash(dest) or before != file_hash(path):
                raise ValueError('PDF source changed during freezing')
            rows[phase].append(row)
            copied += 1
    if file_hash(index) != original_hash:
        raise ValueError('PDF index changed during freezing')
    for phase, records in rows.items():
        dest = input_root/phase/'pdfs/index.csv'
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
            writer.writeheader();writer.writerows(records)
    return {'pdf_inputs_prepared':True, 'pdf_document_cutoff':end,
            'pdf_source_index_sha256':original_hash, 'pdf_documents_copied':copied}


def error_diagnostics(path, golden):
    """Post-inference diagnostics only; never available to matching handlers."""
    from itertools import groupby
    diagnostics = {}
    for tid, group in groupby(read(path), key=lambda row: row['bank_txn_id']):
        rows = list(group)
        expected, category, exception, segment = golden.txns[tid]
        actual = {row['item_id']: row for row in rows if row['item_id']}
        issues = []
        if set(expected)-set(actual):
            issues.append('missing_pairs')
        if set(actual)-set(expected):
            issues.append('spurious_pairs')
        if any(abs(float(actual[i]['amount'])-amount) > max(.01, .005*abs(amount))+1e-9
               for i, (amount, _) in expected.items() if i in actual):
            issues.append('wrong_amount')
        if any(actual[i]['exception'] != exc for i, (_, exc) in expected.items() if i in actual):
            issues.append('wrong_exception')
        if not expected and (len(rows) != 1 or rows[0]['category'] != category or rows[0]['exception'] != exception):
            issues.append('wrong_category_or_exception')
        bucket = diagnostics.setdefault(segment, {'transactions': 0, 'transactions_with_errors': 0, 'issues': {}, 'examples': []})
        bucket['transactions'] += 1
        if issues:
            bucket['transactions_with_errors'] += 1
            for issue in issues:
                bucket['issues'][issue] = bucket['issues'].get(issue, 0)+1
            if len(bucket['examples']) < 10:
                bucket['examples'].append({'bank_txn_id': tid, 'issues': issues,
                                           'expected': expected, 'category': category,
                                           'exception': exception, 'actual': rows})
    return diagnostics


def main(argv=None, handler_hook=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default=str(ROOT/'data/workspace'))
    parser.add_argument('--start', default='2024-10-01')
    parser.add_argument('--end', default='2024-10-31')
    parser.add_argument('--out', required=True)
    parser.add_argument('--code-root', default=str(ROOT), help='Repository/snapshot containing recon/ to evaluate')
    parser.add_argument('--prepared-input', help='Previously frozen input/ directory; identical inputs for A/B')
    parser.add_argument('--prepare-pdf-inputs', action='store_true', help='Freeze PDF evidence even for a PDF-disabled baseline')
    sys.path.insert(0, str(ROOT))
    from recon import pdf_runtime
    pdf_runtime.add_options(parser)
    args = parser.parse_args(argv)
    pdf_runtime.check_options(args)
    date.fromisoformat(args.start); date.fromisoformat(args.end)
    if args.start > args.end:
        raise ValueError('start must precede end')
    started = time.monotonic()
    dest = Path(args.out).resolve()
    dest.mkdir(parents=True, exist_ok=False)
    source = Path(args.data).resolve()
    frozen = dest/'source_snapshot'
    package = frozen/'frozen_recon'
    package.mkdir(parents=True)
    code_root = Path(args.code_root).resolve()
    hashes = {}
    for path in sorted((code_root/'recon').glob('*.py')):
        shutil.copy2(path, package/path.name)
        hashes['recon/'+path.name] = file_hash(path)
    # Older production snapshots predate extraction of the exact same inference loop.
    engine_adapter = not (package/'engine.py').exists()
    if engine_adapter:
        shutil.copy2(ROOT/'recon/engine.py', package/'engine.py')
        hashes['recon/engine.py (shared baseline adapter)'] = file_hash(package/'engine.py')
    for path in [ROOT/'vendor/starter/bankrec_score.py', Path(__file__)]:
        shutil.copy2(path, frozen/path.name)
        hashes[str(path.relative_to(ROOT))] = file_hash(path)
    (dest/'source_hashes.json').write_text(json.dumps(hashes, indent=2)+'\n')
    if args.prepared_input:
        input_root = Path(args.prepared_input).resolve()
        manifest_path = input_root.parent/'input_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if (manifest['preparation']['start'], manifest['preparation']['end']) != (args.start, args.end):
            raise ValueError('Prepared input dates do not match requested window')
        if input_hashes(input_root) != manifest['files']:
            raise ValueError('Prepared input has changed since it was frozen')
        if (args.pdf_facts or args.prepare_pdf_inputs) and not manifest['preparation'].get('pdf_inputs_prepared'):
            raise ValueError('Prepared inputs lack PDF evidence; create a new set with --prepare-pdf-inputs')
        (dest/'input_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    else:
        input_root = dest/'input'
        preparation = prepare_inputs(source, input_root, args.start, args.end)
        if args.pdf_facts or args.prepare_pdf_inputs:
            preparation.update(prepare_pdf_inputs(source, input_root, args.start, args.end))
        manifest = {'preparation': preparation, 'files': input_hashes(input_root)}
        (dest/'input_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print('Frozen temporal inputs ready', manifest['preparation'], flush=True)
    sys.path.insert(0, str(frozen))
    # main is normally a fresh process, but removing package state also supports tests.
    for name in list(sys.modules):
        if name == 'frozen_recon' or name.startswith('frozen_recon.') or name == 'bankrec_score':
            del sys.modules[name]
    C = importlib.import_module('frozen_recon.context')
    E = importlib.import_module('frozen_recon.engine')
    H = importlib.import_module('frozen_recon.history')
    V = importlib.import_module('frozen_recon.validate')
    R = importlib.import_module('frozen_recon.run')
    S = importlib.import_module('bankrec_score')
    stage_started = time.monotonic()
    ctx = C.Context(input_root, 'live')
    stage_seconds = {'context': round(time.monotonic()-stage_started, 3)}
    print(f'Loaded {len(ctx.items):,} items in {stage_seconds["context"]}s', flush=True)
    txns = list(C.read_csv(ctx.phase_dir/'bank_transactions.csv.gz'))
    history_report = H.enrich_referenced_closed(ctx, txns)
    print('Historical references:', history_report, flush=True)
    stage_started = time.monotonic()
    P = importlib.import_module('frozen_recon.pdf_runtime') if args.pdf_facts else None
    pdf_session = P.configure(ctx, args, dest/'pdf') if P else None
    handlers, document_stats = E.build_handlers(ctx)
    extension_report = handler_hook(ctx, handlers) if handler_hook is not None else None
    stage_seconds['handlers'] = round(time.monotonic()-stage_started, 3)
    print('Handlers ready:', stage_seconds, flush=True)
    inference_start = time.monotonic()
    out = dest/'submission.csv.gz'
    with R.output_stream(out) as stream, (dest/'audit.jsonl').open('w') as audit:
        writer = csv.DictWriter(stream, fieldnames=V.FIELDS, lineterminator='\n')
        writer.writeheader()
        def progress(n, total, counts):
            if n % 5000 == 0:
                print(f'{n}/{total} elapsed={time.monotonic()-started:.1f}s', flush=True)
        counts, rejected = E.execute(ctx, txns, handlers, writer, audit, progress)
    inference_seconds = time.monotonic()-inference_start
    pdf_readiness = pdf_session.finish() if pdf_session else None
    validation = V.validate(out, ctx)
    # This is the first point target labels are materialized or passed to a scorer.
    target_ids = {txn['bank_txn_id'] for txn in txns}
    label_file = dest/'target_golden.csv.gz'
    with gzip.open(label_file, 'wt', newline='') as stream:
        writer = None
        for row in read(source/'train/golden_allocations.csv.gz'):
            if row['bank_txn_id'] not in target_ids:
                continue
            if writer is None:
                writer = csv.DictWriter(stream, fieldnames=list(row), lineterminator='\n'); writer.writeheader()
            writer.writerow(row)
    golden = S.load_golden(str(label_file), str(ctx.phase_dir/'bank_transactions.csv.gz'), EXCEPTIONS, sorted(ctx.categories))
    score = S.score_submission(golden, out.read_bytes(), out.name)
    (dest/'error_diagnostics.json').write_text(json.dumps(error_diagnostics(out, golden), indent=2)+'\n')
    report = {'start': args.start, 'end': args.end, **manifest['preparation'],
              'target_labels_used_only_after_predictions': True,
              'production_engine_shared': True, 'baseline_engine_adapter': engine_adapter,
              'input_manifest_sha256': file_hash(dest/'input_manifest.json'),
              'input_root': str(input_root), 'input_items': len(ctx.items),
              'historical_references': history_report, 'document_parsing': document_stats,
              'pdf_readiness': pdf_readiness,
              'pdf_facts': getattr(getattr(ctx, 'pdf_facts', None), 'stats', None),
              'modules': dict(counts), 'rejected_proposals': dict(rejected),
              'seconds_total': round(time.monotonic()-started, 2),
              'seconds_inference': round(inference_seconds, 2), 'stage_seconds': stage_seconds,
              'target_golden_sha256': file_hash(label_file),
              'target_golden_content_sha256': decompressed_hash(label_file),
              'max_rss_bytes_macos': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'validation': validation, 'score': score, 'source_hashes': hashes,
              'output_sha256': file_hash(out)}
    if extension_report is not None:
        report['experimental_extension'] = extension_report
    (dest/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({key: value for key, value in report.items() if key not in ('source_hashes', 'score')}, indent=2), flush=True)
    print(json.dumps({key: value for key, value in score.items() if key != 'segments'}, indent=2), flush=True)
    if not validation['ok'] or not score['ok']:
        raise SystemExit(1)
    return report


if __name__ == '__main__':
    main()
