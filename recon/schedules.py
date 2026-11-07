"""Contractual payment schedules documented in the invoice PDF (POLICIES §5.8, §3).

Only the invoice PDF says that an invoice is paid in instalments ("Forma de pago: 3
vencimientos ...", "Payment terms: 4 instalments ..."). The PDF prints the plan: every
instalment is the total divided by n rounded DOWN to the cent, and the last instalment
carries the remainder (TRAIN: 13,335 of 13,335 instalment allocations follow this).

POLICIES §3: every instalment except the **last** of the plan is ``PARTIAL``; the last
one has no exception. The last is the instalment with index n of the plan, which is not
always the payment that clears the balance (instalments can arrive out of order). The
applied amount is always the instalment that was paid.

The module proposes rows only; it never reads labels and never changes balances.
"""
from decimal import Decimal, ROUND_DOWN
import re
import unicodedata

from .matching import CENT, dec, norm, party_candidates, row, text_of, words, _eligible

INDEX_RE = re.compile(
    r'(?<![A-Z0-9])(?:PLAZO|PLAZOS|INSTAL+MENT|INSTAL+MENTS|VENCIMIENTO|VTO|CUOTA|ECHEANCE|'
    r'ECHEANCES|RATE|RATA|TEILZAHLUNG|PRESTACAO|TERM)\.?\s*(?:NO\.?\s*|N\s*)?(\d{1,2})\s*/\s*(\d{1,2})(?![0-9])')
SKIP_WORDS = ('DISPUT', 'RECLAM', 'DAMAGED', 'PENALTY', 'DEDUCCION', 'DEVOLUCION RECIBO',
              'RUECKLASTSCHRIFT', 'RETURNED', 'CHARGEBACK', 'IMPAYE')


def instalment_plan(item, facts):
    """Return (n, share, last) for a documented plan of at least two instalments."""
    try:
        n = int((facts or {}).get('instalments_count') or 0)
    except (TypeError, ValueError):
        return None
    total = dec(item.get('total_amount'))
    if n < 2 or total <= 0:
        return None
    share = (total / n).quantize(CENT, rounding=ROUND_DOWN)
    return n, share, total - share * (n - 1)


def stated_index(txn):
    """Instalment index stated by the payer, e.g. 'PLAZO 2/3' or 'INSTALMENT 1/2'."""
    text = ' | '.join(str(txn.get(k) or '') for k in ('text', 'ref1', 'ref2', 'remittance_info'))
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().upper()
    found = {(int(k), int(n)) for k, n in INDEX_RE.findall(text) if 1 <= int(k) <= int(n)}
    return found.pop() if len(found) == 1 else None


def instalment_label(plan, cash, opened, index=None):
    """POLICIES §3: PARTIAL for every instalment but the last of the plan."""
    n, share, last = plan
    if index is not None and index[1] == n:
        return '' if index[0] == n else 'PARTIAL'
    if last != share:
        # Exact: the remainder often differs from the share by a single cent.
        return '' if cash == last else 'PARTIAL'
    return '' if abs(cash - opened) <= CENT else 'PARTIAL'


def referenced(item, raw, compact):
    """Our document number (or the customer's PO) is visible, possibly truncated or unprefixed."""
    number = norm(item.get('document_number'))
    if len(number) >= 5 and number in compact:
        return True
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw):
        key = norm(token)
        if len(key) >= 7 and any(c.isdigit() for c in key) and len(number) > len(key) >= max(7, len(number) * 0.6) \
                and number.startswith(key):
            return True
    if any(len(v) >= 5 and number.endswith(v) for v in re.findall(r'(?<![0-9])\d{5,}(?![0-9])', raw)):
        return True
    po = norm(item.get('customer_po'))
    return len(po) >= 6 and po in compact


class Schedules:
    def __init__(self, ctx):
        self.ctx = ctx
        self.facts = getattr(ctx, 'pdf_facts', None)

    def plan(self, item):
        if self.facts is None or not getattr(self.facts, 'enabled', False):
            return None
        if item.get('side') != 'AR' or item.get('kind') != 'invoice':
            return None
        return instalment_plan(item, self.facts.get(item['item_id']))

    def instalment(self, txn):
        ctx = self.ctx
        if dec(txn.get('amount')) <= 0:
            return None
        raw = text_of(txn)
        if any(x in words(raw) for x in SKIP_WORDS):
            return None
        compact = norm(raw)
        parties = party_candidates(txn, ctx)
        scoped = []
        for pid in parties:
            scoped.extend(ctx.by_party.get(pid, ()))
        # A payer that is not identified can still cite our exact document number.
        for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw):
            scoped.extend(ctx.by_number.get(norm(token), ()))
        index = stated_index(txn)
        candidates, exact_other, cited_other = [], [], []
        for iid in dict.fromkeys(scoped):
            item = ctx.items.get(iid)
            if item is None or not _eligible(item, txn):
                continue
            if item.get('currency') != txn.get('currency'):
                continue
            cash = abs(dec(txn['amount']))
            opened = dec(item.get('open_amount'))
            cited = referenced(item, raw, compact)
            plan = self.plan(item) if opened > 0 else None
            if plan and cash <= opened + CENT and cash in (plan[1], plan[2]):
                if index is not None and index[1] != plan[0]:
                    continue
                candidates.append((item, plan, cash, opened, cited))
                continue
            if cited:
                cited_other.append(item)
            if opened > 0 and abs(cash - opened) <= CENT:
                exact_other.append(item)
        if not candidates:
            return None
        cited = [c for c in candidates if c[4]]
        if cited:
            candidates = cited
        elif cited_other or exact_other:
            # Another document is cited, or another open item has exactly this amount:
            # the schedule amount alone is not enough evidence.
            return None
        if len(candidates) != 1:
            return None
        item, plan, cash, opened, _ = candidates[0]
        return [row(txn, item['item_id'], cash, instalment_label(plan, cash, opened, index))]

    def reconcile(self, txn):
        return self.instalment(txn)
