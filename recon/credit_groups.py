"""Receipts that pay invoices net of credit notes (POLICIES 2, 3 and 5.9).

A customer pays one or more invoices minus the credit notes that reference them
(`related_document`). Each credit note is a positive row with no exception.
Structured families only, each with an exact signed-cents match and a unique
solution inside the family; never an unconstrained subset sum.

  quoted_credit   a quoted credit note brings in the invoice it references
  quoted_invoice  quoted invoices plus all their open referencing credit notes
  single_net      one invoice of an identified payer plus all its open credit notes
  run_net         a contiguous issue-date run of one payer's invoices plus all
                  open credit notes referencing them
  unit_subset     quoted invoices plus a unique exact set of further "units" (an
                  invoice with all its open credits) of the same payer

Zero-net veto: a unit whose credits offset its invoice fully never joins a run
or a subset (it would make the group ambiguous); the group is not trimmed.
"""
import re
from datetime import date, timedelta
from collections import defaultdict
from decimal import Decimal

from . import matching as M
from .subsets import subset_solutions

D = Decimal


SUBSET_UNITS = 22   # meet-in-the-middle budget: 2 x 2048 partial sums
FREE_UNITS = 6      # largest unanchored unit set accepted from an exact sum
REMAINDER_UNITS = 3 # unquoted units added to quoted anchors in a large book
HORIZON_DAYS = 365  # subset searches skip units issued more than a year before the receipt


def _cents(value):
    return int((D(str(value)) * 100).to_integral_value())


def _signed(item):
    return -_cents(item['open_amount']) if item.get('kind') == 'credit' else _cents(item['open_amount'])


def _normed(ctx, item, field):
    """Normalized document fields, computed once per item and field."""
    cache = getattr(ctx, '_credit_group_norms', None)
    if cache is None:
        cache = {}
        ctx._credit_group_norms = cache
    key = (item['item_id'], field)
    value = cache.get(key)
    if value is None:
        value = cache[key] = M.norm(item.get(field))
    return value


def _evidence(txn, ctx):
    """Payer candidates and quoted item ids, mirroring matching.reconcile."""
    raw = M.text_of(txn)
    compact = M.norm(raw)
    parties = M.party_candidates(txn, ctx)
    refs = set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw):
        refs.update(ctx.by_number.get(M.norm(token), ()))
    numeric_tokens = [v for v in set(re.findall(r'\b\d{5,}\b', raw))]
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            n = _normed(ctx, item, 'document_number')
            if len(n) >= 5 and n in compact:
                refs.add(iid)
            elif numeric_tokens and any(n.endswith(v) for v in numeric_tokens):
                refs.add(iid)
            elif item.get('customer_po'):
                po = _normed(ctx, item, 'customer_po')
                if len(po) >= 6 and po in compact:
                    refs.add(iid)
    return parties, refs


CANCELLATION = 'ANULACION'


def _cancels(credit):
    return CANCELLATION in M.words(credit.get('reason'))


def _book(ctx, txn, pid):
    """Open same-currency AR items of one payer available at the booking date.

    POLICIES 5.7: an invoice cancelled by a credit note is never settled by bank, nor is
    its cancelling credit note; both leave the book.
    """
    invoices, credits = [], []
    entity, currency, booked = txn.get('entity'), txn.get('currency'), txn.get('booking_date', '9999')
    for iid in ctx.by_party.get(pid, ()):
        item = ctx.items[iid]
        if (item['open_amount'] <= 0 or item.get('side') != 'AR' or item.get('entity') != entity
                or item.get('currency') != currency or item.get('issue_date', '') > booked):
            continue
        (credits if item.get('kind') == 'credit' else invoices).append(item)
    if not credits:
        return invoices, credits, {}
    cancelled = {_normed(ctx, c, 'related_document') for c in credits if _cancels(c)}
    credits = [c for c in credits if not _cancels(c)]
    invoices = [i for i in invoices if _normed(ctx, i, 'document_number') not in cancelled]
    invoices.sort(key=lambda i: (i.get('issue_date', ''), i['item_id']))
    related = defaultdict(list)
    for credit in credits:
        key = _normed(ctx, credit, 'related_document')
        if key:
            related[key].append(credit)
    return invoices, credits, related


