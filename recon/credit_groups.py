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
from collections import defaultdict
from decimal import Decimal

from . import matching as M
from .subsets import subset_solutions

D = Decimal


SUBSET_UNITS = 22   # meet-in-the-middle budget: 2 x 2048 partial sums
FREE_UNITS = 8      # largest unanchored unit set accepted from an exact sum


def _cents(value):
    return int((D(str(value)) * 100).to_integral_value())


def _signed(item):
    return -_cents(item['open_amount']) if item.get('kind') == 'credit' else _cents(item['open_amount'])


def _evidence(txn, ctx):
    """Payer candidates and quoted item ids, mirroring matching.reconcile."""
    raw = M.text_of(txn)
    compact = M.norm(raw)
    parties = M.party_candidates(txn, ctx)
    refs = set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw):
        refs.update(ctx.by_number.get(M.norm(token), ()))
    numeric_tokens = set(re.findall(r'\b\d{5,}\b', raw))
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            n = M.norm(item.get('document_number'))
            if len(n) >= 5 and n in compact:
                refs.add(iid)
            elif any(len(v) >= 5 and n.endswith(v) for v in numeric_tokens):
                refs.add(iid)
            elif item.get('customer_po') and len(M.norm(item['customer_po'])) >= 6 and M.norm(item['customer_po']) in compact:
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
    for iid in ctx.by_party.get(pid, ()):
        item = ctx.items[iid]
        if (item.get('side') != 'AR' or item.get('entity') != txn.get('entity')
                or item.get('currency') != txn.get('currency')
                or item.get('issue_date', '') > txn.get('booking_date', '9999')
                or item['open_amount'] <= 0):
            continue
        (credits if item.get('kind') == 'credit' else invoices).append(item)
    cancelled = {M.norm(c.get('related_document')) for c in credits if _cancels(c)}
    credits = [c for c in credits if not _cancels(c)]
    invoices = [i for i in invoices if M.norm(i.get('document_number')) not in cancelled]
    invoices.sort(key=lambda i: (i.get('issue_date', ''), i['item_id']))
    related = defaultdict(list)
    for credit in credits:
        key = M.norm(credit.get('related_document'))
        if key:
            related[key].append(credit)
    return invoices, credits, related


def _with_credits(invoices, related):
    items = list(invoices)
    for inv in invoices:
        items.extend(related.get(M.norm(inv.get('document_number')), ()))
    return items


def candidates(txn, ctx):
    """Every family solution as (family, party rank, items). Diagnostics use all of them."""
    if D(txn['amount']) <= 0:
        return []
    cash = _cents(abs(D(txn['amount'])))
    parties, refs = _evidence(txn, ctx)
    out = []
    for rank, pid in enumerate(parties):
        invoices, credits, related = _book(ctx, txn, pid)
        if not credits or not invoices:
            continue
        by_doc = {M.norm(i.get('document_number')): i for i in invoices}
        q_inv = [i for i in invoices if i['item_id'] in refs]
        q_cn = [c for c in credits if c['item_id'] in refs]
        # quoted_credit: quoted credits plus the invoices they reference (and quoted invoices).
        if q_cn:
            anchors = {i['item_id']: i for i in q_inv}
            for c in q_cn:
                inv = by_doc.get(M.norm(c.get('related_document')))
                if inv is not None:
                    anchors[inv['item_id']] = inv
            if anchors:
                base = list(anchors.values())
                for variant in (base + q_cn, _with_credits(base, related)):
                    ids = {i['item_id'] for i in variant}
                    if sum(_signed(i) for i in {i['item_id']: i for i in variant}.values()) == cash:
                        out.append(('quoted_credit', rank, tuple(sorted(ids))))
        # quoted_invoice: quoted invoices plus every open credit that references one of them.
        if q_inv:
            items = {i['item_id']: i for i in _with_credits(q_inv, related) + q_cn}
            if len(items) > len(q_inv) and sum(_signed(i) for i in items.values()) == cash:
                out.append(('quoted_invoice', rank, tuple(sorted(items))))
        # single_net: one invoice and all its open credit notes.
        for inv in invoices:
            rel = related.get(M.norm(inv.get('document_number')), ())
            if rel and _cents(inv['open_amount']) - sum(_cents(c['open_amount']) for c in rel) == cash:
                out.append(('single_net', rank, tuple(sorted([inv['item_id']] + [c['item_id'] for c in rel]))))
        # run_net: contiguous issue-date runs of invoices plus their open credits.
        n = len(invoices)
        if n <= 400:
            inv_c = [_cents(i['open_amount']) for i in invoices]
            rel_c = [sum(_cents(c['open_amount']) for c in related.get(M.norm(i.get('document_number')), ())) for i in invoices]
            for a in range(n):
                total = 0
                has_credit = False
                for b in range(a, n):
                    if inv_c[b] - rel_c[b] <= 0:
                        break  # zero-net veto: a run never carries a fully credited invoice
                    total += inv_c[b] - rel_c[b]
                    has_credit = has_credit or rel_c[b] > 0
                    if b > a and has_credit and total == cash:
                        run = invoices[a:b + 1]
                        out.append(('run_net', rank, tuple(sorted(i['item_id'] for i in _with_credits(run, related)))))
        # unit_subset: quoted units are anchors; the remaining units (an invoice with all its
        # open credits, net strictly positive) form a bounded exact subset with a unique solution.
        units = []
        for inv in invoices:
            rel = related.get(M.norm(inv.get('document_number')), ())
            units.append((inv, list(rel), _cents(inv['open_amount']) - sum(_cents(c['open_amount']) for c in rel)))
        anchor_ids = {i['item_id'] for i in q_inv}
        for c in q_cn:
            inv = by_doc.get(M.norm(c.get('related_document')))
            if inv is not None:
                anchor_ids.add(inv['item_id'])
        anchors = [u for u in units if u[0]['item_id'] in anchor_ids]
        rest = [u for u in units if u[0]['item_id'] not in anchor_ids and u[2] > 0]
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
            # Without a quoted anchor, a large amount-only combination is too weak.
            if any(u[1] for u in chosen) and (anchors or len(chosen) <= FREE_UNITS):
                ids = []
                for u in chosen:
                    ids.append(u[0]['item_id'])
                    ids.extend(c['item_id'] for c in u[1])
                out.append(('unit_subset', rank, tuple(sorted(ids))))
    return out


FAMILIES = ('quoted_credit', 'quoted_invoice', 'single_net', 'run_net', 'unit_subset')


def reconcile(txn, ctx):
    """First family (in FAMILIES order) with exactly one solution across payer candidates."""
    found = candidates(txn, ctx)
    for family in FAMILIES:
        solutions = {c[2] for c in found if c[0] == family}
        if family == 'unit_subset' and any(c[0] == 'unit_subset_multi' for c in found):
            return None
        if len(solutions) > 1:
            return None
        if len(solutions) == 1:
            ids = next(iter(solutions))
            return [M.row(txn, iid, ctx.items[iid]['open_amount']) for iid in ids]
    return None
