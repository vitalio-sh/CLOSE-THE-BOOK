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
    from .matching import (_index, _party_entity_ok, _SERVICE_RE, _service_month_word, _invoice_service_month,
                           party_candidates, text_of, words)
    amount = Decimal(str(txn.get('amount') or 0))
    if amount <= 0:
        return None
    raw = text_of(txn)
    # Every month mention with an explicit year ("SERVICE SEPTEMBER 2024"); the bank text
    # often repeats the remittance cut before the year ("SERVICE SEPTEMBER |").
    dated = set()
    for match in _SERVICE_RE.finditer(words(raw)):
        month = _service_month_word(match[1])
        if month and match[2]:
            year = int(match[2]) if len(match[2]) == 4 else 2000 + int(match[2])
            dated.add(f'{year:04d}-{month:02d}')
    if len(dated) != 1:
        return None
    wanted = dated.pop()
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
    # Every customer whose master name is exactly the stated payer name (more homonyms than
    # party resolution keeps).
    names = getattr(ctx, '_exact_customer_names', None)
    if names is None:
        names = {}
        for pid, party in (getattr(ctx, 'customers', {}) or {}).items():
            names.setdefault(norm(party.get('name')), set()).add(pid)
        ctx._exact_customer_names = names
    explicit = norm(txn.get('counterparty_name'))
    if len(explicit) >= 6:
        pids |= {pid for pid in names.get(explicit, ()) if _party_entity_ok(parties.get(pid, {}), owner)}
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


_AGENCY_FILE = re.compile(r'\bEXPTE\s+\d+\s+(.+?)\s+(\d{6,7})\b')


def reconcile_agency(txn, ctx):
    """Collection-agency receipts "EXPTE <file> <customer name cut> <invoice-number suffix>".

    The agency pays on behalf of the customer (POLICIES §5 third-party payers).  The suffix
    is the last digits of one open invoice of that customer; the name cut must start that
    customer's master name.  The receipt settles that invoice when it pays exactly its open
    amount, or the customer's whole open account when it pays exactly that total.
    Last resort only; any other cash difference abstains.
    """
    amount = Decimal(str(txn.get('amount') or 0))
    if amount <= 0:
        return None
    match = None
    for field in ('remittance_info', 'text', 'ref2'):
        match = _AGENCY_FILE.search(str(txn.get(field) or '').upper())
        if match:
            break
    if not match:
        return None
    prefix, suffix = norm(match.group(1)), match.group(2)
    if len(prefix) < 8:
        return None
    entity, day = txn.get('entity'), txn['booking_date']
    customers = getattr(ctx, 'customers', {}) or {}
    index = getattr(ctx, '_ar_number_tails', None)
    if index is None:
        index = {}
        for iid, item in ctx.items.items():
            if item.get('side') == 'AR' and item.get('kind') == 'invoice':
                index.setdefault(str(item.get('document_number') or '')[-6:], []).append(iid)
        ctx._ar_number_tails = index
    anchors = [ctx.items[iid] for iid in index.get(suffix[-6:], ())
               if str(ctx.items[iid].get('document_number') or '').endswith(suffix)]
    anchors = [item for item in anchors
               if item['open_amount'] > 0 and item.get('issue_date', '') <= day and item.get('entity') == entity
               and (norm(customers.get(item.get('party_id'), {}).get('name')).startswith(prefix)
                    or norm(customers.get(item.get('party_id'), {}).get('trade_name')).startswith(prefix))]
    if len(anchors) != 1:
        return None
    anchor = anchors[0]
    if anchor.get('currency', txn.get('currency')) != txn.get('currency'):
        return None
    account = [ctx.items[iid] for iid in ctx.by_party.get(anchor['party_id'], ())
               if ctx.items[iid].get('side') == 'AR' and ctx.items[iid].get('kind') == 'invoice'
               and ctx.items[iid].get('entity') == entity and ctx.items[iid]['open_amount'] > 0
               and ctx.items[iid].get('issue_date', '') <= day]
    if abs(anchor['open_amount'] - amount) <= Decimal('0.005'):
        chosen = [anchor]
    elif abs(sum((i['open_amount'] for i in account), Decimal(0)) - amount) <= Decimal('0.005'):
        chosen = account
    else:
        explained = _explained_difference(anchor, amount, txn, ctx)
        if not explained:
            return None
        exc, applied = explained
        return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': anchor['item_id'],
                 'amount': format(Decimal(str(applied)).quantize(Decimal('0.01')), '.2f'),
                 'category': '', 'exception': exc}]
    return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': item['item_id'],
             'amount': format(item['open_amount'].quantize(Decimal('0.01')), '.2f'),
             'category': '', 'exception': ''} for item in sorted(chosen, key=lambda i: i['item_id'])]


_EXPLAINED = {'OVERPAYMENT', 'SHORT_DISCOUNT', 'SHORT_WITHHOLDING', 'SHORT_BANK_CHARGES', 'SHORT_DISPUTE'}


def _explained_difference(item, amount, txn, ctx):
    """POLICIES §3 exception of the anchor invoice when the agency pays it with a documented
    difference (early-payment discount, 1 %/10 %/round overpayment, withholding, bank charges,
    stated dispute).  An unexplained difference (PARTIAL or an unknown excess) returns None."""
    from .matching import _exception
    exc, applied = _exception(item, amount, txn, ctx)
    return (exc, applied) if exc in _EXPLAINED else None
