"""Reverse one uniquely evidenced prior multi-item allocation group."""
from collections import defaultdict
import re
from .matching import party_candidates, text_of, words, norm, _eligible, dec, CENT, row


def reconcile_return_group(txn, ctx):
    if not re.search(r'DEVOLUCION (?:RECIBO|TRANSFERENCIA)|DEVOL RECIB|RUECKLASTSCHRIFT|RETURNED ITEM|CHEQUE IMPAYE|CHARGEBACK|CONTRACARGO|DIRECT DEBIT RETURN', words(text_of(txn))):
        return None
    parties = party_candidates(txn, ctx)
    if len(parties) != 1:
        return None
    tokens = {norm(t) for t in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', text_of(txn))}
    refs = {i for t in tokens for i in ctx.by_number.get(t, ())}
    groups = defaultdict(list)
    invalid = set()
    for iid in ctx.by_party.get(parties[0], ()):
        item = ctx.items[iid]
        if not _eligible(item, txn, True) or item.get('kind') != 'invoice':
            continue
        history = getattr(ctx, 'history', {}).get(iid, ())
        for h in history:
            if (not h.get('bank_txn_id') or h.get('exception') in ('RETURNED', 'DUPLICATE_PAYMENT')
                    or h.get('account_id') != txn.get('account_id') or h.get('booking_date', '') > txn['booking_date']):
                continue
            original = h['bank_txn_id']
            # Without return-to-original linkage, any subsequent return is ambiguous.
            if any(x.get('exception') == 'RETURNED' and x.get('booking_date', '') >= h['booking_date'] for x in history):
                invalid.add(original)
            groups[original].append((item,h))
    complete = defaultdict(set)
    for iid, history in getattr(ctx, 'history', {}).items():
        for h in history:
            if h.get('bank_txn_id') in groups and h.get('exception') not in ('RETURNED','DUPLICATE_PAYMENT'):
                complete[h['bank_txn_id']].add(iid)
    supported = []
    for original, members in groups.items():
        if original in invalid or len({i['item_id'] for i,h in members}) != len(members):
            continue
        if complete[original] != {i['item_id'] for i,h in members}:
            continue
        if refs and not refs.issubset({i['item_id'] for i,h in members}):
            continue
        currencies = {i['currency'] for i,h in members}
        if len(currencies) != 1:
            continue
        currency = next(iter(currencies))
        if currency == txn['currency']:
            cash = abs(dec(txn['amount']))
        elif txn.get('orig_currency') == currency and txn.get('orig_amount'):
            cash = abs(dec(txn['orig_amount']))
        else:
            continue
        if abs(sum((dec(h['amount']) for i,h in members),dec(0)) - cash) <= CENT:
            supported.append([row(txn,i['item_id'],h['amount'],'RETURNED') for i,h in members])
    return supported[0] if len(supported) == 1 and len(supported[0]) >= 2 else None