def _ids(units):
    ids = []
    for inv, rel, _ in units:
        ids.append(inv['item_id'])
        ids.extend(c['item_id'] for c in rel)
    return tuple(sorted(ids))


def candidates(txn, ctx, horizon_days=None, quoted=None, runs=True, zeros=True):
    """Every family solution as (family, party rank, items). Diagnostics use all of them.

    When `quoted` is a dict, it receives the quoted open item ids of each payer rank.
    """
    if D(txn['amount']) <= 0:
        return []
    horizon = None
    if horizon_days:
        horizon = (date.fromisoformat(txn['booking_date']) - timedelta(days=horizon_days)).isoformat()
    cash = _cents(abs(D(txn['amount'])))
    parties, refs = _evidence(txn, ctx)
    tokens = _tokens(txn)
    out = []
    for rank, pid in enumerate(parties):
        invoices, credits, related = _book(ctx, txn, pid)
        if not credits or not invoices:
            continue
        # A unit is an invoice with all its open credit notes, and its net in cents.
        units = []
        for inv in invoices:
            rel = related.get(_normed(ctx, inv, 'document_number'), ())
            units.append((inv, list(rel), _cents(inv['open_amount']) - sum(_cents(c['open_amount']) for c in rel)))
        by_doc = {_normed(ctx, u[0], 'document_number'): u for u in units}
        # A zero-net pair may be a group's only credit evidence only on a receipt that
        # mentions none of the payer's documents (explicit lists never carry the pair).
        zero_only = zeros and not refs and not _mentions(tokens, by_doc)
        q_inv = [u for u in units if u[0]['item_id'] in refs]
        q_cn = [c for c in credits if c['item_id'] in refs]
        if quoted is not None:
            quoted[rank] = {u[0]['item_id'] for u in q_inv} | {c['item_id'] for c in q_cn}
        anchor_ids = {u[0]['item_id'] for u in q_inv}
        for c in q_cn:
            unit = by_doc.get(_normed(ctx, c, 'related_document'))
            if unit is not None:
                anchor_ids.add(unit[0]['item_id'])
        anchors = [u for u in units if u[0]['item_id'] in anchor_ids]
        # quoted_credit: quoted credits plus the invoices they reference (and quoted invoices).
        if q_cn and anchors:
            explicit = [inv for inv, _, _ in anchors] + q_cn
            if sum(_signed(i) for i in explicit) == cash:
                out.append(('quoted_credit', rank, tuple(sorted({i['item_id'] for i in explicit}))))
            if sum(u[2] for u in anchors) == cash:
                out.append(('quoted_credit', rank, _ids(anchors)))
        # quoted_invoice: quoted invoices plus every open credit that references one of them.
        if q_inv and any(u[1] for u in q_inv) and sum(u[2] for u in q_inv) == cash:
            out.append(('quoted_invoice', rank, _ids(q_inv)))
        # single_net: one invoice and all its open credit notes.
        for u in units:
            if u[1] and u[2] == cash:
                out.append(('single_net', rank, _ids([u])))
        # run_net: contiguous issue-date runs of units (at least two, one with a credit).
        # A fully credited invoice (zero net) inside a run travels with it; a negative unit
        # splits the book. gap_run: the same with at most GAP_UNITS skipped inner units.
        if runs and len(units) <= GAP_BOOK:
            # Runs keep the subset horizon: a span reaching documents years old is a coincidence.
            recent = [u for u in units if horizon is None or u[0].get('issue_date', '') >= horizon]
            for gaps, run in _runs(recent, cash, GAP_UNITS, zero_only):
                out.append(('run_net' if not gaps else 'gap_run', rank, _ids(run)))
        # unit_subset: quoted units are anchors; the remaining positive units form a bounded
        # exact subset with a unique solution.
        rest = [u for u in units if u[0]['item_id'] not in anchor_ids and u[2] > 0
                and (horizon is None or u[0].get('issue_date', '') >= horizon)]
        target = cash - sum(u[2] for u in anchors)
        if target == 0 and anchors:
            sols = [()]
        elif target > 0 and len(rest) <= SUBSET_UNITS:
            sols = subset_solutions([u[2] for u in rest], target, minimum=1, maximum_items=SUBSET_UNITS) or []
        else:
            sols = []
        if len(sols) > 1:
            out.append(('unit_subset_multi', rank, ()))
        elif sols:
            chosen = anchors + [rest[k] for k in sols[0]]
            real = any(u[1] for u in chosen)
            chosen = _with_inner_zeros(units, chosen) if zeros else chosen
            # Without a quoted anchor, a large amount-only combination is too weak.
            if (real or (zero_only and any(u[1] for u in chosen))) and (anchors or len(chosen) <= FREE_UNITS):
                out.append(('unit_subset', rank, _ids(chosen)))
        # anchored_rest: quoted anchors and a remainder of at most REMAINDER_UNITS further units,
        # searched over a book too large for the full subset search.
        if anchors and target > 0 and len(rest) > SUBSET_UNITS:
            found = _small_subsets([u[2] for u in rest], target, REMAINDER_UNITS)
            if len(found) > 1:
                out.append(('anchored_rest_multi', rank, ()))
            elif found:
                chosen = anchors + [rest[k] for k in found[0]]
                real = any(u[1] for u in chosen)
                chosen = _with_inner_zeros(units, chosen) if zeros else chosen
                if real or (zero_only and any(u[1] for u in chosen)):
                    out.append(('anchored_rest', rank, _ids(chosen)))
    return out


