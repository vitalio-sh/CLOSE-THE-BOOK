"""Recover explicitly referenced, already-closed historical items without labels.

An opening snapshot is authoritative about which OLD items remain open. Historical
items absent from it must never be reintroduced as payable balances. This helper
only restores their identity/metadata for duplicate and return handling.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import re
import time

from .context import read_csv, norm

FIELDS = ('text', 'ref1', 'ref2', 'remittance_info', 'end_to_end_id')
RECENT_CLOSED_DAYS = 120
INVOICE_HINT = re.compile(r'(?:FRA(?:S)?\.?|FACTURA(?:S)?|FACT|INVOICE(?:S)?|INV|RECHNUNG|RE-NR|RE|FAC|FATURA)\s*[:.#-]?\s*$', re.I)


def reference_tokens(txn):
    """Exact printed tokens only: no fuzzy, substring, amount, or ID guessing.

Mixed alphabetic/numeric printed references need at least five normalized
characters. Numeric-only references need an invoice cue or >=6 digits; short
supplier invoice numbers are accepted after e.g. FRA or INVOICE.
    """
    result = set()
    for field in FIELDS:
        text = str(txn.get(field) or '')
        # SWIFT field delimiters are transport syntax, not part of invoice numbers.
        text = re.sub(r'/(?:ORDP|REMI|EREF|REF|MOTIF|CHGS|BENM)/', ' ', text, flags=re.I)
        for match in re.finditer(r'[A-Za-z0-9][A-Za-z0-9._/-]*', text):
            token = norm(match[0])
            if len(token) < 4 or not any(c.isdigit() for c in token):
                continue
            if token.isdigit():
                if len(token) < 6 and not INVOICE_HINT.search(text[max(0, match.start()-30):match.start()]):
                    continue
            elif len(token) < 5:
                continue
            result.add(token)
    return result


def enrich_referenced_closed(ctx, txns):
    """Add referenced old identities with Decimal(0) balance; return audit counts.

