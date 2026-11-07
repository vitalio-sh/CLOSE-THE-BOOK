#!/usr/bin/env python3
"""Export measured reconciliation results and a bounded, labelled evidence sample.

No reconciliation is performed here. This consumes the submission and its run audit,
streams bank/invoice CSVs, and never reads golden labels. All money stays Decimal
until JSON serialization; bank amounts are aggregated only within each currency.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import heapq
from itertools import groupby
import json
from pathlib import Path
import time

OUTCOMES = {
    'matched': 'At least one invoice or credit-note item is allocated in the submission. Exceptions may still apply.',
    'categorized': 'No invoice is allocated; a non-UNIDENTIFIED accounting category is supplied.',
    'unidentified': 'The submission marks the transaction UNIDENTIFIED, or supplies neither an item nor a category.',
}
MODULES = {
    'batches': 'Uses structured payment-batch records to propose invoice allocations.',
    'documents': 'Uses parsed remittance advice and verified invoice references.',
    'matching': 'Uses invoice references, party, amount, dates and permitted exception rules.',
    'category': 'Assigns a non-invoice accounting category from transaction evidence.',
    'unidentified': 'No accepted proposal was produced by the enabled modules.',
    'unknown': 'No module attribution is available in the run audit.',
}


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def outcome(rows):
    if any(r.get('item_id') for r in rows):
        return 'matched'
    if any(r.get('category') == 'UNIDENTIFIED' or r.get('exception') == 'UNIDENTIFIED' for r in rows):
        return 'unidentified'
    return 'categorized' if any(r.get('category') for r in rows) else 'unidentified'


class Examples:
    """Stable hash reservoirs, stratified by observable outcome/module/exception."""
    def __init__(self, size):
        self.size = size
        self.buckets = defaultdict(list)

    def add(self, identifier, module, state, exceptions, rows):
        score = int.from_bytes(hashlib.sha256(identifier.encode()).digest()[:8], 'big')
        strata = ['all', 'outcome:' + state, 'module:' + module]
        strata += ['exception:' + e for e in exceptions]
        if len(rows) > 1:
            strata.append('multiple_allocations')
        for stratum in strata:
            heap = self.buckets[stratum]
            limit = self.size if stratum == 'all' else min(12, self.size)
            entry = (-score, identifier, rows)
            if len(heap) < limit:
                heapq.heappush(heap, entry)
            elif entry > heap[0]:
                heapq.heapreplace(heap, entry)

    def selected(self):
        result = {}
        # Round robin ensures rare exceptions and every module survive the cap.
        strata = [sorted(v, reverse=True) for k, v in sorted(self.buckets.items()) if k != 'all']
        for rank in range(12):
            for values in strata:
                if rank < len(values) and len(result) < self.size:
                    _, identifier, rows = values[rank]
                    result[identifier] = rows
        for _, identifier, rows in sorted(self.buckets['all'], reverse=True):
            if len(result) >= self.size:
                break
            result[identifier] = rows
        return result


def source_info(path):
    p = Path(path)
    return {'path': str(p), 'bytes': p.stat().st_size,
            'modified_at': datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}


def export_dashboard(submission, data_root, sample_size=200, report_path=None, audit_path=None):
    start = time.monotonic()
    submission, data_root = Path(submission), Path(data_root)
    report_path = Path(report_path or str(submission) + '.report.json')
    audit_path = Path(audit_path or str(submission) + '.audit.jsonl')
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    if report.get('validation', {}).get('ok') is not True or report.get('official_structure', {}).get('ok') is not True:
        raise ValueError('Run report does not certify valid submission and official structure')
    if report.get('source_changed_during_run') is not False:
        raise ValueError('Run report does not certify stable source files')
    if report.get('checked_inputs_changed') or report.get('publication', 'validated') != 'validated':
        raise ValueError('Run report records rejected or changed input artifacts')
    phase = report.get('phase', 'live')
    digest = sha256(submission)
    if report.get('sha256') and digest != report['sha256']:
        raise ValueError('Submission SHA-256 differs from its run report; refusing mixed artifacts')
    audit_digest = sha256(audit_path)
    if report.get('audit_sha256') and audit_digest != report['audit_sha256']:
        raise ValueError('Audit SHA-256 differs from its run report')
    modules = {}
    with audit_path.open() as stream:
        for line in stream:
            r = json.loads(line)
            modules[r['bank_txn_id']] = r['module']
    examples = Examples(sample_size)
    states = {}
    outcome_counts, module_counts = Counter(), Counter()
    exception_txns, exception_rows, categories = Counter(), Counter(), Counter()
    submission_rows = 0
    for identifier, group in groupby(read_csv(submission), lambda r: r['bank_txn_id']):
        if identifier in states:
            raise ValueError('Submission must group all rows for a transaction together')
        rows = list(group)
        state = outcome(rows)
        module = modules.get(identifier, 'unknown')
        exceptions = sorted({r['exception'] for r in rows if r.get('exception')})
        states[identifier] = (state, module)
        submission_rows += len(rows)
        outcome_counts[state] += 1
        module_counts[module] += 1
        exception_txns.update(exceptions)
        exception_rows.update(r['exception'] for r in rows if r.get('exception'))
        categories.update({r['category'] for r in rows if r.get('category')})
        examples.add(identifier, module, state, exceptions, rows)
    selected = examples.selected()
    bank_path = data_root / phase / 'bank_transactions.csv.gz'
    currency_totals = {}
    samples = []
    bank_count = 0
    missing_submission = 0
    bank_seen = set()
    dates = []
    for bank in read_csv(bank_path):
        bank_count += 1
        identifier = bank['bank_txn_id']
        bank_seen.add(identifier)
        dates.append(bank['booking_date'])
        state, module = states.get(identifier, ('missing', 'unknown'))
        missing_submission += state == 'missing'
        currency = bank['currency']
        totals = currency_totals.setdefault(currency, {'currency': currency, 'transactions': 0,
            'inflow': Decimal(0), 'outflow': Decimal(0), 'net': Decimal(0), 'outcomes': Counter()})
        amount = Decimal(bank['amount'])
        totals['transactions'] += 1
        totals['net'] += amount
        totals['inflow' if amount >= 0 else 'outflow'] += abs(amount)
        totals['outcomes'][state] += 1
        if identifier not in selected:
            continue
        rows = selected[identifier]
        samples.append({'id': identifier, 'date': bank['booking_date'], 'value_date': bank.get('value_date', ''),
            'entity': bank['entity'], 'party': bank.get('counterparty_name', ''),
            'currency': currency, 'bank_amount': bank['amount'], 'description': bank.get('text', ''),
            'refs': {key: bank.get(key, '') for key in ('ref1', 'ref2', 'end_to_end_id', 'remittance_info')},
            'original_currency': bank.get('orig_currency', ''), 'original_amount': bank.get('orig_amount', ''),
            'module': module, 'outcome': state,
            'exception_codes': sorted({r['exception'] for r in rows if r.get('exception')}),
            'category': next((r['category'] for r in rows if r.get('category')), ''),
            'bank_source': str(bank_path), 'allocations': [
                {'item_id': r['item_id'], 'amount_applied': r['amount'], 'exception': r.get('exception', '')}
                for r in rows if r.get('item_id')]})
    wanted = {a['item_id'] for txn in samples for a in txn['allocations']}
    items = {}
    item_sources = []
    # Snapshot supplies historical open items without loading the whole catalogue.
    phase_dir = data_root / phase
    current_paths = [phase_dir / 'open_items_at_start.csv.gz'] + [phase_dir / (n + '.csv.gz') for n in ('ar_invoices', 'ap_invoices', 'credit_notes')]
    for path in current_paths:
        if not path.exists():
            continue
        item_sources.append(str(path))
        for row in read_csv(path):
            if row['item_id'] in wanted:
                items[row['item_id']] = {**items.get(row['item_id'], {}), **row, 'source': str(path)}
    remaining = wanted - items.keys()
    history_phases = ('live', 'train') if phase == 'final' else ('train',) if phase != 'train' else ()
    for historic_phase in history_phases:
        for name in ('ar_invoices', 'ap_invoices', 'credit_notes'):
            if not remaining:
                break
            path = data_root / historic_phase / (name + '.csv.gz')
            if not path.exists():
                continue
            item_sources.append(str(path))
            for row in read_csv(path):
                if row['item_id'] in remaining:
                    items[row['item_id']] = {**row, 'source': str(path)}
                    remaining.remove(row['item_id'])
    masters = data_root / 'masters'
    parties = {}
    for name, key in (('customers', 'customer_id'), ('suppliers', 'supplier_id')):
        override = phase_dir / 'masters_final' / (name + '.csv')
        for row in read_csv(override if override.exists() else masters / (name + '.csv')):
            parties[row[key]] = row['name']
    entity_path = phase_dir / 'masters_final' / 'entities.csv'
    entities = {r['entity']: r['legal_name'] for r in read_csv(entity_path if entity_path.exists() else masters / 'entities.csv')}
    missing_items = set()
    for txn in samples:
        txn['entity_name'] = entities.get(txn['entity'], txn['entity'])
        for allocation in txn['allocations']:
            item = items.get(allocation['item_id'], {})
            if not item:
                missing_items.add(allocation['item_id'])
            party_id = item.get('party_id') or item.get('customer_id') or item.get('supplier_id', '')
            allocation.update({'invoice_number': item.get('document_number') or item.get('supplier_invoice_number', ''),
                'item_currency': item.get('currency', ''), 'total_amount': item.get('total_amount', ''),
                'issue_date': item.get('issue_date', ''), 'due_date': item.get('due_date', ''),
                'party': item.get('customer_name') or item.get('supplier_name') or item.get('party_name') or parties.get(party_id, ''),
                'party_id': party_id, 'entity': item.get('entity', ''), 'source': item.get('source', ''),
                'metadata_available': bool(item)})
        txn['sample_tags'] = [txn['module'], txn['outcome']] + txn['exception_codes']
        if len(txn['allocations']) > 1:
            txn['sample_tags'].append('multiple_allocations')
        if len(txn['allocations']) == 1:
            a = txn['allocations'][0]
            if a['item_currency'] == txn['currency'] and a['total_amount'] and abs(Decimal(txn['bank_amount'])) == abs(Decimal(a['total_amount'])):
                txn['sample_tags'].append('bank_amount_equals_invoice_total')
    for totals in currency_totals.values():
        for key in ('inflow', 'outflow', 'net'):
            totals[key] = format(totals[key], '.2f')
        totals['outcomes'] = dict(totals['outcomes'])
    sources = {'submission': source_info(submission), 'report': source_info(report_path),
        'audit': source_info(audit_path), 'bank_transactions': source_info(bank_path), 'invoice_metadata': item_sources}
    pipeline = {key: report.get(key) for key in ('phase', 'started_at', 'finished_at', 'run_id', 'enabled_modules', 'seconds', 'validation', 'official_structure',
        'modules', 'document_parsing', 'pdf_facts', 'historical_references', 'source_hashes',
        'source_changed_during_run', 'rejected_proposals')}
    if not pipeline.get('run_id'):
        finished = datetime.fromtimestamp(report_path.stat().st_mtime, timezone.utc)
        elapsed = max(0, float(report.get('seconds') or 0))
        pipeline.update({'run_id': 'legacy-' + digest,
            'started_at': (finished - timedelta(seconds=elapsed)).isoformat(),
            'finished_at': finished.isoformat(),
            'timestamp_source': 'legacy report modification time minus recorded run duration'})
    else:
        pipeline['timestamp_source'] = 'runner UTC timestamps'
    if pipeline.get('enabled_modules') is None and all(k in (report.get('modules') or {}) for k in ('batches', 'documents', 'matching')):
        pipeline['enabled_modules'] = ['batches', 'documents', 'matching']
    pipeline.update({'sha256': digest, 'hash_verified': report.get('sha256') == digest,
        'audit_sha256': audit_digest, 'audit_hash_verified': report.get('audit_sha256') == audit_digest,
        'live_accuracy': None, 'accuracy_note': 'No hidden LIVE labels were read. Coverage and format validation are not accuracy scores.'})
    validation = report['validation']
    if (missing_submission or states.keys() - bank_seen or states.keys() - modules.keys()
            or dict(module_counts) != report.get('modules') or not bank_count
            or validation.get('transactions') != bank_count or validation.get('covered') != bank_count
            or validation.get('missing') != 0 or validation.get('rows') != submission_rows):
        raise ValueError('Export coverage/counts differ from validated run artifacts')
    if sha256(submission) != digest or sha256(audit_path) != audit_digest or report_path.read_bytes() != report_bytes:
        raise ValueError('Run artifacts changed during export; retry after publication completes')
    return {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'sources': sources, 'pipeline': pipeline,
        'summary': {'transactions': bank_count, 'submission_transactions': len(states), 'submission_rows': submission_rows,
            'outcomes': {key: outcome_counts[key] for key in OUTCOMES},
            'exceptions': [{'code': k, 'transactions': exception_txns[k], 'allocations': exception_rows[k]} for k in sorted(exception_txns)],
            'categories': [{'category': k, 'transactions': v} for k, v in categories.most_common()],
            'currencies': sorted(currency_totals.values(), key=lambda r: r['currency']),
            'modules': [{'module': k, 'transactions': v} for k, v in module_counts.most_common()],
            'date_from': min(dates) if dates else None, 'date_to': max(dates) if dates else None},
        'sample': {'size': len(samples), 'population': bank_count, 'requested_size': sample_size,
            'selection': 'Deterministic stratified examples by module, outcome and exception; search covers this sample only.',
            'missing_invoice_metadata': len(missing_items)},
        'export_checks': {'missing_submission_transactions': missing_submission,
            'submission_ids_absent_from_bank': len(states.keys() - bank_seen),
            'audit_ids_missing': len(states.keys() - modules.keys()),
            'module_counts_match_report': dict(module_counts) == report.get('modules'),
            'seconds': round(time.monotonic() - start, 2)},
        'definitions': {'outcomes': OUTCOMES, 'module_descriptions': MODULES,
            'amounts': 'Bank inflow/outflow/net are grouped by bank currency. Allocation amount_applied and invoice totals are in item_currency; they are never summed across currencies.',
            'exceptions': 'Counts overlap: one transaction can have several exception codes. Allocations counts submission rows carrying the code, including no-item category rows.',
            'sample_tags': 'Tags describe observable output or amount equality, not a claimed matching algorithm or confidence.'},
        'transactions': sorted(samples, key=lambda r: (r['date'], r['id']))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submission', default=None)
    parser.add_argument('--data', default='data/workspace')
    parser.add_argument('--out', default='dashboard/data.json')
    parser.add_argument('--sample-size', type=int, default=200)
    parser.add_argument('--report')
    parser.add_argument('--audit')
    args = parser.parse_args()
    if args.sample_size < 1:
        parser.error('--sample-size must be positive')
    submission = args.submission
    if submission is None:
        submission = 'outputs/submission_live_release.csv.gz'
        if not Path(submission).exists():
            submission = 'outputs/submission_live_ready.csv.gz'
    result = export_dashboard(submission, args.data, args.sample_size, args.report, args.audit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + '.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
    temporary.replace(out)
    print(json.dumps({'output': str(out), 'bytes': out.stat().st_size, 'transactions': result['summary']['transactions'],
        'sample': result['sample'], 'export_checks': result['export_checks']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
