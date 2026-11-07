"""Same-named payers resolved by the one exact open amount (POLICIES 5.5 + 5.6).

party_candidates deliberately proposes no payer when several parties of the paying entity share
the printed name ('Noah Evans', 'Coral Bay North Pools Pty Ltd'), or cuts a long namesake list at
12. The name still narrows the payer to those namesakes. When exactly one of them holds open
invoices whose open amount equals the cash, that namesake paid, and POLICIES 5.6 picks the oldest
open one by invoice date. Two or more namesakes with an exact item, or none, abstain.

Runs as a last resort after every other matcher, receipts only, full open amount only.
"""
from collections import defaultdict

from . import matching as m


def _namesakes(txn, ctx):
    prefixes, _ibans, aliases, _children, parties = m._index(ctx)
    raw = m.text_of(txn)
    compact = m.norm(raw)
    owner = txn.get('entity')
    pool = set()
    for i in range(max(0, len(compact) - 9)):
        pool.update(prefixes.get(compact[i:i + 10], ()))
    tokens = m.words(raw).split()
    shorts = getattr(ctx, '_matching_shorts', None) or {}
    for size in (1, 2, 3, 4):
        for i in range(len(tokens) - size + 1):
            key = ''.join(tokens[i:i + size])
            if 6 <= len(key) <= 9:
                pool.update(shorts.get(key, ()))
    names = defaultdict(set)
    for pid in pool:
        if not m._party_entity_ok(parties.get(pid, {}), owner):
            continue
        # Full printed name evidence: a whole alias occurs in the bank text.
        for alias in aliases[pid]:
            if len(alias) >= 6 and alias in compact:
                names[alias].add(pid)
    # Only genuine namesake groups (two or more parties printed under one name).
    return set().union(*[pids for pids in names.values() if len(pids) >= 2]) if names else set()


def reconcile(txn, ctx):
    if m.dec(txn['amount']) <= 0:
        return None
    if m.classify(txn, ctx):
        return None
    namesakes = _namesakes(txn, ctx)
    if len(namesakes) < 2:
        return None
    exact = defaultdict(list)
    for pid in namesakes:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if item.get('side', 'AR') != 'AR' or item.get('kind') != 'invoice' or not m._eligible(item, txn):
                continue
            opened = m.dec(item.get('open_amount'))
            cash, fx = m._cash(item, txn)
            if cash is not None and opened > 0 and abs(cash - opened) <= m.CENT:
                exact[pid].append((item, fx))
    if len(exact) != 1:
        return None
    (options,) = exact.values()
    options.sort(key=lambda x: (x[0].get('issue_date', ''), x[0]['item_id']))
    item, fx = options[0]
    return [m.row(txn, item['item_id'], item['open_amount'], 'FX_DIFFERENCE' if fx else '')]
