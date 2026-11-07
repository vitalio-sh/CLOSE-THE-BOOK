"""Unreferenced cross-border receipt short by a fixed intermediary fee; no balance writes.

POLICIES §3 SHORT_BANK_CHARGES: intermediary charges on a cross-border receipt; the
invoice is settled in full (amount = open amount). Ordinary matching needs a reference
or an exact amount, so a foreign payer's receipt that quotes nothing and arrives short
by the fee reaches the UNIDENTIFIED fallback.

Evidence, all observable at runtime:
- the line is a cross-border credit transfer (text marker or ISO code XBCT);
- the payer is identified (party_candidates);
- exactly one open AR invoice of those payers, same entity and currency, exceeds the
  cash by a fee of the fixed schedule (matching.sbc_fee_lattice; TRAIN: every golden
  case, and no other exception class, on cross-border lines).
"""
from .matching import party_candidates, row, dec, sbc_fee_lattice, cross_border_transfer


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
        parties = party_candidates(txn, ctx)
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
