"""Reject incomplete inputs before inference; record the exact tables checked.

This is an input contract, not a claim that matching or accounting is correct.
SAMPLE intentionally has no authoritative opening snapshot.
"""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def check_inputs(data_root, phase, modules=('batches', 'documents', 'matching')):
    root = Path(data_root).resolve()
    folder = root / phase
    report = {'phase': phase, 'tables': {}, 'warnings': []}
    errors = []

    def table(path, required, key=None, amounts=(), dates=(), nonempty=False):
        relative = str(path.relative_to(root))
        if not path.is_file():
            errors.append(f'Missing required input: {relative}')
            return
        count = 0
        seen = set()
        try:
            with (gzip.open if path.suffix == '.gz' else open)(path, 'rt', encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames or []
                missing = set(required) - set(fields)
                if missing or len(fields) != len(set(fields)):
                    errors.append(f'{relative}: missing columns {sorted(missing)} or duplicate header')
                    return
                for count, row in enumerate(reader, 1):
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError(f'row {count}: wrong field count')
                    if key:
                        identifier = row[key]
                        if not identifier or identifier in seen:
                            raise ValueError(f'row {count}: blank or duplicate {key} {identifier!r}')
                        seen.add(identifier)
                    for column in amounts:
                        if not Decimal(row[column]).is_finite():
                            raise ValueError(f'row {count}: nonfinite {column}')
                    for column in dates:
                        date.fromisoformat(row[column])
            if nonempty and not count:
                raise ValueError('table is empty')
            report['tables'][relative] = {'rows': count, 'bytes': path.stat().st_size, 'sha256': digest(path)}
        except (OSError, EOFError, ValueError, InvalidOperation, csv.Error) as exc:
            errors.append(f'{relative}: {exc}')

    active = bool([m for m in modules if m])
    bank_columns = {'bank_txn_id', 'booking_date', 'account_id'}
    if active:
        bank_columns |= {'entity', 'amount', 'currency'}
    table(folder / 'bank_transactions.csv.gz', bank_columns, 'bank_txn_id',
          amounts=('amount',) if active else (), dates=('booking_date',), nonempty=True)
    if active:
        for filename, party, number in (
            ('ar_invoices', 'customer_id', 'document_number'),
            ('ap_invoices', 'supplier_id', 'supplier_invoice_number'),
            ('credit_notes', 'party_id', 'document_number'),
        ):
            table(folder / (filename + '.csv.gz'),
                  {'item_id', 'entity', party, number, 'issue_date', 'currency', 'total_amount'},
                  'item_id', amounts=('total_amount',), dates=('issue_date',))
        if phase in ('live', 'final'):
            table(folder / 'open_items_at_start.csv.gz',
                  {'item_id', 'document_number', 'party_id', 'total_amount', 'open_amount'},
                  'item_id', amounts=('total_amount', 'open_amount'))
        else:
            report['warnings'].append('No authoritative opening snapshot required for SAMPLE/TRAIN; this limits balance evaluation.')
        for filename, key in (('customers', 'customer_id'), ('suppliers', 'supplier_id'),
                              ('bank_accounts', 'account_id'), ('categories', 'category'),
                              ('payment_terms', 'code')):
            override = folder / 'masters_final' / (filename + '.csv')
            path = override if phase == 'final' and override.is_file() else root / 'masters' / (filename + '.csv')
            table(path, {key}, key, nonempty=True)
        history_phases = ('train', 'live') if phase == 'final' else ('train',) if phase == 'live' else ()
        for earlier in history_phases:
            for name in ('ar_invoices', 'ap_invoices', 'credit_notes'):
                path = root / earlier / (name + '.csv.gz')
                if not path.is_file():
                    errors.append(f'Missing required historical table: {earlier}/{name}.csv.gz')
        if 'documents' in modules:
            index = folder / 'remittance_advices/index.csv'
            table(index, {'file'}, 'file')
            if index.is_file():
                with index.open(encoding='utf-8-sig', newline='') as stream:
                    for row in csv.DictReader(stream):
                        filename = row.get('file')
                        if filename and not (index.parent / filename).is_file():
                            errors.append(f'Advice listed in index is missing: {filename}')
                            if len(errors) >= 25:
                                break
    if errors:
        raise ValueError('Input preflight failed:\n' + '\n'.join(errors[:25]))
    report['ok'] = True
    return report
