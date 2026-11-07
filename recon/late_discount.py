"""Early-payment discount taken after the discount window (POLICIES §3).

A receipt whose cash equals exactly one open invoice of the identified payer
*less its terms discount*, booked after issue + discount_days + 3, is not a
SHORT_DISCOUNT: the invoice stays open for the difference and the row is
PARTIAL at the cash collected. Ported from solution/eltriki-x (bankrec receipts).

Precision guards: payer must be identified, the invoice fully open, same
currency (no FX), terms with a discount, and exactly one candidate invoice.
Runs last, so it only sees receipts every other handler abstained on.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .matching import _cash, _eligible, dec, party_candidates, row

CENT = Decimal('0.01')


def reconcile(txn, ctx):
    if dec(txn['amount']) <= 0:
        return None
    parties = party_candidates(txn, ctx)
    if not parties:
        return None
    terms_by_code = getattr(ctx, 'payment_terms', {}) or {}
    try:
        booked = date.fromisoformat(txn['booking_date'])
    except (KeyError, ValueError):
        return None
    hits = set()
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items.get(iid) or {}
            if item.get('side', 'AR') != 'AR' or item.get('kind') != 'invoice' or not _eligible(item, txn):
                continue
            total = dec(item.get('total_amount'))
            if total <= 0 or abs(dec(item.get('open_amount', total)) - total) > CENT:
                continue
            cash, fx = _cash(item, txn)
            if cash is None or fx:
                continue
            terms = terms_by_code.get(item.get('payment_terms')) or {}
            try:
                pct = Decimal(str(terms.get('early_payment_discount_pct') or 0))
                days = int(terms.get('discount_days') or 0)
                elapsed = (booked - date.fromisoformat(item['issue_date'])).days
            except (ValueError, KeyError, ArithmeticError):
                continue
            if pct <= 0:
                continue
            expected = total - (total * pct / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(cash - expected) <= CENT and elapsed > days + 3:
                hits.add(iid)
    if len(hits) != 1:
        return None
    iid = hits.pop()
    cash, _ = _cash(ctx.items[iid], txn)
    return [row(txn, iid, cash, 'PARTIAL')]
