"""Last-resort receipts with a noisy or missing reference (POLICIES 5.5).

Only runs after every other module abstained. A proposal always clears full open
amounts of one payer's open invoices whose total equals the cash exactly, and the
set must be the only one consistent with the evidence. No amount-only global lookup:
the candidate pool is the open book of payers named in the bank text, and an exact
sum is accepted only when the open book makes a coincidental hit unlikely.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from decimal import Decimal
import re

from .matching import norm, row, dec, text_of, words, _eligible, _index, classify

D = Decimal
WINDOW = 2000            # cents; neighbourhood used to estimate coincidental exact sums
FULL_POOL = 24           # exhaustive meet-in-the-middle search up to this many candidates
FULL_DENSITY = 0.03      # max expected coincidental hits for an exhaustive search
SMALL_K = 3              # larger pools: only sets of at most this many unreferenced items
SMALL_DENSITY = 0.02
MAX_SECOND_TIER = 3
PREFER_QUOTED = True
CHARGES_GUARD = False
AMOUNT_ONLY_K = 99         # max set size without any reference anchor


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


def payer_tiers(txn, ctx):
    """Customers of the bank line's entity named in the text, best evidence tier first.

    POLICIES 5.5: party names in bank texts are truncated or trade names, and many
    customers share one printed name, so a tier keeps every tied customer (no rank
    cut). Short names (6-9 characters) are matched as whole aliases.
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
    tiers = [sorted(pid for score, pid in ranked if score >= best - 8)]
    # A weaker tier (a longer printed name, e.g. 'S.L.U.' against 'S.L.') is tried only
    # when it is a handful of customers with a 16+ character name prefix in the text.
    second = sorted(pid for score, pid in ranked if 46 <= score < best - 8)
    if len(second) <= MAX_SECOND_TIER:
        tiers.append(second)
    result = []
    for tier in tiers:
        if tier:
            for pid in list(tier):
                tier.extend(sorted(children.get(pid, ())))
            result.append(list(dict.fromkeys(tier)))
    return result


def _osa1(a, b):
    """True when the optimal-string-alignment distance is exactly 1."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


_MONEY = re.compile(r'\d+[.,]\d{2}(?!\d)')
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9/_-]*')


def reference_tokens(txn):
    """Distinct alphanumeric tokens with digits from the free-text fields.

    Money amounts are removed first (they must never clear an invoice). Norma 43
    concept lines are cut at a fixed width and joined with ' | ', which splits a
    reference in two; the joined variant restores it.
    """
    tokens = set()
    for key in ('text', 'ref2', 'remittance_info'):
        value = _MONEY.sub(' ', str(txn.get(key) or ''))
        for variant in {value, value.replace(' | ', '')}:
            for token in _TOKEN.findall(variant):
                n = norm(token)
                if 5 <= len(n) <= 20 and any(c.isdigit() for c in n):
                    tokens.add(n)
    return tokens


def _level(n, t):
    """2 = token is the number or a substring of it, 1 = one edit from its aligned tail, 0 = none."""
    if t in n:
        return 2
    if len(t) >= 6:
        for length in (len(t) - 1, len(t), len(t) + 1):
            if 5 <= length <= len(n) and _osa1(n[-length:], t):
                return 1
    return 0


def anchors_of(group, tokens):
    """Items singled out by a reference token within one payer's open invoices.

    A token is evidence only when it designates exactly one open invoice: by
    substring first (number without prefix, truncated, cut at 16/35/38), else by
    a single typo or transposition of its aligned tail (POLICIES 5.5).
    """
    numbers = [(i, norm(i.get('document_number'))) for i in group]
    exact, fuzzy, mentioned = {}, {}, set()
    for t in tokens:
        hits = [(i, _level(n, t)) for i, n in numbers if len(n) >= 5]
        sub = [i for i, level in hits if level == 2]
        mentioned.update(i['item_id'] for i, level in hits if level)
        if len(sub) == 1:
            exact[sub[0]['item_id']] = sub[0]
        elif not sub:
            one = [i for i, level in hits if level == 1]
            if len(one) == 1:
                fuzzy[one[0]['item_id']] = one[0]
    anchors_of.mentioned = mentioned
    return list(exact.values()), [i for k, i in fuzzy.items() if k not in exact]


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


def exact_and_density(values, target, window=WINDOW):
    """Up to two exact index sets plus the expected number of coincidental hits.

    With N subset sums inside target +- window, an exact hit is evidence only when
    N / (2*window + 1) -- the chance that some set sums to the target by accident --
    is small. Meet in the middle, so only for small candidate lists.
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