GAP_UNITS = 2   # inner units a run may skip (paid elsewhere)
GAP_BOOK = 50   # largest unit book searched for runs


def _with_inner_zeros(units, chosen):
    """Add fully credited invoices (zero net) issued strictly inside the chosen date span."""
    dates = [u[0].get('issue_date', '') for u in chosen]
    if not dates:
        return chosen
    lo, hi = min(dates), max(dates)
    ids = {u[0]['item_id'] for u in chosen}
    extra = [u for u in units if u[1] and u[2] == 0 and u[0]['item_id'] not in ids
             and lo < u[0].get('issue_date', '') < hi]
    return chosen + extra


def _tokens(txn):
    """Reference-like tokens of a receipt: at least five characters, four of them digits."""
    out = set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', M.text_of(txn)):
        n = M.norm(token)
        if len(n) >= 5 and sum(ch.isdigit() for ch in n) >= 4:
            out.add(n)
    return out


def _mentions(tokens, by_doc):
    """Some token is a payer document number, its tail, or a one-character typo of it."""
    for number in by_doc:
        for token in tokens:
            if token == number or (len(token) >= 5 and number.endswith(token)) or _near(token, number):
                return True
    return False


def _runs(units, cash, max_gaps, zero_only=True):
    """Issue-date runs of positive units with at most `max_gaps` skipped inner units.

    Zero-net units inside the span travel with the run; a negative unit splits the book.
    Each run has at least two positive units and one unit with a credit note.
    Yields (number of skipped units, units).
    """
    found = {}
    segment = []
    segments = []
    for unit in units:
        if unit[2] < 0:
            segments.append(segment)
            segment = []
        else:
            segment.append(unit)
    segments.append(segment)
    for seq in segments:
        pos = [k for k, u in enumerate(seq) if u[2] > 0]
        values = [seq[k][2] for k in pos]
        top = max(values) if values else 0
        for a in range(len(pos)):
            total = 0
            for b in range(a, len(pos)):
                total += values[b]
                diff = total - cash
                if diff < 0 or b == a:
                    continue
                if diff > top * max_gaps:
                    break
                skips = [()] if diff == 0 else []
                if diff and max_gaps >= 1:
                    skips += [(k,) for k in range(a + 1, b) if values[k] == diff]
                if diff and max_gaps >= 2:
                    seen = {}
                    for k in range(a + 1, b):
                        other = seen.get(diff - values[k])
                        if other is not None:
                            skips.append((other, k))
                        seen.setdefault(values[k], k)
                for skip in skips:
                    if len(skip) > max_gaps:
                        continue
                    run = [seq[k] for k in range(pos[a], pos[b] + 1)
                           if seq[k][2] == 0 or k not in {pos[j] for j in skip}]
                    run = [u for u in run if u[2] > 0 or u[1]]
                    if not any(u[1] for u in run if u[2] > 0) and not (zero_only and any(u[1] for u in run)):
                        continue
                    key = _ids(run)
                    found.setdefault(key, (len(skip), run))
            if len(found) > 3:
                break
    return list(found.values())


