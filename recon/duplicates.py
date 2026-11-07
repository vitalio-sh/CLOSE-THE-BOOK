"""Duplicate payment of a recently closed invoice; no target labels or balance writes.

POLICIES §3 DUPLICATE_PAYMENT: the item had already been settled and is paid again;
amount = what was paid again. Runs after every other handler, so it only sees lines
that no open item explained.

Evidence, all observable at runtime:
- the payer is identified (party_candidates, evidence order);
- no open item of any candidate payer has this amount (open or total), so the line
  is not a same-amount family such as monthly fees (POLICIES §5.6);
- exactly one invoice of the first payer that has a match was closed before the phase
  (absent from the authoritative opening snapshot, see history.enrich_referenced_closed),
  issued at most RECENT_CLOSED_DAYS before the line, same entity and currency, and its
  total equals the cash;
- that invoice was not collected by direct debit: a second payment of a collected
  receipt is the transfer after a returned debit (POLICIES §5.10), not a duplicate.
"""
from datetime import date
from decimal import Decimal

from .history import RECENT_CLOSED_DAYS
from .matching import party_candidates, row, dec, CENT
from .context import norm


class ClosedDuplicates:
    def __init__(self, ctx):
        self.ctx = ctx

    def reconcile(self, txn):
        ctx = self.ctx
        closed = getattr(ctx, 'recent_closed', None) or {}
        amount = dec(txn.get('amount'))
        cash = abs(amount)
        if cash <= 0:
            return None
        side = 'AR' if amount > 0 else 'AP'
        parties = party_candidates(txn, ctx)
        if not parties:
            return None
        currency = txn.get('currency')
        owner = txn.get('entity')
        for pid in parties:
            for iid in ctx.by_party.get(pid, ()):
                item = ctx.items[iid]
                if item.get('side') != side or item.get('currency') != currency:
                    continue
                if dec(item.get('open_amount')) > 0 and (
                        abs(dec(item.get('open_amount')) - cash) <= CENT
                        or abs(dec(item.get('total_amount')) - cash) <= CENT):
                    return None
        try:
            booking = date.fromisoformat(txn['booking_date'])
        except (KeyError, ValueError):
            return None
        for pid in parties:
            hits = []
            for record in closed.get(pid, ()):
                iid = record['item_id']
                if ('AR' if iid.startswith('AR') else 'AP') != side:
                    continue
                if record.get('currency') != currency or dec(record.get('total_amount')) != cash:
                    continue
                entity = record.get('entity')
                if entity != owner and not (owner == 'ES00' and side == 'AP' and entity in ('ES01', 'ES02', 'PT01')):
                    continue
                try:
                    age = (booking - date.fromisoformat(record['issue_date'])).days
                except (KeyError, ValueError):
                    continue
                if 0 <= age <= RECENT_CLOSED_DAYS:
                    hits.append(record)
            # Items of the payer already settled in full (in this phase or restored as
            # historical closed): a second identical payment of one of them is a duplicate.
            seen = {record['item_id'] for record in hits}
            for iid in ctx.by_party.get(pid, ()):
                item = ctx.items.get(iid)
                if (not item or iid in seen or item.get('side') != side or item.get('kind') == 'credit'
                        or item.get('currency') != currency or dec(item.get('open_amount')) != 0
                        or dec(item.get('total_amount')) != cash):
                    continue
                entity = item.get('entity')
                if entity and entity != owner and not (owner == 'ES00' and side == 'AP' and entity in ('ES01', 'ES02', 'PT01')):
                    continue
                try:
                    age = (booking - date.fromisoformat(item['issue_date'])).days
                except (KeyError, ValueError, TypeError):
                    continue
                if 0 <= age <= RECENT_CLOSED_DAYS:
                    hits.append(item)
                    seen.add(iid)
            if not hits:
                continue
            if len(hits) != 1 or hits[0].get('payment_method') == 'dd':
                return None
            record = hits[0]
            iid = record['item_id']
            if iid not in ctx.items:
                ctx.add_item(dict(record, open_amount=Decimal('0'), historical_closed=True))
                ctx.by_number[norm(ctx.items[iid]['document_number'])].append(iid)
                ctx.by_party[ctx.items[iid]['party_id']].append(iid)
            return [row(txn, iid, cash, 'DUPLICATE_PAYMENT')]
        return None