def small_sets_and_density(values, target, kmax=SMALL_K, window=WINDOW, stop=None):
    """Exact sets of at most kmax items (up to two) and the same density estimate restricted to them.

    Once the neighbourhood count passes `stop` the density is reported as infinite
    and only the existence of an exact set is still determined.
    """
    n = len(values)
    order = sorted(range(n), key=values.__getitem__)
    sv = [values[i] for i in order]
    found, near = [], 0
    lo, hi = bisect_left(sv, target - window), bisect_right(sv, target + window)
    near += hi - lo
    found += [(order[j],) for j in range(bisect_left(sv, target), bisect_right(sv, target))][:2]
    if kmax >= 2:
        for a in range(n):
            lo = bisect_left(sv, target - window - sv[a], a + 1)
            hi = bisect_right(sv, target + window - sv[a], a + 1)
            near += hi - lo
            if len(found) < 2:
                e = bisect_left(sv, target - sv[a], lo, hi)
                found += [(order[a], order[j]) for j in range(e, bisect_right(sv, target - sv[a], lo, hi))][:2]
    dense = stop is not None and near > stop
    top = sv[-1] if sv else 0
    if kmax >= 3 and not dense:
        for a in range(n):
            # Skip partners too small to reach the window even with the largest third item.
            for b in range(max(a + 1, bisect_left(sv, target - window - sv[a] - top)), n):
                s = sv[a] + sv[b]
                if s > target + window:
                    break
                lo = bisect_left(sv, target - window - s, b + 1)
                hi = bisect_right(sv, target + window - s, b + 1)
                near += hi - lo
                if len(found) < 2 and hi > lo:
                    e = bisect_left(sv, target - s, lo, hi)
                    found += [(order[a], order[b], order[j]) for j in range(e, bisect_right(sv, target - s, lo, hi))][:2]
            if stop is not None and near > stop:
                dense = True
                break
    if dense:
        if not found and kmax >= 3:
            last = {}
            for j, v in enumerate(sv):
                last[v] = j
            for a in range(n):
                for b in range(max(a + 1, bisect_left(sv, target - sv[a] - top)), n):
                    j = last.get(target - sv[a] - sv[b])
                    if j is not None and j > b:
                        found.append((order[a], order[b], order[j]))
                        break
                if found:
                    break
        return found[:2], float('inf')
    return found[:2], near / (2 * window + 1)


def _remainder(rest, gap):
    """('ok', indices) for a unique low-density exact set, 'ambiguous', or None."""
    values = [cents(i['open_amount']) for i in rest]
    if len(rest) <= FULL_POOL:
        found, density = exact_and_density(values, gap)
        limit = FULL_DENSITY
    else:
        found, density = small_sets_and_density(values, gap, stop=SMALL_DENSITY * (2 * WINDOW + 1))
        limit = SMALL_DENSITY
    if not found:
        return None
    if len(found) > 1 or density > limit:
        return 'ambiguous'
    _solve_party.meta = (len(found[0]), len(rest), round(density, 4))
    return found[0]


def _solve_party(group, target, tokens, amount_only=True):
    """(items, rule) for the unique evidence-consistent exact set, 'ambiguous', or None."""
    exact, fuzzy = anchors_of(group, tokens)
    mentioned = anchors_of.mentioned
    attempts = [(exact + fuzzy, 'anchored_fuzzy')] if fuzzy else []
    attempts.append((exact, 'anchored' if exact else 'amount_only'))
    for anchors, rule in attempts:
        gap = target - sum(cents(i['open_amount']) for i in anchors)
        if gap < 0:
            continue
        if gap == 0:
            if anchors:
                return anchors, rule
            continue
        if not anchors and not amount_only:
            continue
        chosen = {i['item_id'] for i in anchors}
        rest = [i for i in group if i['item_id'] not in chosen and 0 < cents(i['open_amount']) <= gap]
        # A reference list that quotes every paid invoice (truncated or garbled) restricts
        # the remainder to the quoted invoices; otherwise the whole open book is searched.
        quoted = [i for i in rest if i['item_id'] in mentioned]
        for pool, suffix in (((quoted, '+quoted'),) if quoted and len(quoted) < len(rest) else ()) + ((rest, ''),):
            found = _remainder(pool, gap)
            if found == 'ambiguous':
                return 'ambiguous'
            if found is not None:
                if not anchors and len(found) > AMOUNT_ONLY_K:
                    return 'ambiguous'
                return anchors + [pool[k] for k in found], rule + suffix
    return None


_CHARGED = re.compile(r'\b(?:SWIFT|TRANSFER EN DIV|TRANSFERENCIA INTERNACIONAL|INTERNATIONAL TRANSFER|WIRE TRANSFER'
                      r'|GASTOS (?:SHA|BEN|OUR)|CHGS)\b')


def reconcile(txn, ctx):
    if dec(txn.get('amount')) <= 0 or classify(txn, ctx):
        return None
    tiers = payer_tiers(txn, ctx)
    if not tiers:
        return None
    target = cents(abs(dec(txn['amount'])))
    tokens = reference_tokens(txn)
    # POLICIES 3 SHORT_BANK_CHARGES: a cross-border transfer can arrive short of the
    # invoices, so an exact amount-only set is not evidence there.
    bank_text = words(' | '.join(str(txn.get(k) or '') for k in ('text', 'ref1', 'ref2', 'remittance_info')))
    charged = CHARGES_GUARD and (bool(_CHARGED.search(bank_text)) or 'XBCT' in str(txn.get('bank_code') or '').upper())
    for parties in tiers:
        pools = _pool(txn, ctx, parties)
        # The payer is the customer whose invoices the text quotes: when one customer has
        # two or more uniquely quoted invoices, customers quoted less cannot be the payer.
        quoted = {pid: len(anchors_of(group, tokens)[0]) for pid, group in pools.items()}
        best = max(quoted.values(), default=0)
        answers = []
        for pid in sorted(pools):
            if PREFER_QUOTED and best >= 2 and quoted[pid] < best:
                continue
            result = _solve_party(pools[pid], target, tokens, amount_only=not charged)
            if result == 'ambiguous':
                return None
            if result:
                answers.append(result)
        if len(answers) > 1:
            return None
        if answers:
            items, rule = answers[0]
            reconcile.last_rule = rule
            return [row(txn, i['item_id'], i['open_amount']) for i in sorted(items, key=lambda i: i['item_id'])]
    return None
