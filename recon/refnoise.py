"""Last-resort receipts with a noisy or missing reference (POLICIES 5.5).

Only runs after every other module abstained. A proposal always clears full open
amounts of one payer's open invoices whose total equals the cash exactly, and the
set must be the only one consistent with the evidence. No amount-only global lookup:
the candidate pool is the open book of payers named in the bank text.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from decimal import Decimal
import re

from .matching import norm, row, dec, text_of, _eligible, _index, classify
from .subsets import subset_solutions

D = Decimal


def _short_alias_index(ctx):
    cached = getattr(ctx, '_refnoise_short', None)
    if cached is not None:
        return cached
    prefixes, ibans, aliases, children, parties = _index(ctx)
    short = defaultdict(set)
    for pid, names in aliases.items():
        for alias in names:
            if 6 <= len(alias) < 10:
                short[alias].add(pid)
    ctx._refnoise_short = short
    return short


def payer_candidates(txn, ctx):
    """Every customer of the bank line's entity whose name evidence ties for best.

    POLICIES 5.5: party names in bank texts are truncated or trade names. Many
    customers share one printed name, so a tie keeps all of them (no rank cut);
    the open-book evidence below must then single out one payer.
    """
    prefixes, ibans, aliases, children, parties = _index(ctx)
    short = _short_alias_index(ctx)
    raw = text_of(txn)
    compact = norm(raw)
    found = set()
    for i in range(max(0, len(compact) - 9)):
        found.update(prefixes.get(compact[i:i + 10], ()))
    for length in range(6, 10):
        for i in range(len(compact) - length + 1):
            found.update(short.get(compact[i:i + length], ()))
    for token in re.findall('[A-Za-z0-9-]{8,}', raw):
        found.update(ibans.get(norm(token), ()))
    found.update(ibans.get(norm(txn.get('counterparty_account')), ()))
    explicit = norm(txn.get('counterparty_name'))
    entity = txn.get('entity')
    ranked = []
    for pid in found:
        p = parties.get(pid, {})
        if p.get('entity') and p.get('entity') != entity:
            continue
        account_hit = any(norm(p.get(k)) and norm(p.get(k)) in compact for k in ('iban', 'account_number', 'sepa_mandate_id'))
        score = 100 if account_hit else 0
        for alias in aliases[pid]:
            if alias == explicit:
                score = max(score, 90)
            elif alias in compact:
                score = max(score, 80 + min(9, len(alias) // 5))
            elif len(alias) >= 12:
                for length in (24, 20, 16, 12):
                    if len(alias) >= length and alias[:length] in compact:
                        score = max(score, 30 + length)
                        break
        if score >= 42:
            ranked.append((score, pid))
    if not ranked:
        return []
    best = max(score for score, _ in ranked)
    selected = sorted(pid for score, pid in ranked if score >= best - 8)
    for pid in list(selected):
        selected.extend(sorted(children.get(pid, ())))
    return list(dict.fromkeys(selected))


def _osa1(a, b):
    """True when optimal-string-alignment distance is exactly 1."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if len(a) > len(b):
        a, b = b, a
    for i in range(len(b)):
        if b[:i] + b[i + 1:] == a:
            return True
    return False


_MONEY = re.compile(r'\d+[.,]\d{2}(?!\d)')


def reference_tokens(txn):
    """Alphanumeric tokens with digits from the free-text fields; money amounts excluded."""
    fields = ' | '.join(str(txn.get(k) or '') for k in ('text', 'ref2', 'remittance_info'))
    fields = _MONEY.sub(' ', fields)
    tokens = set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/_-]*', fields):
        n = norm(token)
        if 4 <= len(n) <= 20 and any(c.isdigit() for c in n):
            tokens.add(n)
    return tokens


def evidence(item, tokens, compact):
    """'strong' (exact, suffix), 'fuzzy' (one edit), 'prefix' (truncated) or ''."""
    n = norm(item.get('document_number'))
    if len(n) < 5:
        return ''
    if n in compact:
        return 'strong'
    for t in tokens:
        if len(t) >= 5 and n.endswith(t):
            return 'strong'
    for t in tokens:
        if len(t) >= 6:
            for length in (len(t) - 1, len(t), len(t) + 1):
                if 5 <= length <= len(n) and _osa1(n[-length:], t):
                    return 'fuzzy'
    for t in tokens:
        if 5 <= len(t) < len(n) and n.startswith(t):
            return 'prefix'
    return ''