Only LIVE/FINAL with an opening snapshot qualify. Existing items, balances and
indices are preserved. Call once before constructing cached matcher indices.
    """
    start = time.monotonic()
    result = {'added_count': 0, 'scanned_rows': 0, 'reference_tokens': 0,
              'seconds': 0.0, 'skipped_reason': None}
    phase_dir = Path(ctx.phase_dir)
    if phase_dir.name not in ('live', 'final'):
        result['skipped_reason'] = 'Only LIVE/FINAL have an authoritative opening snapshot'
        return result
    if not (phase_dir / 'open_items_at_start.csv.gz').exists():
        result['skipped_reason'] = 'Missing authoritative opening snapshot'
        return result
    refs = defaultdict(set)
    first_date = None
    for txn in txns:
        booking = txn.get('booking_date')
        if booking and (first_date is None or booking < first_date):
            first_date = booking
        for token in reference_tokens(txn):
            refs[token].add(txn.get('entity', ''))
    if first_date is not None:
        result['batch_reference_tokens'] = _batch_references(ctx, phase_dir, first_date, refs)
        result['reversal_reference_tokens'] = _reversal_references(ctx, phase_dir, first_date, refs, txns)
        result['repeated_transfer_tokens'] = _repeated_transfer_references(ctx, phase_dir, first_date, txns, refs)
    result['reference_tokens'] = len(refs)
    if first_date is None:
        result['skipped_reason'] = 'No dated transactions'
        return result
    seen_roots = set()
    # Recently closed invoices (issued shortly before the phase, absent from the
    # authoritative opening snapshot) by party: the catalogue for duplicate payments.
    try:
        recent_floor = (date.fromisoformat(first_date) - timedelta(days=RECENT_CLOSED_DAYS)).isoformat()
    except ValueError:
        recent_floor = first_date
    recent_closed = defaultdict(list)
    ctx.recent_closed = recent_closed
    for folder in getattr(ctx, 'history_dirs', ()):
        folder = Path(folder)
        resolved = folder.resolve()
        if resolved == phase_dir.resolve() or resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        for filename in ('ar_invoices.csv.gz', 'ap_invoices.csv.gz', 'credit_notes.csv.gz'):
            for record in read_csv(folder / filename):
                result['scanned_rows'] += 1
                iid = record['item_id']
                if iid in ctx.items:
                    continue
                if filename != 'credit_notes.csv.gz' and recent_floor <= record.get('issue_date', '') < first_date:
                    party = record.get('customer_id') or record.get('supplier_id') or ''
                    if party:
                        recent_closed[party].append(record)
                number = record.get('document_number') or record.get('supplier_invoice_number', '')
                key = norm(number)
                entities = refs.get(key)
                if not entities:
                    continue
                # Absence from opening proves closed only for documents issued before phase.
                issue_date = record.get('issue_date', '')
                if not issue_date or issue_date >= first_date:
                    continue
                entity = record.get('entity', '')
                factory = iid.startswith('AP') and entity in ('ES01', 'ES02', 'PT01') and 'ES00' in entities
                if entity not in entities and '' not in entities and not factory:
                    continue
                restored = dict(record, open_amount=Decimal('0'), historical_closed=True)
                ctx.add_item(restored)
                ctx.by_number[key].append(iid)
                ctx.by_party[ctx.items[iid]['party_id']].append(iid)
                result['added_count'] += 1
    result['recent_closed_parties'] = len(recent_closed)
    result['seconds'] = round(time.monotonic() - start, 3)
    return result

def _batch_references(ctx, phase_dir, first_date, refs):
    """Invoice numbers printed in direct-debit batch files of the phase and the six months before.

    A returned collection re-opens the printed invoice. When that invoice was collected
    before the phase began it is closed and absent from the opening snapshot, so its
    identity is restored at zero balance like a bank-text reference. Entity comes from
    the batch file name (``AU01_NSW_202507.csv``).
    """
    try:
        floor = (date.fromisoformat(first_date).replace(day=1) - timedelta(days=183)).strftime('%Y%m')
    except ValueError:
        return 0
    added = 0
    roots = [phase_dir] + [Path(p) for p in getattr(ctx, 'history_dirs', ())]
    seen = set()
    for root in roots:
        for path in sorted((Path(root) / 'direct_debit_batches').glob('*.csv')):
            if path.name.startswith('._') or path.name in seen:
                continue
            month = re.search(r'_(\d{6})(?:_\d+)?\.csv$', path.name)
            # The floor applies to every folder: evaluation windows ship all earlier
            # batch files inside the phase folder, LIVE/FINAL only in-phase files.
            if not month or month.group(1) < floor:
                continue
            seen.add(path.name)
            entity = path.name.split('_')[0]
            for row in read_csv(path):
                token = norm(row.get('remittance_info', ''))
                if len(token) >= 5 and any(c.isdigit() for c in token) and entity not in refs[token]:
                    refs[token].add(entity)
                    added += 1
    return added


CHARGEBACK = re.compile(r'CHARGEBACK|CONTRACARGO|RUECKBELASTUNG|RETROCESION TARJETA|RIADDEBITO', re.I)
RETURN_TEXT = re.compile(r'(?:RETURN(?:ED)?|DEVOLUCION TRANSFERENCIA|RUECKUEBERWEISUNG|RETOUR|REJECT(?:ED)?)\W+[A-Z]{2}\d{2}\b', re.I)
MASKED_CARD = re.compile(r'\*{2,}\s*(\d{4})\b')


def _reversal_references(ctx, phase_dir, first_date, refs, txns):
    """Documents behind a returned cheque, a card chargeback or a returned supplier transfer.

    POLICIES section 3 (RETURNED): every item of the returned operation gets the row. The bank text
    quotes the cheque number, the masked card plus merchant id, or the payment-run end-to-end id
    (or only the beneficiary name), never the invoice. The invoice was usually settled before the
    phase, so it is absent from the opening snapshot; restore its identity at zero balance, only
    for batch rows that a return line of the phase actually points to.
    """
    try:
        floor = (date.fromisoformat(first_date).replace(day=1) - timedelta(days=183)).strftime('%Y%m')
    except ValueError:
        return 0
    card_keys = set()
    returns_by_amount = defaultdict(list)
    for txn in txns:
        raw = ' '.join(str(txn.get(f) or '') for f in FIELDS)
        try:
            amount = Decimal(str(txn.get('amount') or '0'))
        except ArithmeticError:
            continue
        if amount < 0 and CHARGEBACK.search(raw):
            digits = {t for t in re.findall(r'\d{6,}', raw)}
            for last4 in MASKED_CARD.findall(raw):
                for mid in digits:
                    card_keys.add((txn.get('entity', ''), mid, last4))
        if amount > 0 and RETURN_TEXT.search(raw):
            returns_by_amount[amount].append((txn.get('entity', ''), norm(raw)))
    added = 0
    roots = [phase_dir] + [Path(p) for p in getattr(ctx, 'history_dirs', ())]
    for folder in ('cheque_deposits', 'card_settlements', 'payment_runs'):
        seen = set()
        for root in roots:
            for path in sorted((Path(root) / folder).glob('*.csv')):
                if path.name.startswith('._') or path.name in seen:
                    continue
                month = re.search(r'_(\d{6})(?:_\d+)?\.csv$', path.name)
                if not month or month.group(1) < floor:
                    continue
                seen.add(path.name)
                entity = path.name.split('_')[0]
                for row in read_csv(path):
                    token = ''
                    if folder == 'cheque_deposits':
                        if entity in refs.get(norm(row.get('cheque_number', '')), ()):
                            token = norm(row.get('stub_reference', ''))
                    elif folder == 'card_settlements':
                        card = re.sub(r'\D', '', row.get('card', ''))[-4:]
                        if (entity, norm(row.get('merchant_id', '')), card) in card_keys:
                            token = norm(row.get('order_number', ''))
                    elif row.get('run_type') == 'transfers':
                        quoted = entity in refs.get(norm(row.get('end_to_end_id', '')), ())
                        if not quoted:
                            try:
                                cash = Decimal(row.get('transfer_amount') or '0')
                            except ArithmeticError:
                                cash = None
                            name = norm(row.get('beneficiary', ''))
                            quoted = len(name) >= 6 and any(
                                e == entity and name in text for e, text in returns_by_amount.get(cash, ()))
                        if quoted:
                            token = norm(row.get('document_number', ''))
                    if len(token) >= 3 and any(c.isdigit() for c in token) and entity not in refs[token]:
                        refs[token].add(entity)
                        added += 1
    return added


def transfer_reference(txn):
    """Rebuild a per-transfer end-to-end ID cut by the bank statement.

    Norma 43 lines carry the run ID truncated after the dash in ref1/ref2
    ('PR010024091060-0') and the transfer sequence at the end of the text
    ('PAGO TRANSFERENCIAS | 4799-56-1122872-73 051775 000001'). Return the
    normalized 'PR0100240910600001', or '' unless both parts agree.
    """
    run = None
    for key in ('ref2', 'ref1'):
        run = re.fullmatch(r'(PR\d{12})-(\d{0,3})', str(txn.get(key) or '').strip())
        if run:
            break
    if not run:
        return ''
    sequence = re.search(r'\b(\d{6})\s*$', str(txn.get('text') or ''))
    if not sequence or not sequence[1][-4:].startswith(run[2]):
        return ''
    return norm(run[1] + '-' + sequence[1][-4:])


def _repeated_transfer_references(ctx, phase_dir, first_date, txns, refs):
    """Documents of an earlier supplier transfer that a phase debit pays again.

    POLICIES 3 (DUPLICATE_PAYMENT): paying the same transfer twice is a duplicate of
    the documents it settled. The debit quotes the run ID cut after the dash plus the
    transfer sequence (rebuilt by transfer_reference). When the original transfer was
    made before the phase, its documents are closed and absent from the opening
    snapshot; restore their identities at zero balance. Numbers that are open in the
    snapshot are left alone.
    """
    wanted = {}
    for txn in txns:
        try:
            amount = Decimal(str(txn.get('amount') or '0'))
        except ArithmeticError:
            continue
        if amount < 0:
            rebuilt = transfer_reference(txn)
            if rebuilt:
                wanted[(rebuilt, txn.get('account_id'))] = txn
    if not wanted:
        return 0
    try:
        floor = (date.fromisoformat(first_date).replace(day=1) - timedelta(days=92)).strftime('%Y%m')
    except ValueError:
        return 0
    added = 0
    roots = [phase_dir] + [Path(p) for p in getattr(ctx, 'history_dirs', ())]
    seen = set()
    for root in roots:
        for path in sorted((Path(root) / 'payment_runs').glob('*.csv')):
            if path.name.startswith('._') or path.name in seen:
                continue
            month = re.search(r'_(\d{6})(?:_\d+)?\.csv$', path.name)
            if not month or month.group(1) < floor:
                continue
            seen.add(path.name)
            entity = path.name.split('_')[0]
            for row in read_csv(path):
                if row.get('run_type') not in ('transfers', 'confirming'):
                    continue
                if (norm(row.get('end_to_end_id', '')), row.get('debtor_account_id')) not in wanted:
                    continue
                token = norm(row.get('document_number', ''))
                if token and not ctx.by_number.get(token) and entity not in refs[token]:
                    refs[token].add(entity)
                    added += 1
    return added
