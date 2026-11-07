"""Documented cash differences on a payer's invoices (POLICIES 2 and 3).

A receipt can be short of the open amounts by an early-payment discount (taken in
terms: SHORT_DISCOUNT; taken late: PARTIAL for the cash), by tax withheld at source
(MX/BR/MA customers: SHORT_WITHHOLDING) and can arrive in another currency with the
instructed amount in the invoice currency (FX_DIFFERENCE replaces the full-clearing
codes). Only exact cent signatures are accepted, and only when they are unique.
"""
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_DOWN, ROUND_HALF_UP
from itertools import product
import re

from .matching import CENT, dec, norm, row, text_of, _eligible, _exception, party_candidates

WITHHOLDING_COUNTRIES = ('MX', 'BR', 'MA')
WITHHOLDING_RATES = (D('0.0125'), D('0.015'), D('0.10'))
MAX_DISCOUNT_ITEMS = 10


def half_up(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def target_for(txn, currency):
    """Cash expressed in the item currency: the bank amount or the instructed original amount."""
    if currency == txn.get('currency'):
        return abs(dec(txn['amount'])), False
    if txn.get('orig_amount') and txn.get('orig_currency') == currency:
        return abs(dec(txn['orig_amount'])), True
    return None, False


def discount_terms(item, ctx):
    """(cash after discount, last accepted booking date) for an untouched invoice with discount terms.

    POLICIES 3 SHORT_DISCOUNT: cash = total - discount % x total, the discount rounded to the
    cent (HALF_UP) before it is subtracted; booking date <= issue date + discount days + 3.
    """
    terms = getattr(ctx, 'payment_terms', {}) or {}
    term = terms.get(item.get('payment_terms'), {}) if isinstance(terms, dict) else {}
    pct = dec(term.get('early_payment_discount_pct'))
    total = dec(item.get('total_amount'))
    if not 0 < pct < 100 or total != dec(item.get('open_amount')) or total <= 0:
        return None
    try:
        deadline = date.fromisoformat(item['issue_date']) + timedelta(days=int(term['discount_days']) + 3)
    except (KeyError, TypeError, ValueError):
        return None
    return total - half_up(total * pct / 100), deadline


def withholding_value(item, ctx, rate):
    """Cash after tax withheld at source on an untouched invoice of an MX/BR/MA customer."""
    customer = getattr(ctx, 'customers', {}).get(item.get('party_id'), {})
    total = dec(item.get('total_amount'))
    if customer.get('country') not in WITHHOLDING_COUNTRIES or total != dec(item.get('open_amount')):
        return None
    return total - half_up(total * rate)


def _late(txn, deadline):
    try:
        return date.fromisoformat(txn['booking_date']) > deadline
    except (KeyError, TypeError, ValueError):
        return True


def item_options(item, txn, ctx, fx, rate=None):
    """Ways an invoice can be settled by part of this cash: (cash value, exception, row amount)."""
    opened = dec(item.get('open_amount'))
    full = 'FX_DIFFERENCE' if fx else ''
    if rate is not None:
        value = withholding_value(item, ctx, rate)
        return [] if value is None else [(value, 'FX_DIFFERENCE' if fx else 'SHORT_WITHHOLDING', opened)]
    options = [(opened, full, opened)]
    terms = discount_terms(item, ctx)
    if terms is not None:
        value, deadline = terms
        if _late(txn, deadline):
            options.append((value, 'PARTIAL', value))
        else:
            options.append((value, 'FX_DIFFERENCE' if fx else 'SHORT_DISCOUNT', opened))
    return options


def explain_set(txn, ctx, invoices, credits=()):
    """Rows for exactly these invoices (and netted credits) when one adjustment pattern fits the cash.

    Every invoice is settled; each may be paid in full or net of its documented discount, or all
    of them net of one withholding rate. Ambiguous patterns abstain.
    """
    if not invoices or len({i.get('currency') for i in list(invoices) + list(credits)}) != 1:
        return None
    if len({i.get('party_id') for i in invoices}) != 1:
        return None
    target, fx = target_for(txn, invoices[0].get('currency'))
    if target is None:
        return None
    netted = sum((dec(c.get('open_amount')) for c in credits), D(0))
    solutions = []
    patterns = [None] + list(WITHHOLDING_RATES)
    for rate in patterns:
        choices = [item_options(i, txn, ctx, fx, rate) for i in invoices]
        if any(not c for c in choices):
            continue
        if sum(len(c) > 1 for c in choices) > MAX_DISCOUNT_ITEMS:
            return None
        for combo in product(*choices):
            if abs(sum((c[0] for c in combo), D(0)) - netted - target) <= CENT:
                solutions.append(combo)
                if len(solutions) > 1:
                    return None
    if len(solutions) != 1:
        return None
    rows = [row(txn, i['item_id'], amount, exception) for i, (_, exception, amount) in zip(invoices, solutions[0])]
    rows += [row(txn, c['item_id'], c['open_amount']) for c in credits]
    return sorted(rows, key=lambda r: r['item_id'])


def single_adjusted(txn, ctx):
    """One invoice of an identified payer explains the receipt through a documented difference.

    Exact full amounts are the ordinary matcher's job; here only discount (in terms or late),
    withholding and FX-plus-deduction signatures count, and the explanation must be unique among
    all identified payers' invoices.
    """
    if dec(txn['amount']) <= 0:
        return None
    parties = party_candidates(txn, ctx)
    if not parties:
        return None
    found = []
    seen = set()
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            if iid in seen:
                continue
            seen.add(iid)
            item = ctx.items[iid]
            if item.get('side') != 'AR' or item.get('kind') != 'invoice' or not _eligible(item, txn):
                continue
            if item.get('entity') != txn.get('entity') or dec(item.get('open_amount')) <= 0:
                continue
            target, fx = target_for(txn, item.get('currency'))
            if target is None:
                continue
            options = item_options(item, txn, ctx, fx)[1:]
            for rate in WITHHOLDING_RATES:
                options += item_options(item, txn, ctx, fx, rate)
            for value, exception, amount in options:
                if abs(value - target) <= CENT:
                    found.append((item, exception, amount))
            if len(found) > 1:
                return None
    if len(found) != 1:
        return None
    item, exception, amount = found[0]
    return [row(txn, item['item_id'], amount, exception)]


def truncated_reference(txn, ctx):
    """A receipt quoting one invoice number cut by the bank (POLICIES 5.5: truncation at 16/35/38).

    The cut number must be the prefix of exactly one open document of the identified payers,
    the text must quote no complete number, and the cash must be below the open amount. A credit
    note of the payer that exactly bridges the difference is netted; otherwise the ordinary
    difference rules apply (PARTIAL for an unexplained shortfall, POLICIES 3).
    """
    if dec(txn['amount']) <= 0:
        return None
    parties = party_candidates(txn, ctx)
    if not parties:
        return None
    raw = text_of(txn)
    compact = norm(raw)
    tokens = {t for t in (norm(x) for x in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw))
              if len(t) >= 7 and any(c.isdigit() for c in t)}
    numeric = set(re.findall(r'\b\d{5,}\b', raw))
    if not tokens:
        return None
    items, seen = [], set()
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if iid in seen or item.get('entity') != txn.get('entity'):
                continue
            # Credit notes are netted documents, not receipts of their own sign.
            if not (_eligible(item, txn) or (item.get('kind') == 'credit' and _eligible(item, txn, True))):
                continue
            seen.add(iid)
            if dec(item.get('open_amount')) > 0:
                items.append(item)
    hits = []
    for item in items:
        number = norm(item.get('document_number'))
        if len(number) >= 5 and (number in compact or any(len(v) >= 5 and number.endswith(v) for v in numeric)):
            return None
        if any(len(number) > len(t) >= max(7, len(number) * 0.6) and number.startswith(t) for t in tokens):
            hits.append(item)
    if len(hits) != 1 or hits[0].get('kind') != 'invoice' or hits[0].get('side') != 'AR':
        return None
    item = hits[0]
    cash, fx = target_for(txn, item.get('currency'))
    opened = dec(item.get('open_amount'))
    if cash is None or cash >= opened - CENT:
        return None
    bridges = [c for c in items if c.get('kind') == 'credit' and c.get('party_id') == item.get('party_id')
               and c.get('currency') == item.get('currency') and abs(opened - dec(c['open_amount']) - cash) <= CENT]
    if len(bridges) > 1:
        return None
    if bridges:
        return sorted([row(txn, item['item_id'], opened, 'FX_DIFFERENCE' if fx else ''),
                       row(txn, bridges[0]['item_id'], bridges[0]['open_amount'])], key=lambda r: r['item_id'])
    exception, applied = _exception(item, cash, txn, ctx, fx)
    return [row(txn, item['item_id'], applied, exception)]


