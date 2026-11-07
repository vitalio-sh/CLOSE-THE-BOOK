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


def reconcile_homonym_month(txn, ctx):
    """Same-named payers resolved by the stated service month (POLICIES 5.6).

    Several customers of the entity share a short person name, so party resolution abstains.
    When the text names a service month ("SERVICE AUGUST 2024", invoiced at the start of the
    following month) and exactly one open invoice of those same-named customers has exactly
    the amount for that month, that invoice is paid.  Last resort only.
    """
    from .matching import (_index, _party_entity_ok, _stated_service_month, _invoice_service_month,
                           party_candidates, text_of, words)
    amount = Decimal(str(txn.get('amount') or 0))
    if amount <= 0:
        return None
    raw = text_of(txn)
    stated = _stated_service_month(raw, txn.get('booking_date'))
    if not stated or not stated[1]:
        return None
    wanted = stated[0]
    _, _, _, _, parties = _index(ctx)
    shorts = getattr(ctx, '_matching_shorts', None) or {}
    owner, day = txn.get('entity'), txn['booking_date']
    pids = set(party_candidates(txn, ctx))
    tokens = words(raw).split()
    for size in (1, 2, 3, 4):
        for i in range(len(tokens) - size + 1):
            key = ''.join(tokens[i:i + size])
            if 6 <= len(key) <= 9 and key in shorts:
                pids |= {pid for pid in shorts[key] if _party_entity_ok(parties.get(pid, {}), owner)}
    candidates = []
    for pid in pids:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if (item.get('side') == 'AR' and item.get('kind') == 'invoice' and item.get('entity') == owner
                    and item.get('issue_date', '') <= day and item.get('currency', txn.get('currency')) == txn.get('currency')
                    and abs(item['open_amount'] - amount) <= Decimal('0.005')):
                facts = getattr(ctx, 'pdf_facts', None)
                fact = facts.get(iid) if facts is not None else {}
                service = (fact or {}).get('service_month') or _invoice_service_month(item)
                if service == wanted:
                    candidates.append(item)
    if len(candidates) != 1:
        return None
    item = candidates[0]
    return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': item['item_id'],
             'amount': format(item['open_amount'].quantize(Decimal('0.01')), '.2f'),
             'category': '', 'exception': ''}]