def _small_subsets(values, target, size):
    """Up to two index tuples of at most `size` values summing to target (size <= 3)."""
    found = []
    where = defaultdict(list)
    for k, v in enumerate(values):
        where[v].append(k)
    for k in where.get(target, ()):
        found.append((k,))
    n = len(values)
    if size >= 2:
        for a in range(n):
            for b in where.get(target - values[a], ()):
                if b > a:
                    found.append((a, b))
            if len(found) > 1:
                return found
    if size >= 3 and len(found) <= 1 and n <= 200:
        for a in range(n):
            for b in range(a + 1, n):
                for c in where.get(target - values[a] - values[b], ()):
                    if c > b:
                        found.append((a, b, c))
                if len(found) > 1:
                    return found
    return found


FAMILIES = ('quoted_credit', 'quoted_invoice', 'single_net', 'run_net', 'unit_subset', 'anchored_rest', 'gap_run')


def reconcile(txn, ctx):
    """First family (in FAMILIES order) with exactly one solution across payer candidates.

    Contiguous and gapped runs share one uniqueness pool: a gapless run is taken at its
    place in the order only when no gapped alternative exists, a gapped run only last.
    """
    found = candidates(txn, ctx, HORIZON_DAYS)
    runs = {c[2] for c in found if c[0] in ('run_net', 'gap_run')}
    for family in FAMILIES:
        if any(c[0] == family + '_multi' for c in found):
            return None
        solutions = {c[2] for c in found if c[0] == family}
        if family in ('run_net', 'gap_run') and solutions:
            if len(runs) > 1:
                return None
        if len(solutions) > 1:
            return None
        if len(solutions) == 1:
            ids = next(iter(solutions))
            return [M.row(txn, iid, ctx.items[iid]['open_amount']) for iid in ids]
    return None


EARLY_FAMILIES = ('quoted_credit', 'quoted_invoice', 'single_net', 'unit_subset', 'anchored_rest')


def _near(a, b):
    """Same-length document numbers that differ in exactly one character (a typo)."""
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def reconcile_early(txn, ctx):
    """Exact credit-note nets that run before ordinary matching (POLICIES 2, 5.9).

    Ordinary matching reads such a receipt as a short payment (partial, withholding)
    or an overpayment of one quoted invoice. A unique exact family solution that
    includes the related credit notes explains the cash difference instead. Only the
    quoted/single families and anchored subsets run here; every quoted open item of
    the payer must be in the solution, unless it is a one-character typo of one.
    """
    quoted = {}
    found = candidates(txn, ctx, HORIZON_DAYS, quoted, runs=False, zeros=False)
    for family in EARLY_FAMILIES:
        if any(c[0] == family + '_multi' for c in found):
            return None
        sols = {(c[1], c[2]) for c in found if c[0] == family}
        if family in ('unit_subset', 'anchored_rest'):
            sols = {s for s in sols if quoted.get(s[0])}
        if len({s[1] for s in sols}) > 1:
            return None
        if sols:
            rank, ids = next(iter(sols))
            # Quoted documents that already settle the cash exactly are an ordinary payment;
            # an extra invoice/credit pair of equal amounts would be unexplained noise.
            printed = [ctx.items[i] for i in quoted.get(rank, ())]
            if printed and sum(_signed(i) for i in printed) == _cents(abs(D(txn['amount']))) \
                    and set(ids) != {i['item_id'] for i in printed}:
                return None
            chosen = {_normed(ctx, ctx.items[i], 'document_number') for i in ids}
            for iid in quoted.get(rank, ()):
                if iid in ids:
                    continue
                number = _normed(ctx, ctx.items[iid], 'document_number')
                if not any(_near(number, other) for other in chosen):
                    return None
            return [M.row(txn, iid, ctx.items[iid]['open_amount']) for iid in ids]
    return None