MAX_SET_ITEMS = 24


def _subsets_for_targets(values, targets, limit=2):
    """Meet-in-the-middle exact subsets (integer cents) for several targets at once."""
    middle = len(values) // 2

    def sums(start, stop):
        result = [(0, ())]
        for index in range(start, stop):
            result += [(total + values[index], ids + (index,)) for total, ids in result]
        return result
    right = {}
    for total, ids in sums(middle, len(values)):
        right.setdefault(total, []).append(ids)
    found = {target: [] for target in targets}
    for total, left in sums(0, middle):
        for target in targets:
            for tail in right.get(target - total, ()):
                if len(found[target]) <= limit:
                    found[target].append(left + tail)
    return found


def adjusted_sets(txn, ctx):
    """Several invoices of one identified payer, no usable reference, one documented difference.

    Either exactly one invoice is paid net of its early-payment discount (in terms or late) and
    the others in full, or every invoice is paid net of one withholding rate. Groups are capped
    at 24 open invoices, and a plain exact subset or a second pattern makes the line ambiguous.
    """
    if dec(txn['amount']) <= 0:
        return None
    parties = party_candidates(txn, ctx)
    for pid in parties:
        invoices = [ctx.items[i] for i in ctx.by_party.get(pid, ())]
        invoices = [i for i in invoices if i.get('side') == 'AR' and i.get('kind') == 'invoice'
                    and i.get('entity') == txn.get('entity') and _eligible(i, txn) and dec(i.get('open_amount')) > 0]
        solutions = []
        currencies = sorted({i.get('currency') for i in invoices})
        for currency in currencies:
            target, fx = target_for(txn, currency)
            group = sorted((i for i in invoices if i.get('currency') == currency), key=lambda i: i['item_id'])
            if target is None or len(group) < 2:
                continue
            if len(group) > MAX_SET_ITEMS:
                return None
            cents = int(target * 100)
            opens = [int(dec(i['open_amount']) * 100) for i in group]
            deltas = {}
            for j, item in enumerate(group):
                terms = discount_terms(item, ctx)
                if terms is not None:
                    deltas[j] = (opens[j] - int(terms[0] * 100), terms)
            targets = {cents} | {cents + d for d, _ in deltas.values()}
            found = _subsets_for_targets(opens, sorted(targets))
            solutions += [('plain', ids, None, fx, group) for ids in found[cents] if len(ids) >= 2]
            for j, (delta, terms) in deltas.items():
                solutions += [('discount', ids, j, fx, group) for ids in found[cents + delta] if len(ids) >= 2 and j in ids]
            customer = getattr(ctx, 'customers', {}).get(pid, {})
            if customer.get('country') in WITHHOLDING_COUNTRIES:
                for rate in WITHHOLDING_RATES:
                    values = [int(v * 100) if (v := withholding_value(i, ctx, rate)) is not None else -10 ** 12 for i in group]
                    solutions += [('withholding', ids, rate, fx, group) for ids in _subsets_for_targets(values, [cents])[cents]
                                  if len(ids) >= 2]
        if not solutions:
            continue
        if len(solutions) != 1 or solutions[0][0] == 'plain':
            return None
        kind, ids, detail, fx, group = solutions[0]
        rows = []
        for index in ids:
            item = group[index]
            opened = dec(item['open_amount'])
            if kind == 'withholding':
                rows.append(row(txn, item['item_id'], opened, 'FX_DIFFERENCE' if fx else 'SHORT_WITHHOLDING'))
            elif index == detail:
                value, deadline = discount_terms(item, ctx)
                if _late(txn, deadline):
                    rows.append(row(txn, item['item_id'], value, 'PARTIAL'))
                else:
                    rows.append(row(txn, item['item_id'], opened, 'FX_DIFFERENCE' if fx else 'SHORT_DISCOUNT'))
            else:
                rows.append(row(txn, item['item_id'], opened, 'FX_DIFFERENCE' if fx else ''))
        return sorted(rows, key=lambda r: r['item_id'])
    return None


