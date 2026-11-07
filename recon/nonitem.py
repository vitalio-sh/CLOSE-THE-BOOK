"""Lines that settle no item: frozen TRAIN category lookup, bank-reversal pairs and
text evidence for UNIDENTIFIED receipts.

POLICIES §4 (categories), §4 "Bank error and its reversal" (both lines are BANK_REVERSAL)
and §3 UNIDENTIFIED ("a receipt for which no open item exists ... One row: category
UNIDENTIFIED, exception UNIDENTIFIED").  Where POLICIES is silent, TRAIN history decides:
the lookup table and the generic-reference vocabulary below are mined from TRAIN golden
before 2024-10-01 by ``scripts/mine_nonitem_lookup.py`` (never from LIVE/FINAL labels).
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import re
import unicodedata

from .context import read_csv

try:
    from .nonitem_lookup import CATEGORY_LOOKUP
except ImportError:  # pragma: no cover - table is generated offline
    CATEGORY_LOOKUP = {}


def _ascii(value):
    return unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().upper()


def _sign(txn):
    return '+' if Decimal(str(txn.get('amount') or 0)) > 0 else '-'


def text_key(txn, words=3):
    """(sign, first words of the bank text without digits/punctuation) — the lookup key."""
    text = re.sub(r'[^A-Z ]+', ' ', re.sub(r'\d+', ' ', _ascii(txn.get('text'))))
    return _sign(txn), ' '.join(text.split()[:words])


def _row(txn, category, exception=''):
    return {'bank_txn_id': txn['bank_txn_id'], 'item_id': '', 'amount': '',
            'category': category, 'exception': exception}


# ---------------------------------------------------------------------------
# Bank error and its reversal (POLICIES §4): "ANULACION OPERACION INDEBIDA N <date+id>
# <original text>" cancels an earlier line of the same account with the opposite amount.
_REVERSAL = re.compile(r'ANULACION OPERACION INDEBIDA')


def _full_text(txn):
    return ' '.join(str(txn.get(k) or '') for k in ('text', 'ref1', 'ref2', 'remittance_info')).upper()


def is_reversal_line(txn):
    return bool(_REVERSAL.search(_full_text(txn)))


def reversal_originals(txns, max_days=31):
    """Map original bank_txn_id -> reversing bank_txn_id.

    Candidates: same account, exactly the opposite amount, booked on or before the
    reversal (within ``max_days``), and either quoted in the reversal text or booked on the
    date encoded after "N ".  Prefer quoted, then dated, then the latest.  An unquoted,
    undated opposite line is never paired.  Each original is used at most once.
    """
    by_key = defaultdict(list)
    reversals = []
    for txn in txns:
        if is_reversal_line(txn):
            reversals.append(txn)
        else:
            by_key[(txn.get('account_id'), Decimal(str(txn['amount'])))].append(txn)
    used, pairs = set(), {}
    for rev in sorted(reversals, key=lambda t: (t['booking_date'], t['bank_txn_id'])):
        text = _full_text(rev)
        coded = re.search(r'INDEBIDA N (\d{6,8})', text)
        rev_day = date.fromisoformat(rev['booking_date'])
        candidates = [t for t in by_key[(rev.get('account_id'), -Decimal(str(rev['amount'])))]
                      if t['bank_txn_id'] not in used and t['booking_date'] <= rev['booking_date']
                      and (rev_day - date.fromisoformat(t['booking_date'])).days <= max_days]
        if not candidates:
            continue

        def rank(t):
            own = (t.get('text') or '').upper().strip()
            quoted = bool(own) and own in text
            dated = bool(coded) and t['booking_date'].replace('-', '').startswith(coded.group(1)[:8])
            return quoted, dated, t['booking_date'], t['bank_txn_id']

        ranked = [(rank(t), t) for t in candidates]
        ranked = [(key, t) for key, t in ranked if key[0] or key[1]]
        if not ranked:
            continue
        best = max(ranked, key=lambda kt: kt[0])[1]
        used.add(best['bank_txn_id'])
        pairs[best['bank_txn_id']] = rev['bank_txn_id']
    return pairs


# ---------------------------------------------------------------------------
# UNIDENTIFIED evidence (POLICIES §3: unknown payer, wrong beneficiary, cash deposit).
# In TRAIN these remittance texts occur only on UNIDENTIFIED receipts: a bare generic
# word or "REF <n>" with no document number, a cash deposit, or a payer given only as
# initials.  None of them ever carries an invoice.
_GENERIC = r'(?:REF \d{3,7}|ORDER|TRANSFER|INVOICE|ANTICIPO|PAGO PEDIDO|PAGO|ENVIO|FACTURA)'
_TAIL = r'(?:\s*/REF|\s*/EREF/NOTPROVIDED|\s*/EREF/?|\s*/)*\s*$'
_GENERIC_FULL = re.compile(r'^' + _GENERIC + _TAIL)
_GENERIC_END = re.compile(r'(?:^|(?<=[+*:])|(?<=/)(?<!N/)|(?<![\d/] )(?<=\s))' + _GENERIC + _TAIL)
_MX_CONCEPT = re.compile(r'CONCEPTO:? (?:(?:REF \d{3,7}|ORDER|TRANSFER|INVOICE|ANTICIPO|PAGO PEDIDO|ENVIO|FACTURA)'
                         r'(?: [^\d]*)?|PAGO)\s*$')
_CASH = re.compile(r'INGRESO EFECTIVO|VERSEMENT ESPECES|INGRESO CAJERO|VERSAMENTO CONTANTI|DEPOSITO EN EFECTIVO'
                   r'|CASH DEPOSIT|BAREINZAHLUNG')
_LEGAL_FORMS = {'S.A.', 'S.L.', 'S.L.U.', 'S.A.U.', 'B.V.', 'N.V.', 'S.P.A.', 'S.N.C.', 'S.A.S.', 'S.R.L.', 'L.L.C.',
                'P.L.C.', 'S.C.', 'C.V.', 'E.K.', 'S.E.', 'A.G.', 'S.C.A.', 'P.C.', 'L.P.', 'L.L.P.', 'C.B.',
                'S.A.R.L.', 'S.P.', 'S.N.', 'S.R.', 'L.U.', 'R.L.'}
# Words that introduce the payer name in the statement formats (so the initials ARE the name).
_NAME_INTRO = {'', 'CREDIT', 'TRANSFERENCIA', 'TRANSFERENCIAS', 'DA', 'DE', 'IN', 'PRZYCH.', 'RECEBIDO', 'FP',
               'ENVIO', 'ANTICIPO', 'FACTURA', 'PEDIDO', 'PAGO', 'CONCEPTO', 'NAME', 'ORDP', 'ORG', 'BOLETO'}
_INITIALS = re.compile(r'(?:^|(?<=[\s/\-|:]))((?:[A-Z]\.(?:&\.)?){2,4})(?=[\s/|]|$)')
_INITIALS_ONLY = re.compile(r'(?:[A-Z]\.(?:&\.)?){2,4}')


def generic_reference(txn):
    rem = _ascii(txn.get('remittance_info')).strip()
    if rem:
        return bool(_GENERIC_FULL.match(rem))
    text = _ascii(txn.get('text')).strip()
    return bool(_MX_CONCEPT.search(text) or _GENERIC_END.search(text))


def cash_deposit(txn):
    return bool(_CASH.search(_ascii(f"{txn.get('text') or ''} {txn.get('remittance_info') or ''}")))


def initials_payer(txn):
    name = _ascii(txn.get('counterparty_name')).strip()
    if name and _INITIALS_ONLY.fullmatch(name):
        return True
    text = _ascii(f"{txn.get('text') or ''} | {txn.get('ref2') or ''}")
    for match in _INITIALS.finditer(text):
        token = match.group(1)
        if token in _LEGAL_FORMS:
            continue
        before = text[:match.start(1)].rstrip(' -/|:').split()
        previous = before[-1].split('/')[-1] if before else ''
        if previous in _NAME_INTRO or re.fullmatch(r'[\d\-]+', previous):
            return True
    return False


def unidentified_evidence(txn, allow_round=True):
    """Why a receipt looks unidentified, or None.  Receipts only (POLICIES §3)."""
    amount = Decimal(str(txn.get('amount') or 0))
    if amount <= 0:
        return None
    if generic_reference(txn):
        return 'generic_reference'
    if cash_deposit(txn):
        return 'cash_deposit'
    if initials_payer(txn):
        return 'initials_payer'
    # Round receipts (whole hundreds, below the size of treasury movements) are, in TRAIN,
    # nearly always unidentified; used only after every matcher has abstained.
    if allow_round and amount % 100 == 0 and amount < 10000:
        return 'round_amount'
    return None


# A receipt whose remittance slot is empty or a bare generic word, in each statement format.
# Used only after every matcher abstained, together with "no open item for the payer".
_BARE = r'(?:PAYMENT|TRANSFERENCIA|TRANSFER|PAGO)?'
_BARE_REMITTANCE = [re.compile(p) for p in (
    r'SVWZ\+' + _BARE + r'\s*$', r'/REMI/' + _BARE + r'(?:/|$)', r'/MOTIF ' + _BARE + r'\s*/REF\s*$',
    r'CAUSALE ' + _BARE + r'\s*$', r'TYTUL: ' + _BARE + r'\s*$', r'CONCEPTO:? ' + _BARE + r'\s*$',
    r'CCD PAYMENT ID:\d+(?: RMR\*IV\*PAYMENT\*PI\*[\d.]+\\?)?\s*$', r'^DIRECT CREDIT ',
    r'^(?:TT IN|FP) [A-Z .&,]+ PAYMENT$', r'^PIX RECEBIDO .*PAYMENT$')]


def bare_remittance(txn):
    if _ascii(txn.get('remittance_info')).strip() not in ('', 'PAYMENT'):
        return False
    text = _ascii(txn.get('text')).strip()
    return any(p.search(text) for p in _BARE_REMITTANCE)


def payer_open_items(txn, ctx):
    """Open AR items of the identified payer(s) issued by the booking date, or [] (unknown payer)."""
    from .matching import party_candidates
    owner, day = txn.get('entity'), txn['booking_date']
    found = []
    for pid in party_candidates(txn, ctx):
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if (item.get('side') == 'AR' and item['open_amount'] > 0 and item.get('entity') == owner
                    and item.get('issue_date', '') <= day):
                found.append(item)
    return found


def unknown_payer_receipt(txn, ctx):
    """POLICIES §3 UNIDENTIFIED: "a receipt for which no open item exists (unknown payer ...)".

    Only for a receipt every matcher abstained on, with no document reference (bare remittance),
    and either no open item for the payer at all (or no payer found), or a small receipt (< 500):
    in TRAIN, small unreferenced receipts that no matcher could place are mostly unidentified.
    """
    amount = Decimal(str(txn.get('amount') or 0))
    if amount <= 0 or not bare_remittance(txn):
        return False
    return amount < 500 or not payer_open_items(txn, ctx)


# Explicit category wording that, in TRAIN, never comes with an item (sign, pattern, category).
_TEXT_RULES = [
    ('-', re.compile(r'DUPLICAD|PAGO DOBLE|DUPLICATE|\bREFUND'), 'CUSTOMER_REFUND'),   # §3/§4 refund of a duplicate
    ('+', re.compile(r'DUPLICAD|PAGO DOBLE|DUPLICATE'), 'SUPPLIER_REFUND'),
    ('-', re.compile(r'\bDGFIP\b'), 'TAX_PAYMENT'),
    ('+', re.compile(r'\bAEAT\b'), 'TAX_REFUND'),
    ('', re.compile(r'PRESTAMO INTRAGRUP|DEVOLUCION PRESTAM'), 'INTERCOMPANY'),
]


def text_rule_category(txn):
    sign, text = _sign(txn), _full_text(txn)
    for rule_sign, pattern, category in _TEXT_RULES:
        if rule_sign in ('', sign) and pattern.search(text):
            return category
    return None


def fallback_category(txn):
    """Category for a line every handler abstained on (no exception implied).

    Receipts quoting "REEMBOLSO" that settle no supplier credit note are refunds of a
    payment we made twice (§3 DUPLICATE_PAYMENT, §4 SUPPLIER_REFUND).  Outgoing lines left
    over after the explicit fee/tax/payroll wording are, in TRAIN, customer refunds.
    """
    if _sign(txn) == '+':
        return 'SUPPLIER_REFUND' if 'REEMBOLSO' in _full_text(txn) else 'UNIDENTIFIED'
    return 'CUSTOMER_REFUND'


class NonItemClassifier:
    """Front handler: only proposals that TRAIN shows never settle an item."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._pairs = None

    def pairs(self):
        if self._pairs is None:
            txns = getattr(self.ctx, 'phase_txns', None)
            if txns is None:
                phase_dir = getattr(self.ctx, 'phase_dir', None)
                txns = list(read_csv(phase_dir / 'bank_transactions.csv.gz')) if phase_dir else []
            self._pairs = reversal_originals(txns)
        return self._pairs

    def reconcile(self, txn):
        categories = getattr(self.ctx, 'categories', None) or ()
        if 'BANK_REVERSAL' in categories and (is_reversal_line(txn) or txn['bank_txn_id'] in self.pairs()):
            return [_row(txn, 'BANK_REVERSAL')]
        category = CATEGORY_LOOKUP.get(text_key(txn))
        if category and category in categories:
            return [_row(txn, category)]
        category = text_rule_category(txn)
        if category and category in categories:
            return [_row(txn, category)]
        if 'UNIDENTIFIED' in categories and unidentified_evidence(txn, allow_round=False):
            return [_row(txn, 'UNIDENTIFIED', 'UNIDENTIFIED')]
        return None