def _pool(txn, ctx, parties):
    pools = defaultdict(list)
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if (item.get('side') == 'AR' and item.get('kind') == 'invoice' and _eligible(item, txn)
                    and item.get('currency') == txn.get('currency') and item.get('entity') == txn.get('entity')
                    and dec(item.get('open_amount')) > 0):
                pools[pid].append(item)
    for pid in pools:
        pools[pid].sort(key=lambda i: (i.get('issue_date', ''), i['item_id']))
    return pools


def cents(value):
    return int((dec(value) * 100).to_integral_value())


def _half_sums(values):
    sums = [(0, 0)]
    for k, v in enumerate(values):
        sums += [(t + v, m | (1 << k)) for t, m in sums]
    return sums


def exact_and_density(values, target, window=2000):
    """Up to two exact index sets plus the count of subsets whose sum lies within +-window.

    The neighbourhood count estimates how many sets would hit the target by
    coincidence: with N sums spread over 2*window+1 cents, an exact hit carries
    evidence only when N/(2*window+1) is small.
    """
    mid = len(values) // 2
    left = _half_sums(values[:mid])
    right = sorted(_half_sums(values[mid:]))
    keys = [t for t, _ in right]
    found, near = [], 0
    for total, mask in left:
        lo = bisect_left(keys, target - window - total)
        hi = bisect_right(keys, target + window - total)
        near += hi - lo
        if len(found) < 2:
            a = bisect_left(keys, target - total, lo, hi)
            b = bisect_right(keys, target - total, lo, hi)
            for j in range(a, min(b, a + 2)):
                full = mask | (right[j][1] << mid)
                if full:
                    found.append(tuple(k for k in range(len(values)) if full >> k & 1))
    return found[:2], near / (2 * window + 1)


def _solve_party(group, target, tokens, compact, max_pool=24, max_density=0.03):
    """Return (items, rule) for the unique evidence-consistent exact set, or None/'ambiguous'."""
    ev = {i['item_id']: evidence(i, tokens, compact) for i in group}
    strong = [i for i in group if ev[i['item_id']] == 'strong']
    weak = [i for i in group if ev[i['item_id']] in ('fuzzy', 'prefix')]
    for anchors, rule in ((strong + weak, 'anchored_all'), (strong, 'anchored_strong')):
        if not anchors and rule == 'anchored_strong':
            anchors = []
        gap = target - sum(cents(i['open_amount']) for i in anchors)
        if gap < 0:
            continue
        if gap == 0 and anchors:
            return anchors, rule
        rest = [i for i in group if i not in anchors and 0 < cents(i['open_amount']) <= gap]
        if len(rest) > max_pool:
            continue
        found, lam = exact_and_density([cents(i['open_amount']) for i in rest], gap)
        if len(found) == 1 and lam > max_density:
            return 'ambiguous'
        if len(found) == 1:
            _solve_party.meta = (len(anchors), len(found[0]), len(rest), round(lam, 3))
            return anchors + [rest[k] for k in found[0]], (rule if anchors else 'subset_free')
        if len(found) > 1:
            return 'ambiguous'
    return None


def reconcile(txn, ctx, max_pool=24):
    if dec(txn.get('amount')) <= 0 or classify(txn, ctx):
        return None
    parties = payer_candidates(txn, ctx)
    if not parties:
        return None
    pools = _pool(txn, ctx, parties)
    if not pools:
        return None
    target = cents(abs(dec(txn['amount'])))
    tokens = reference_tokens(txn)
    compact = norm(' | '.join(str(txn.get(k) or '') for k in ('text', 'ref2', 'remittance_info')))
    answers = []
    for pid in sorted(pools):
        result = _solve_party(pools[pid], target, tokens, compact, max_pool)
        if result == 'ambiguous':
            return None
        if result:
            answers.append(result)
    if len(answers) != 1:
        return None
    items, rule = answers[0]
    reconcile.last_rule = rule
    return [row(txn, i['item_id'], i['open_amount']) for i in sorted(items, key=lambda i: i['item_id'])]
