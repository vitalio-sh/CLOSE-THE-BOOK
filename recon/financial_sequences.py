"""Document-supported financial sequence fallback; no target labels or balance writes."""
from decimal import Decimal, ROUND_HALF_UP
from .matching import party_candidates, _eligible, _cash, row, dec, CENT, text_of, norm, words
import re


class FinancialSequences:
    def __init__(self, ctx):
        self.ctx = ctx
        self.facts = getattr(ctx, 'pdf_facts', None)

    def retention(self, txn):
        ctx = self.ctx
        plausible = []
        refs = self.explicit_refs(txn)
        if len(refs)>1:return None
        for pid in party_candidates(txn, ctx):
            for iid in ctx.by_party.get(pid, ()):
                item = ctx.items[iid]
                if item.get('side') != 'AR' or item.get('kind') != 'invoice' or not _eligible(item, txn):
                    continue
                if refs and iid not in refs:continue
                cash, fx = _cash(item, txn)
                total = dec(item.get('total_amount'))
                opened = dec(item.get('open_amount'))
                if cash is None or opened != total or not 0 < cash < opened:
                    continue
                # Ratios only shortlist PDFs; they never establish a match.
                if any(abs(cash - (total * ratio).quantize(CENT, rounding=ROUND_HALF_UP)) <= CENT
                       for ratio in (Decimal('.90'), Decimal('.95'))):
                    plausible.append((item, cash))
        if not plausible:
            return None
        if self.facts is None or not self.facts.enabled:
            return None
        supported = []
        for item, cash in plausible:
            facts = self.facts.get(item['item_id'])
            if not facts.get('source') or not facts.get('net_due') or not facts.get('retention_pct'):
                continue
            pct = dec(facts['retention_pct'])
            if pct not in (Decimal(5), Decimal(10)):
                continue
            if abs(cash - dec(facts['net_due'])) > CENT:
                continue
            retained = (dec(item['total_amount']) * pct / 100).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(dec(item['total_amount']) - retained - cash) > CENT:
                continue
            supported.append(row(txn, item['item_id'], cash, 'PARTIAL'))
        return supported if len(supported) == 1 else None

    def explicit_refs(self, txn):
        from .financial_references import finance_reference_sets
        return finance_reference_sets(txn,self.ctx,self.facts)['all']

    def reconcile(self, txn):
        raw=words(text_of(txn))
        if any(x in raw for x in ('DISPUT','RECLAM','DAMAGED','PENALTY','DEDUCCION')):
            return None
        return self.retention(txn)