# TRAIN: unplanned partial payments ('A CUENTA') are 25/30/40/50/60/70 % of the invoice total
# and first payments under a guarantee retention are 90/95 %, truncated to the cent.
PARTIAL_FRACTIONS = tuple(D(x) for x in ('0.25', '0.30', '0.40', '0.50', '0.60', '0.70', '0.90', '0.95'))


def fraction_partial(txn, ctx):
    """An unreferenced receipt that is a round fraction of exactly one untouched invoice of the payer.

    POLICIES 3 PARTIAL (unplanned partial payment, first payment under a retention): the item
    keeps an open balance and the row amount is the cash. A credit note of the payer that exactly
    bridges the invoice and the cash is netted instead (POLICIES 2).
    """
    if dec(txn['amount']) <= 0:
        return None
    parties = party_candidates(txn, ctx)
    hits, credits, seen = [], [], set()
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if iid in seen or item.get('side') != 'AR' or item.get('entity') != txn.get('entity'):
                continue
            seen.add(iid)
            if dec(item.get('open_amount')) <= 0:
                continue
            if item.get('kind') == 'credit':
                if _eligible(item, txn, True):
                    credits.append(item)
                continue
            total = dec(item.get('total_amount'))
            if not _eligible(item, txn) or total != dec(item.get('open_amount')):
                continue
            cash, fx = target_for(txn, item.get('currency'))
            if cash is None:
                continue
            if any((total * f).quantize(CENT, rounding=ROUND_DOWN) == cash for f in PARTIAL_FRACTIONS):
                hits.append((item, cash, fx))
    if len(hits) != 1:
        return None
    item, cash, fx = hits[0]
    opened = dec(item['open_amount'])
    bridges = [c for c in credits if c.get('party_id') == item.get('party_id') and c.get('currency') == item.get('currency')
               and abs(opened - dec(c['open_amount']) - cash) <= CENT]
    if len(bridges) == 1:
        return sorted([row(txn, item['item_id'], opened, 'FX_DIFFERENCE' if fx else ''),
                       row(txn, bridges[0]['item_id'], bridges[0]['open_amount'])], key=lambda r: r['item_id'])
    if bridges:
        return None
    return [row(txn, item['item_id'], cash, 'PARTIAL')]


def reconcile_adjusted(txn, ctx):
    return (single_adjusted(txn, ctx) or truncated_reference(txn, ctx) or fraction_partial(txn, ctx)
            or adjusted_sets(txn, ctx))
