"""Receipts paid by a confirming bank on behalf of the customer (POLICIES §5 third-party payers).

Spanish "ABONO CONFIRMING <bank> | EFC" credits carry the ordering customer only as a
bank-truncated name in ref2 ("ORD. HOTELES NAV").  The confirming bank pays one invoice of
that customer in full.  The truncated name is too short for ordinary alias scoring, so it
is resolved here by prefix against the customer master of the bank entity.  Last resort
only: runs after every other handler abstained.
"""
from decimal import Decimal
import re

from .context import norm

_CONFIRMING = re.compile(r'ABONO CONFIRMING')
_ORDERER = re.compile(r'\bORD\.\s*([^|]+)')


def _orderer_prefix(txn):
    for field in ('ref2', 'text', 'remittance_info'):
        match = _ORDERER.search(str(txn.get(field) or '').upper())
        if match:
            return norm(match.group(1))
    return ''


def _customers_by_prefix(ctx, entity, prefix):
    found = []
    for pid, party in (getattr(ctx, 'customers', {}) or {}).items():
        if party.get('entity') != entity:
            continue
        names = [norm(party.get('name')), norm(party.get('trade_name'))]
        if any(n and n.startswith(prefix) for n in names):
            found.append(pid)
    return found


def reconcile_confirming(txn, ctx):
    amount = Decimal(str(txn.get('amount') or 0))
    if amount <= 0 or not _CONFIRMING.search(str(txn.get('text') or '').upper()):
        return None
    prefix = _orderer_prefix(txn)
    if len(prefix) < 8:
        return None
    entity, day = txn.get('entity'), txn['booking_date']
    candidates = []
    for pid in _customers_by_prefix(ctx, entity, prefix):
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if (item.get('side') == 'AR' and item.get('kind') == 'invoice' and item.get('entity') == entity
                    and item.get('issue_date', '') <= day and item.get('currency', txn.get('currency')) == txn.get('currency')
                    and abs(item['open_amount'] - amount) <= Decimal('0.005')):
                candidates.append(item)
    if len({item['party_id'] for item in candidates}) != 1:
        return None
    # POLICIES 5.6: several open invoices of the same amount and no usable reference -> oldest first.
    item = min(candidates, key=lambda i: (i.get('due_date') or '', i.get('issue_date') or '', i['item_id']))
    return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': item['item_id'],
             'amount': format(item['open_amount'].quantize(Decimal('0.01')), '.2f'),
             'category': '', 'exception': ''}]
