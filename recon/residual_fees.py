"""Unreferenced cross-border receipt short by a fixed intermediary fee; no balance writes.

POLICIES §3 SHORT_BANK_CHARGES: intermediary charges on a cross-border receipt; the
invoice is settled in full (amount = open amount). Ordinary matching needs a reference
or an exact amount, so a foreign payer's receipt that quotes nothing and arrives short
by the fee reaches the UNIDENTIFIED fallback.

Evidence, all observable at runtime:
- the line is a cross-border credit transfer (text marker or ISO code XBCT);
- the payer is identified (party_candidates, or refnoise's payer search for short names);
- exactly one open AR invoice of those payers, same entity and currency, exceeds the
  cash by a fee of the fixed schedule (matching.sbc_fee_lattice; TRAIN: every golden
  case, and no other exception class, on cross-border lines).
"""
from .matching import party_candidates, row, dec, sbc_fee_lattice, cross_border_transfer, CENT


class ResidualFees:
    def __init__(self, ctx):
        self.ctx = ctx

    def reconcile(self, txn):
        ctx = self.ctx
        cash = dec(txn.get('amount'))
        if cash <= 0:
            return None
        currency = txn.get('currency')
        lattice = sbc_fee_lattice(currency)
        if not lattice or not cross_border_transfer(txn):
            return None
        # Short payer names (6-9 letters, e.g. 'LQS Spa SA') never reach the 10-letter
        # prefix index of party_candidates; refnoise's payer search covers them.
        parties = party_candidates(txn, ctx)
        if not parties:
            from .refnoise import payer_candidates
            parties = payer_candidates(txn, ctx)
        if not parties:
            return None
        entity = txn.get('entity')
        hits = []
        for pid in parties:
            for iid in ctx.by_party.get(pid, ()):
                item = ctx.items.get(iid)
                if (not item or item.get('side') != 'AR' or item.get('kind') == 'credit'
                        or item.get('currency') != currency):
                    continue
                if entity and item.get('entity') and item.get('entity') != entity:
                    continue
                opened = dec(item.get('open_amount'))
                if opened > 0 and opened - cash in lattice and iid not in hits:
                    hits.append(iid)
        if len(hits) != 1:
            return None
        iid = hits[0]
        return [row(txn, iid, ctx.items[iid]['open_amount'], 'SHORT_BANK_CHARGES')]


class ResidualDiscounts:
    """Unreferenced receipt = one invoice less its early-payment discount (POLICIES §3).

    SHORT_DISCOUNT: paid within the discount window with the terms discount deducted;
    amount = full open (the total). Ordinary matching looks for an exact set first and,
    for an unreferenced receipt, could take a subset of the payer's old small invoices
    that happens to sum to the cash. Evidence, all observable at runtime:
    - identified payer (party_candidates, or refnoise's payer search for short names);
    - no open invoice of the payer equals the cash (a full payment reading exists);
    - exactly one open AR invoice of the payer (same entity and currency), unpaid
      (open == total), with a discount term, booked within issue date .. discount_days + 3,
      and cash == total - round_half_up(total * pct) (the cent signature of the terms,
      as in matching._exception and documents._discount_within_terms).
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def reconcile(self, txn):
        from datetime import date, timedelta
        from decimal import ROUND_HALF_UP
        ctx = self.ctx
        cash = dec(txn.get('amount'))
        if cash <= 0:
            return None
        terms = getattr(ctx, 'payment_terms', {})
        if not isinstance(terms, dict) or not terms:
            return None
        parties = party_candidates(txn, ctx)
        if not parties:
            from .refnoise import payer_candidates
            parties = payer_candidates(txn, ctx)
        if not parties:
            return None
        try:
            booked = date.fromisoformat(txn['booking_date'])
        except (KeyError, ValueError):
            return None
        currency, entity = txn.get('currency'), txn.get('entity')
        hits = []
        for pid in parties:
            for iid in ctx.by_party.get(pid, ()):
                item = ctx.items.get(iid)
                if (not item or item.get('side') != 'AR' or item.get('kind') == 'credit'
                        or item.get('currency') != currency):
                    continue
                if entity and item.get('entity') and item.get('entity') != entity:
                    continue
                opened, total = dec(item.get('open_amount')), dec(item.get('total_amount'))
                if opened <= 0:
                    continue
                if abs(opened - cash) <= CENT:
                    return None
                term = terms.get(item.get('payment_terms')) or {}
                pct = dec(term.get('early_payment_discount_pct'))
                if not pct or opened != total:
                    continue
                expected = total - (total * pct / 100).quantize(CENT, rounding=ROUND_HALF_UP)
                if abs(expected - cash) > CENT:
                    continue
                try:
                    issued = date.fromisoformat(item['issue_date'])
                    deadline = issued + timedelta(days=int(term['discount_days']) + 3)
                except (KeyError, ValueError, TypeError):
                    continue
                if issued <= booked <= deadline and iid not in hits:
                    hits.append(iid)
        if len(hits) != 1:
            return None
        iid = hits[0]
        return [row(txn, iid, ctx.items[iid]['open_amount'], 'SHORT_DISCOUNT')]
