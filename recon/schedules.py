"""Contractual payment schedules documented in the invoice PDF (POLICIES §5.8, §3).

Only the invoice PDF says that an invoice is paid in instalments ("Forma de pago: 3
vencimientos ...", "Payment terms: 4 instalments ..."). The PDF prints the plan: every
instalment is the total divided by n rounded DOWN to the cent, and the last instalment
carries the remainder (TRAIN: 13,335 of 13,335 instalment allocations follow this).

POLICIES §3: every instalment except the **last** of the plan is ``PARTIAL``; the last
one has no exception. The last is the instalment with index n of the plan, which is not
always the payment that clears the balance (instalments can arrive out of order). The
applied amount is always the instalment that was paid.

The same PDF clauses also make two other first payments PARTIAL (POLICIES §3): an
invoice that deducts an advance received against a proforma (the cash is the printed net
amount due), and an invoice with a 5-10 % guarantee retention (the cash is the total less
the retention, rounded half up; TRAIN 2,692 of 2,692 within a cent). These run before
ordinary matching so a documented retention is not read as a withholding-tax lookalike.

The module proposes rows only; it never reads labels and never changes balances.
"""
from decimal import Decimal as D, ROUND_DOWN, ROUND_HALF_UP
import re
import unicodedata

from .matching import CENT, dec, norm, party_candidates, row, text_of, words, _cash, _eligible

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


def near_referenced(item, raw):
    """POLICIES §5.5 typos: one wrong or two transposed characters in our document number."""
    number = norm(item.get('document_number'))
    digits = re.sub(r'^[A-Z]+', '', number)
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw):
        key = norm(token)
        if len(key) < 7 or sum(c.isdigit() for c in key) < 6:
            continue
        for target in {number, digits}:
            if len(target) != len(key) or target == key:
                continue
            diff = [i for i, (a, b) in enumerate(zip(key, target)) if a != b]
            if len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and
                                  key[diff[0]] == target[diff[1]] and key[diff[1]] == target[diff[0]]):
                return True
    return False


class Schedules:
    def __init__(self, ctx):
        self.ctx = ctx
        self.facts = getattr(ctx, 'pdf_facts', None)

    def facts_of(self, item):
        if self.facts is None or not getattr(self.facts, 'enabled', False):
            return {}
        if item.get('side') != 'AR' or item.get('kind') != 'invoice':
            return {}
        return self.facts.get(item['item_id']) or {}

    def plan(self, item):
        facts = self.facts_of(item)
        return instalment_plan(item, facts) if facts else None

    def expected(self, item, txn, index):
        """(applied amount, exception) when the cash fits the documented contract, else None."""
        facts = self.facts_of(item)
        if not facts:
            return None
        cash, fx = _cash(item, txn)
        opened = dec(item.get('open_amount'))
        total = dec(item.get('total_amount'))
        if cash is None or opened <= 0 or cash > opened + CENT:
            return None
        plan = instalment_plan(item, facts)
        if plan and cash in (plan[1], plan[2]):
            if index is not None and index[1] != plan[0]:
                return None
            label = instalment_label(plan, cash, opened, index)
            if fx and label == '':
                # FX_DIFFERENCE belongs only to an invoice row that is fully cleared.
                if abs(cash - opened) > CENT:
                    return None
                label = 'FX_DIFFERENCE'
            return cash, label
        # POLICIES §3: the payment of an invoice whose PDF deducts a proforma advance,
        # and the first payment of an invoice with a guarantee retention, are PARTIAL.
        if opened != total:
            return None
        if facts.get('advance_amount') and facts.get('net_due'):
            net = dec(facts['net_due'])
            if abs(total - dec(facts['advance_amount']) - net) <= CENT and abs(cash - net) <= CENT:
                return cash, 'PARTIAL'
        if facts.get('retention_pct'):
            pct = dec(facts['retention_pct'])
            if D(5) <= pct <= D(10):
                # TRAIN: the payer pays total x (1 - pct) rounded half up; the printed
                # net can be a cent away because the PDF rounds the retention down.
                payable = (total * (1 - pct / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
                if abs(cash - payable) <= CENT or (facts.get('net_due') and abs(cash - dec(facts['net_due'])) <= CENT):
                    return cash, 'PARTIAL'
        return None

    def contract(self, txn):
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
            cited = referenced(item, raw, compact)
            fit = self.expected(item, txn, index)
            if fit is not None:
                candidates.append((item, fit, cited, cited or near_referenced(item, raw)))
                continue
            if cited:
                cited_other.append(item)
            cash, _ = _cash(item, txn)
            opened = dec(item.get('open_amount'))
            if cash is not None and opened > 0 and abs(cash - opened) <= CENT:
                exact_other.append(item)
        if not candidates:
            return None
        cited = [c for c in candidates if c[2]]
        near = [c for c in candidates if c[3]]
        if cited:
            candidates = cited
        elif len(near) == 1 and not exact_other and all(
                (_cash(i, txn)[0] or 0) > dec(i.get('open_amount')) for i in cited_other):
            # A mistyped number of the only invoice whose contract fits the cash, when the
            # cited document could only take this cash as an overpayment.
            candidates = near
        elif cited_other or exact_other:
            # Another document is cited, or another open item has exactly this amount:
            # the contract amount alone is not enough evidence.
            return None
        if len(candidates) != 1:
            return None
        item, (amount, exception) = candidates[0][:2]
        return [row(txn, item['item_id'], amount, exception)]

    def reconcile(self, txn):
        return self.contract(txn)
