"""Evidence-based ordinary reconciliation. No labels are accessed at inference."""
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, ROUND_FLOOR
import re
import unicodedata
from .subsets import unique_subset, subset_solutions, positive_subsets, SubsetIndex

D = Decimal
CENT = D('0.01')

def norm(value):
    value = unicodedata.normalize('NFKD', str(value or '').upper())
    return re.sub('[^A-Z0-9]', '', value)

def words(value):
    value = unicodedata.normalize('NFKD', str(value or '').upper())
    return ' '.join(re.findall('[A-Z0-9]+', ''.join(c for c in value if not unicodedata.combining(c))))

def dec(value):
    return D(str(value or '0'))

def row(txn, item=None, amount=None, exception='', category=''):
    return {'bank_txn_id': txn['bank_txn_id'], 'item_id': item or '',
            'amount': format(dec(amount).quantize(CENT, rounding=ROUND_HALF_UP), '.2f') if item else '',
            'category': category, 'exception': exception}

def text_of(txn):
    return ' | '.join(str(txn.get(k) or '') for k in ('text','ref1','ref2','remittance_info','counterparty_name','counterparty_account','end_to_end_id'))

def _index(ctx):
    cached = getattr(ctx, '_matching_index', None)
    if cached is not None:
        return cached
    prefixes, ibans, aliases, children = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    previous_accounts = defaultdict(set)
    parties = dict(getattr(ctx,'customers',{})); parties.update(getattr(ctx,'suppliers',{}))
    for pid, p in parties.items():
        for key in ('name','trade_name'):
            n = norm(p.get(key))
            if len(n) >= 6:
                aliases[pid].add(n); prefixes[n[:10]].add(pid)
        for key in ('iban','account_number','sepa_mandate_id'):
            n = norm(p.get(key))
            if n: ibans[n].add(pid)
        if p.get('parent_customer_id'): children[p['parent_customer_id']].add(pid)
    # FINAL: previous master names and accounts of renamed / re-banked parties stay valid aliases.
    for name, key in (('customers', 'customer_id'), ('suppliers', 'supplier_id')):
        for p in getattr(ctx, 'previous_masters', {}).get(name, ()):
            pid = p.get(key)
            if not pid: continue
            for field in ('name', 'trade_name'):
                n = norm(p.get(field))
                if len(n) >= 6 and n not in aliases[pid]:
                    aliases[pid].add(n); prefixes[n[:10]].add(pid)
            for field in ('iban', 'account_number', 'sepa_mandate_id'):
                n = norm(p.get(field))
                if n:
                    ibans[n].add(pid); previous_accounts[pid].add(n)
    # Historical printed names are necessary when current masters have been renamed.
    for item in ctx.items.values():
        pid = item.get('party_id')
        if not pid: continue
        for key in ('customer_name','supplier_name','party_name'):
            n = norm(item.get(key))
            if len(n) >= 6 and n not in aliases[pid]:
                aliases[pid].add(n); prefixes[n[:10]].add(pid)
    ctx._previous_accounts = previous_accounts
    # Names of 6-9 normalised characters ('Emma Ross') never fill a 10-character
    # prefix window; they are looked up as whole-word sequences instead.
    shorts = defaultdict(set)
    for pid, names in aliases.items():
        for n in names:
            if len(n) < 10:
                shorts[n].add(pid)
    cached = (prefixes,ibans,aliases,children,parties)
    ctx._matching_index = cached
    ctx._matching_shorts = shorts
    return cached

def _party_entity_ok(party, owner):
    entity = party.get('entity')
    return not entity or not owner or entity == owner or (owner == 'ES00' and entity in ('ES01', 'ES02', 'PT01'))

def _short_name_candidates(txn, ctx, parties):
    """Payers whose short name (6-9 characters) is a whole-word sequence of the bank text.

    POLICIES 5.5: the party name is often the only evidence. Several same-named
    payers in the paying entity are ambiguous and are not proposed.
    """
    shorts = getattr(ctx, '_matching_shorts', None) or {}
    if not shorts:
        return set()
    tokens = words(text_of(txn)).split()
    owner = txn.get('entity')
    found = set()
    for size in (1, 2, 3, 4):
        for i in range(len(tokens) - size + 1):
            key = ''.join(tokens[i:i + size])
            if 6 <= len(key) <= 9 and key in shorts:
                local = {pid for pid in shorts[key] if _party_entity_ok(parties.get(pid, {}), owner)}
                if len(local) == 1:
                    found |= local
    return found

def party_candidates(txn,ctx,short_names=True):
    prefixes,ibans,aliases,children,parties = _index(ctx)
    raw = text_of(txn); compact=norm(raw)
    candidates=set()
    for i in range(max(0,len(compact)-9)):
        candidates.update(prefixes.get(compact[i:i+10],()))
    if short_names:
        candidates.update(_short_name_candidates(txn, ctx, parties))
    for token in re.findall('[A-Za-z0-9-]{8,}',raw):
        candidates.update(ibans.get(norm(token),()))
    acct=norm(txn.get('counterparty_account'))
    candidates.update(ibans.get(acct,()))
    ranked=[]
    explicit=norm(txn.get('counterparty_name'))
    for pid in candidates:
        p=parties.get(pid,{})
        account_hit=any(norm(p.get(k)) and norm(p.get(k)) in compact for k in ('iban','account_number','sepa_mandate_id'))
        account_hit=account_hit or any(a in compact for a in getattr(ctx,'_previous_accounts',{}).get(pid,()))
        score=100 if account_hit else 0
        for alias in aliases[pid]:
            if alias==explicit:score=max(score,90)
            elif alias in compact:score=max(score,80+min(9,len(alias)//5))
            elif len(alias)>=12:
                # Require a reasonably long prefix, avoiding generic short trade names.
                for length in (24,20,16,12):
                    if len(alias)>=length and alias[:length] in compact:
                        score=max(score,30+length);break
        if score:
            ranked.append((score,pid))
    if not ranked:return []
    # Entity first (POLICIES 5.2): when a candidate belongs to the bank account's entity (or,
    # for ES00, to the payment-factory entities ES01/ES02/PT01), rank only those, so a
    # same-named party of another country cannot crowd the local one out.
    # Idea credit: marinamoncayola (solution/marinamoncayola d4a3744).
    owner=txn.get('entity')
    if owner:
        allowed={owner}|({'ES01','ES02','PT01'} if owner=='ES00' else set())
        local=[(score,pid) for score,pid in ranked if parties.get(pid,{}).get('entity') in allowed]
        if local: ranked=local
    ranked.sort(reverse=True)
    threshold=max(42,ranked[0][0]-8)
    selected=[pid for score,pid in ranked if score>=threshold][:12]
    for pid in list(selected): selected.extend(children.get(pid,()))
    return list(dict.fromkeys(selected))

def index_same_day(ctx, txns):
    """Compact texts of each entity's bank lines per booking day (read-only lookup)."""
    groups = defaultdict(list)
    for txn in txns:
        groups[(txn.get('entity'), txn.get('booking_date'))].append(txn)
    index = {}
    for key, group in groups.items():
        starts, ids, parts, offset = [], [], [], 0
        for txn in group:
            compact = norm(text_of(txn))
            starts.append(offset); ids.append(txn['bank_txn_id']); parts.append(compact)
            offset += len(compact) + 1
        index[key] = ('|'.join(parts), starts, ids)
    ctx.same_day_texts = index


def _quoted_by_sibling(ctx, txn, items):
    """Items whose document number another bank line of the same entity and day quotes.

    POLICIES 5.6: FIFO applies only *without a usable reference*. When a sibling line
    of the same day quotes an invoice, that invoice is the sibling's, not the oldest
    open candidate of an unreferenced same-amount receipt.
    """
    index = getattr(ctx, 'same_day_texts', None)
    entry = index.get((txn.get('entity'), txn.get('booking_date'))) if index else None
    if not entry:
        return set()
    blob, starts, ids = entry
    quoted = set()
    for item in items:
        number = norm(item.get('document_number'))
        if len(number) < 6:
            continue
        pos = blob.find(number)
        while pos >= 0:
            if ids[bisect_right(starts, pos) - 1] != txn['bank_txn_id']:
                quoted.add(item['item_id'])
                break
            pos = blob.find(number, pos + 1)
    return quoted

def _remember_last(fn):
    """One-entry memo: matching, credit groups and the fallback ask about the same line."""
    last = [None, None, None]
    def wrapper(txn, ctx):
        key = (txn.get('bank_txn_id'), text_of(txn), txn.get('counterparty_account'))
        if last[0] is not ctx or last[1] != key:
            last[:] = [ctx, key, fn(txn, ctx)]
        return list(last[2])
    return wrapper

party_candidates = _remember_last(party_candidates)

def _eligible(item, txn, reverse=False):
    if item.get('issue_date','') > txn.get('booking_date','9999'):return False
    entity=item.get('entity');owner=txn.get('entity')
    if entity and owner and entity != owner:
        if not (owner=='ES00' and entity in ('ES01','ES02','PT01') and item.get('side')=='AP'):
            return False
    sign=dec(txn['amount'])>0
    side=item.get('side','AR');credit=item.get('kind')=='credit'
    normal_positive=(side=='AR') != credit
    return normal_positive != sign if reverse else normal_positive == sign

def _cash(item,txn):
    if item.get('currency') == txn.get('currency'):
        return abs(dec(txn['amount'])),False
    if txn.get('orig_amount') and txn.get('orig_currency')==item.get('currency'):
        return abs(dec(txn['orig_amount'])),True
    return None,False

def _exception(item, cash, txn, ctx, fx=False, grace=0):
    opened=dec(item.get('open_amount',item.get('total_amount')))
    if opened <= 0:return 'DUPLICATE_PAYMENT',cash
    if abs(cash-opened)<=CENT:return ('FX_DIFFERENCE' if fx and item.get('kind')!='credit' else ''),opened
    if cash > opened:
        if fx:
            return 'FX_DIFFERENCE',opened
        # POLICIES §3 OVERPAYMENT: the customer paid more than the open amount. In TRAIN
        # the excess is always a round amount or exactly 1 %/10 % of the invoice; any
        # other excess is another document of the same payment (an unquoted invoice,
        # a discounted or partial item), so the referenced invoice is cleared without
        # claiming an overpayment.
        excess=cash-opened
        if excess in OVERPAY_ROUND or any(abs(excess-(opened*r).quantize(CENT,rounding=ROUND_HALF_UP))<=CENT
                                          for r in (D('0.01'),D('0.10'))):
            return 'OVERPAYMENT',opened
        return '',opened
    # Discounts must use booking date and the total, not remaining balance.
    terms=getattr(ctx,'payment_terms',{})
    term=terms.get(item.get('payment_terms'),{}) if isinstance(terms,dict) else {}
    pct=dec(term.get('early_payment_discount_pct'))
    total=dec(item.get('total_amount'))
    if pct and opened==total:
        expected=(total*(D(1)-pct/D(100))).quantize(CENT,rounding=ROUND_HALF_UP)
        try:
            deadline=date.fromisoformat(item['issue_date'])+timedelta(days=int(term['discount_days'])+3+grace)
            if abs(cash-expected)<=CENT and date.fromisoformat(txn['booking_date'])<=deadline:
                return ('FX_DIFFERENCE' if fx else 'SHORT_DISCOUNT'),opened
        except (ValueError,KeyError,TypeError):pass
    customer=getattr(ctx,'customers',{}).get(item.get('party_id'),{})
    from .adjusted import retention_pct
    # A PDF guarantee retention explains the shortfall (first payment PARTIAL), not tax withheld.
    if customer.get('country') in ('MX','BR','MA') and opened==total and retention_pct(item, ctx) is None:
        for rate in (D('0.0125'),D('0.015'),D('0.10')):
            if abs(cash-(total*(1-rate)).quantize(CENT,rounding=ROUND_HALF_UP))<=CENT:
                return ('FX_DIFFERENCE' if fx else 'SHORT_WITHHOLDING'),opened
    text=words(text_of(txn))
    if any(x in text for x in ('DISPUT','RECLAM','DAMAGED','PENALTY','DEDUCCION')):
        return 'SHORT_DISPUTE',cash
    # Intermediary charges on a documented cross-border receipt (POLICIES §3).
    # A small difference alone never establishes this exception: the bank line
    # must carry a cross-border marker and the payer must be foreign.
    sepa={'AD','AL','AT','BE','BG','CH','CY','CZ','DE','DK','EE','ES','FI','FR','GB','GI','GR','HR','HU','IE','IS','IT','LI','LT','LU','LV','MC','MD','ME','MK','MT','NL','NO','PL','PT','RO','RS','SE','SI','SK','SM','VA'}
    country=customer.get('country','').upper()
    transfer=cross_border_transfer(txn)
    # TRAIN history: foreign SEPA-member payers (CH, GB, PL, MT...) that pay by an
    # international transfer also lose the intermediary fee, so a payer whose country
    # differs from the receiving entity's country qualifies like a non-SEPA payer.
    entity_country=str(txn.get('entity') or item.get('entity') or '')[:2].upper()
    foreign=bool(country) and (country not in sepa or country!=entity_country)
    if (not fx and item.get('side')=='AR' and item.get('kind')!='credit'
            and foreign and transfer):
        short=opened-cash
        currency=item.get('currency')
        rate=D(1) if currency=='EUR' else None
        if rate is None:
            rates=getattr(ctx,'fx_rates',{})
            dates=[day for day,values in rates.items() if day<=txn.get('booking_date','') and currency in values]
            if dates:rate=dec(rates[max(dates)][currency])
        lattice=sbc_fee_lattice(currency)
        if lattice:
            # TRAIN (every year, 1,090 of 1,090 golden cases): the intermediary fee is
            # one of a fixed set, so any other shortfall is a discount or a partial.
            if short in lattice:
                return 'SHORT_BANK_CHARGES',opened
        elif rate and rate>0 and 0<short and short/rate<=D(60):
            # A fixed fee can exceed 2 % of a small invoice (TRAIN: up to ~5 %), but a
            # shortfall that is an exact percentage share of the total is a late discount,
            # a retention or a partial payment, never a bank fee. The original non-SEPA
            # rule (<= 2 %) is kept unchanged; the share guard applies to the extension.
            if (country not in sepa and short<=opened*D('0.02')) or \
                    (short<=total*D('0.05') and not _percent_share(short,total)):
                return 'SHORT_BANK_CHARGES',opened
    return 'PARTIAL',cash


_XB_TEXT = re.compile(r'\b(?:SWIFT|TRANSFER EN DIV|TRANSFERENCIA INTERNACIONAL|INTERNATIONAL TRANSFER|WIRE TRANSFER|GASTOS (?:SHA|BEN)|CHGS)\b')


def cross_border_transfer(txn):
    """The bank line documents a cross-border credit transfer (text marker or ISO code XBCT)."""
    bank_text=words(' | '.join(str(txn.get(k) or '') for k in ('text','ref1','ref2','remittance_info')))
    # ISO 20022 bank transaction code PMNT/RCDT/XBCT = cross-border credit transfer.
    return bool(_XB_TEXT.search(bank_text)) or 'XBCT' in str(txn.get('bank_code') or '').upper()


# POLICIES §3 SHORT_BANK_CHARGES: intermediary fees. TRAIN history (2021-2025, no
# exception) shows a fixed EUR fee schedule, charged in the receipt currency at a fixed
# conversion and truncated to the cent. Currencies without history keep the bounds rule.
_SBC_FEES_EUR = (10, 15, 20, 25, 35, 45)
_SBC_CONVERSION = {'EUR': D('1'), 'USD': D('1.185'), 'CAD': D('1.48'), 'AUD': D('1.58')}
_SBC_LATTICE = {currency: frozenset((D(fee) * factor).quantize(CENT, rounding=ROUND_DOWN) for fee in _SBC_FEES_EUR)
                for currency, factor in _SBC_CONVERSION.items()}


def sbc_fee_lattice(currency):
    return _SBC_LATTICE.get(str(currency or '').upper())


_SHARE_PCTS = tuple(D(x) for x in ('1', '1.25', '1.5', '2', '2.5', '3', '4', '5', '10', '15', '20'))


def _percent_share(short, total):
    """True when ``short`` is an exact common percentage of ``total`` (either rounding)."""
    for pct in _SHARE_PCTS:
        x = total * pct / 100
        if short in (x.quantize(CENT, rounding=ROUND_HALF_UP), x.quantize(CENT, rounding=ROUND_DOWN)):
            return True
    return False

def classify(txn,ctx):
    """Only explicit category evidence; return None for unknown lines."""
    t=words(' | '.join(str(txn.get(k) or '') for k in ('text','ref1','ref2','remittance_info')));positive=dec(txn['amount'])>0
    rules=[
      ('BANK_REVERSAL',r'ANULACION OPERACION INDEBIDA|BANK ERROR|ERROR REVERSAL|ERRONEOUS'),
      ('CASH_POOL',r'CASH CONCENTRATION|CASH POOL|CASHPOOL|ZBA SWEEP|ZERO BALANC(?:E|ING)?|NIVELACION SALDO|TRASPASO PROPIO|TRASPASO ENTRE CUENTAS ES00'),
      ('INTERCOMPANY',r'INTRAGRUPO|INTERCOMPANY|FINANCIACION IC|PRESTAMO IC|TRANSF IC'),
      ('INTERNAL_TRANSFER',r'TRASPASO (?:A|DE) CTA|OWN ACCOUNT|INTERNAL TRANSFER|ENTRE CUENTAS PROPIAS'),
      ('CUSTOMER_ADVANCE',r'PROFORMA|ANTICIPO PF'),
      ('CARD_EXPENSES',r'ADEUDO MENSUAL DE TARJETA|CORPORATE CARD|CARD STATEMENT'),
      ('PAYROLL',r'NOMINAS?|PAYROLL|SALARIES|GEHALT|LOHN|SALAIRES'),
      ('SOCIAL_SECURITY',r'SEGURIDAD SOCIAL|SEG SOCIAL|SOCIAL CHARGES|SOCIAL SECURITY|URSSAF|SOZIAL'),
      ('LEASING',r'LEASING|RENTING|ARRENDAMIENTO|CUOTA LSG'),
      ('INSURANCE',r'PRIMA SEGURO|INSURANCE PREMIUM|POLIZA SEGURO|ASSURANCE'),
      ('LOAN_REPAYMENT',r'AMORTIZACION|LOAN REPAYMENT|CUOTA PRESTAMO|PRES [A-Z]{2}[0-9]+'),
      ('LOAN_DRAWDOWN',r'DISPOSICION PRESTAMO|LOAN DRAWDOWN|ABONO PRESTAMO'),
      ('FX_DEAL',r'FX DEAL|FOREX DEAL|COMPRA DIVISAS?|VENTA DIVISAS?|COMPRAVENTA DIVISAS?|OPERACION CAMBIO'),
    ]
    category=None
    for cat,pattern in rules:
        if re.search(r'(?<![A-Z0-9])(?:'+pattern+r')(?![A-Z0-9])',t):category=cat;break
    if category is None and re.search(r'\b(?:INTERESTS?|INTERESES|ZINS(?:EN)?)\b',t):category='INTEREST_INCOME' if positive else 'INTEREST_EXPENSE'
    if category is None and re.search(r'\b(?:IMPUESTOS?|TAX PAYMENT|TAX REFUND|VAT RETURN|DEVOLUCION HACIENDA)\b',t):category='TAX_REFUND' if positive else 'TAX_PAYMENT'
    if category is None and re.search(r'\b(?:COMISION(?:ES)?|COMMISSION(?:S)?|BANK FEES?|SERVICE CHARGES?|GEBUHR(?:EN)?|FRAIS|MAINTENANCE FEE|LIQUIDACION CTA CTO|COM TRANSF|SERV EM TRANSF)\b',t):category='BANK_FEES'
    if category is None and re.search(r'\b(?:DEVOLUCION PAGO DUPLICADO|ABONO PAGO DOBLE|DUPLICATE REFUND|REFUND DUPLICATE PAYMENT)\b',t):category='SUPPLIER_REFUND' if positive else 'CUSTOMER_REFUND'
    return [row(txn,category=category)] if category else None

def _net_referenced(txn, invoices, credits, refs, ctx):
    """An explicit invoice group, with uniquely evidenced deductions only.

    Unmentioned credits must name a referenced invoice via related_document.
    Discounts must use invoice terms; no arbitrary partial split is inferred.
    """
    if not invoices or len({i['party_id'] for i in invoices}) != 1:
        return None
    cash = abs(dec(txn['amount']))
    gross = sum((dec(i['open_amount']) for i in invoices), D(0))
    numbers = {norm(i['document_number']) for i in invoices}
    explicit_credits = [c for c in credits if c['item_id'] in refs]
    fixed = sum((dec(c['open_amount']) for c in explicit_credits), D(0))
    deficit = gross - fixed - cash
    if deficit < 0:
        return None
    deductions = []
    for credit in credits:
        if credit in explicit_credits:
            continue
        if norm(credit.get('related_document')) in numbers:
            deductions.append((dec(credit['open_amount']), 'credit', credit))
    for item in invoices:
        total = dec(item.get('total_amount'))
        term = getattr(ctx, 'payment_terms', {}).get(item.get('payment_terms'), {})
        pct = dec(term.get('early_payment_discount_pct'))
        if total == dec(item['open_amount']) and 0 < pct < 100:
            discounted = (total * (1 - pct / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
            deductions.append((total-discounted, 'discount', item))
    if deficit == 0:
        selected = ()
    else:
        selected = unique_subset([int(d[0]*100) for d in deductions], int(deficit*100))
        if selected is None:
            return None
    rows = {i['item_id']: row(txn, i['item_id'], i['open_amount']) for i in invoices + explicit_credits}
    settled = invoices + explicit_credits + [deductions[k][2] for k in selected if deductions[k][1] == 'credit']
    from .adjusted import newest_grace
    for index in selected:
        amount, kind, item = deductions[index]
        if kind == 'credit':
            rows[item['item_id']] = row(txn, item['item_id'], item['open_amount'])
        else:
            exc, applied = _exception(item, dec(item['open_amount'])-amount, txn, ctx, grace=newest_grace(item, settled))
            rows[item['item_id']] = row(txn, item['item_id'], applied, exc)
    return [rows[key] for key in sorted(rows)]

_SERVICE_MONTHS = {
    1: 'JANUARY ENERO JANVIER JANUAR GENNAIO JANEIRO JAN ENE JANV GEN',
    2: 'FEBRUARY FEBRERO FEVRIER FEBRUAR FEBBRAIO FEVEREIRO FEB FEVR FEV',
    3: 'MARCH MARZO MARS MARZ MAERZ MARCO MAR MRZ',
    4: 'APRIL ABRIL AVRIL APRILE APR ABR AVR',
    5: 'MAY MAYO MAI MAGGIO MAIO MAG',
    6: 'JUNE JUNIO JUIN JUNI GIUGNO JUNHO JUN GIU',
    7: 'JULY JULIO JUILLET JULI LUGLIO JULHO JUL JUIL LUG',
    8: 'AUGUST AGOSTO AOUT AUG AGO',
    9: 'SEPTEMBER SEPTIEMBRE SETIEMBRE SEPTEMBRE SETTEMBRE SETEMBRO SEP SEPT SET',
    10: 'OCTOBER OCTUBRE OCTOBRE OKTOBER OTTOBRE OUTUBRO OCT OKT OTT OUT',
    11: 'NOVEMBER NOVIEMBRE NOVEMBRE NOVEMBRO NOV',
    12: 'DECEMBER DICIEMBRE DECEMBRE DEZEMBER DICEMBRE DEZEMBRO DEC DIC DEZ',
}
_SERVICE_MONTH_OF = {w: m for m, ws in _SERVICE_MONTHS.items() for w in ws.split()}
_SERVICE_FULL = [(w, m) for m, ws in _SERVICE_MONTHS.items() for w in ws.split() if len(w) >= 6]
_SERVICE_RE = re.compile(r'\b(?:SERVICE|SERVICES|SERVICIOS?|MANT|MANTENIMIENTO|MAINTENANCE|MAINT|MES|MONTH|CUOTA'
                         r'|WARTUNG|ENTRETIEN|MANUTENZIONE|MENSUALIDAD|ALQUILER|MIETE|LOYER)'
                         r'(?:\s+(?:DE|DEL|OF|MANTENIMIENTO))?\s+([A-Z]{3,10})(?:\s+(20\d{2}|\d{2})\b)?')


def _service_month_word(word):
    """Month number of a month word: full name, abbreviation, or a bank-truncated full name."""
    if word in _SERVICE_MONTH_OF:
        return _SERVICE_MONTH_OF[word]
    if len(word) >= 5:   # 'CUOTA MANTENIMIENTO DICIEMB' (text cut at 38 characters)
        hits = {m for full, m in _SERVICE_FULL if full.startswith(word)}
        if len(hits) == 1:
            return hits.pop()
    return None


def _stated_service_month(raw, booking_date):
    """Return the service-month upper bound and whether its year is explicit.

    Policy 5.6: monthly fees are invoiced at the beginning of the following
    month. A yearless month therefore must precede the booking month.
    Every keyword occurrence is tried ('SPA SERVICE ACME ... SERVICE JANUARY 2025').
    """
    for match in _SERVICE_RE.finditer(words(raw)):
        month = _service_month_word(match[1])
        if month is None:
            continue
        try:
            booked = date.fromisoformat(booking_date)
            year = (int(match[2]) if len(match[2]) == 4 else 2000 + int(match[2])) if match[2] else booked.year - (month >= booked.month)
            return f'{year:04d}-{month:02d}', bool(match[2])
        except (TypeError, ValueError):
            return None
    return None

def _invoice_service_month(item):
    try:
        issued = date.fromisoformat(item['issue_date'])
        prior = date(issued.year, issued.month, 1) - timedelta(days=1)
        return prior.strftime('%Y-%m')
    except (KeyError, ValueError, TypeError):
        return None

def reconcile(txn, ctx):
    return _reconcile(txn, ctx)


def quoted_groups(txn, ctx):
    """Complete readings of a receipt that quotes open invoices of one payer: one quoted
    invoice at a TRAIN partial share, or the quoted invoices plus a unique rest. Runs after
    the credit-note families and the noisy-reference fallback declined."""
    quoted = _quoted_refs(txn, ctx)
    if not quoted:
        return None
    return _quoted_partial(txn, ctx, quoted) or _quoted_rest_partial(txn, ctx, quoted)


def quoted_only(txn, ctx):
    """Last resort after the fragment anchors: the unambiguously quoted invoices alone."""
    return _quoted_only(txn, ctx)


_RETURN_RE = (r'DEVOLUCION (?:RECIBO|TRANSFERENCIA)|DEVOL RECIB|RUECKLASTSCHRIFT|RETURNED ITEM|CHEQUE IMPAYE'
              r'|CHARGEBACK|CONTRACARGO|DIRECT DEBIT RETURN')


def _quoted_refs(txn, ctx):
    """Unambiguously quoted open AR invoices of one payer in a receipt, or None.

    Full document numbers, or numeric suffixes that name exactly one open invoice
    (POLICIES 5.5: our number without prefix). Payer evidence narrows the candidates.
    """
    if dec(txn['amount']) <= 0 or classify(txn, ctx):
        return None
    raw = text_of(txn)
    if re.search(_RETURN_RE, words(raw)):
        return None
    compact = norm(raw)
    parties = party_candidates(txn, ctx)
    numeric_tokens = set(re.findall(r'\b\d{5,}\b', raw))
    candidates = set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}', raw):
        candidates.update(ctx.by_number.get(norm(token), ()))
    for pid in parties:
        candidates.update(ctx.by_party.get(pid, ()))
    def usable(item):
        return (item.get('kind') != 'credit' and item.get('side') == 'AR' and dec(item.get('open_amount')) > 0
                and item.get('currency') == txn.get('currency') and item.get('entity') == txn.get('entity')
                and _eligible(item, txn))
    pool = [ctx.items[i] for i in candidates if i in ctx.items and usable(ctx.items[i])]
    if parties:
        pool = [i for i in pool if i.get('party_id') in parties] or pool
    quoted, by_token = {}, defaultdict(set)
    for item in pool:
        n = norm(item.get('document_number'))
        if len(n) >= 7 and n in compact:
            quoted[item['item_id']] = item
        else:
            for v in numeric_tokens:
                if n.endswith(v):
                    by_token[v].add(item['item_id'])
    for v, ids in by_token.items():
        if len(ids) == 1:   # a suffix shared by several open invoices identifies none of them
            iid = next(iter(ids))
            quoted[iid] = ctx.items[iid]
    pdf_facts = getattr(ctx, 'pdf_facts', None)
    if pdf_facts is not None and any(pdf_facts.resolve(i) != i for i in quoted):
        return None
    if not quoted or len({i['party_id'] for i in quoted.values()}) != 1:
        return None
    return quoted


def _quoted_only(txn, ctx, quoted=None):
    """A receipt that quotes open invoices of one payer but was not fully explained.

    The cash exceeds the quoted invoices and no unique rule identified the rest (the list
    was cut by the bank, POLICIES 5.5). Each unambiguously quoted invoice is a normal payment
    of its open amount (POLICIES 2); the unexplained remainder is not guessed.
    """
    quoted = _quoted_refs(txn, ctx) if quoted is None else quoted
    if not quoted:
        return None
    if sum((dec(i['open_amount']) for i in quoted.values()), D(0)) >= dec(txn['amount']) - CENT:
        return None
    return [row(txn, iid, quoted[iid]['open_amount']) for iid in sorted(quoted)]


# TRAIN: a multi-invoice receipt that leaves one invoice partly open pays 40, 50 or 60 %
# of that invoice's total (ar_n_to_m); other partial ratios belong to discounts/disputes.
_PARTIAL_SHARES = (D('0.4'), D('0.5'), D('0.6'))


def _quoted_partial(txn, ctx, quoted=None):
    """All paid invoices are quoted, the cash is short, and exactly one invoice takes the cut.

    The others are paid in full; the one fresh invoice whose applied amount is a TRAIN
    partial share of its total is PARTIAL with the applied amount (POLICIES 2 and 3).
    """
    quoted = _quoted_refs(txn, ctx) if quoted is None else quoted
    if not quoted or len(quoted) < 2:
        return None
    cash = dec(txn['amount'])
    gross = sum((dec(i['open_amount']) for i in quoted.values()), D(0))
    if gross <= cash + CENT:
        return None
    found = []
    for iid, item in quoted.items():
        opened, total = dec(item['open_amount']), dec(item.get('total_amount'))
        if opened != total:
            continue
        applied = cash - (gross - opened)
        if not D(0) < applied < opened:
            continue
        shares = {(total * f).quantize(CENT, rounding=r) for f in _PARTIAL_SHARES for r in (ROUND_HALF_UP, ROUND_FLOOR)}
        if any(abs(applied - v) <= CENT for v in shares):
            found.append((iid, applied))
    if len(found) != 1:
        return None
    partial, applied = found[0]
    return [row(txn, iid, applied if iid == partial else quoted[iid]['open_amount'], 'PARTIAL' if iid == partial else '')
            for iid in sorted(quoted)]


def _quoted_rest_partial(txn, ctx, quoted=None):
    """The quoted invoices leave a gap that one unique combination of the payer's other open
    invoices fills: all in full, or with exactly one fresh invoice paid at a TRAIN partial
    share (40/50/60 % of its total, PARTIAL with the applied amount; POLICIES 2, 3, 5.5).

    The plain all-in-full reading is tried first; a partial reading is considered only when
    no plain combination exists, and must be unique across every quoted or unquoted choice.
    """
    quoted = _quoted_refs(txn, ctx) if quoted is None else quoted
    if not quoted:
        return None
    cash = dec(txn['amount'])
    gap = cash - sum((dec(i['open_amount']) for i in quoted.values()), D(0))
    if gap <= CENT:
        return None
    party = next(iter(quoted.values()))['party_id']
    fresh_quoted = [i for i in quoted.values() if dec(i['open_amount']) == dec(i.get('total_amount'))]
    bound = max([gap / D('0.4')] + [gap + dec(i['open_amount']) for i in fresh_quoted])
    candidates = [ctx.items[i] for i in ctx.by_party.get(party, ()) if i not in quoted]
    candidates = [i for i in candidates if i.get('kind') != 'credit' and i.get('side') == 'AR'
                  and i.get('currency') == txn.get('currency') and i.get('entity') == txn.get('entity')
                  and _eligible(i, txn) and dec(i['open_amount']) > 0]
    try:
        limit = (date.fromisoformat(txn['booking_date']) - timedelta(days=_REST_WINDOW_DAYS)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    def bounded(ceiling):
        pool = [i for i in candidates if dec(i['open_amount']) <= ceiling]
        if len(pool) > _REST_MAX_ITEMS:
            pool = [i for i in pool if i.get('issue_date', '') >= limit]
        return sorted(pool, key=lambda i: i['item_id']) if len(pool) <= _REST_MAX_ITEMS else None
    cents = lambda amount: int((amount * 100).to_integral_value())
    def emit(pool, mask, partial=None, applied=None):
        chosen = list(quoted.values()) + [pool[k] for k in range(len(pool)) if mask >> k & 1]
        if partial is not None and partial['item_id'] not in {i['item_id'] for i in chosen}:
            chosen.append(partial)
        return [row(txn, i['item_id'], applied if partial is not None and i['item_id'] == partial['item_id'] else i['open_amount'],
                    'PARTIAL' if partial is not None and i['item_id'] == partial['item_id'] else '')
                for i in sorted(chosen, key=lambda i: i['item_id'])]
    # The all-in-full reading over exactly the pool of _unique_rest decides first.
    plain_pool = bounded(gap)
    if plain_pool is None:
        return None
    plain = SubsetIndex([cents(dec(i['open_amount'])) for i in plain_pool], cents(gap), _REST_MAX_ITEMS).masks(cents(gap), limit=2)
    if len(plain) == 1:
        return emit(plain_pool, plain[0])
    if plain:
        return None
    pool = bounded(bound)
    if pool is None:
        return None
    index = SubsetIndex([cents(dec(i['open_amount'])) for i in pool], cents(bound), _REST_MAX_ITEMS)
    if not index.ok:
        return None
    def shares(item):
        total = dec(item.get('total_amount'))
        return {(total * f).quantize(CENT, rounding=r) for f in _PARTIAL_SHARES for r in (ROUND_HALF_UP, ROUND_FLOOR)}
    readings = {}
    for item in fresh_quoted:
        for value in shares(item):
            if D(0) < value < dec(item['open_amount']):
                for mask in index.masks(cents(gap + dec(item['open_amount']) - value), limit=2):
                    readings[(item['item_id'], mask)] = (item, value)
    for k, item in enumerate(pool):
        if dec(item['open_amount']) != dec(item.get('total_amount')):
            continue
        for value in shares(item):
            if D(0) < value < min(gap, dec(item['open_amount'])) + CENT:
                for mask in index.masks(cents(gap - value), exclude=1 << k, limit=2):
                    readings[(item['item_id'], mask)] = (item, value)
        if len(readings) > 1:
            return None
    if len(readings) != 1:
        return None
    (iid, mask), (item, value) = next(iter(readings.items()))
    return emit(pool, mask, item, value)


def _reconcile(txn,ctx):
    if classify(txn,ctx):
        return None  # Allow runner to label this explicit non-item operation.
    raw=text_of(txn);compact=norm(raw)
    returning=bool(re.search(r'DEVOLUCION (?:RECIBO|TRANSFERENCIA)|DEVOL RECIB|RUECKLASTSCHRIFT|RETURNED ITEM|CHEQUE IMPAYE|CHARGEBACK|CONTRACARGO|DIRECT DEBIT RETURN',words(raw)))
    parties=party_candidates(txn,ctx)
    ids=set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}',raw):
        key=norm(token)
        ids.update(ctx.by_number.get(key,()))
    scoped=[]
    for pid in parties:
        scoped.extend(ctx.by_party.get(pid,()))
    # Search document numbers and truncated suffixes only inside identified parties.
    refs=set(ids)
    numeric_tokens=set(re.findall(r'\b\d{5,}\b',raw))
    # Bank texts cut references at 16/35/38 characters: 'INVOICE INV24-0151' for INV24-0151xx.
    truncated_tokens={t for t in (norm(x) for x in re.findall(r'[A-Za-z0-9][A-Za-z0-9/._-]{3,}',raw))
                      if len(t)>=7 and any(c.isdigit() for c in t)}
    prefix_hits=set()
    for iid in scoped:
        item=ctx.items[iid]
        n=norm(item.get('document_number'))
        if len(n)>=5 and n in compact:refs.add(iid)
        # POLICIES 5.5: a number quoted without its prefix ('INV24-0116705 0116143') is an
        # explicit reference even when a truncated token ('INV24-0116') is also its prefix.
        elif any(len(v)>=5 and n.endswith(v) for v in numeric_tokens):refs.add(iid)
        elif any(len(n)>len(t)>=max(7,len(n)*0.6) and n.startswith(t) for t in truncated_tokens):prefix_hits.add(iid)
        elif item.get('customer_po') and len(norm(item['customer_po']))>=6 and norm(item['customer_po']) in compact:refs.add(iid)
    pdf_facts=getattr(ctx,'pdf_facts',None)
    if pdf_facts is not None:
        refs={pdf_facts.resolve(i) for i in refs if i in ctx.items}
    valid_refs=[ctx.items[i] for i in refs if i in ctx.items and _eligible(ctx.items[i],txn,returning)]
    # Identical document numbers across companies need corroborating party evidence.
    if parties:
        preferred=[i for i in valid_refs if i.get('party_id') in parties]
        if preferred:valid_refs=preferred
    if returning:
        candidates=valid_refs or [ctx.items[i] for i in scoped if _eligible(ctx.items[i],txn,True)]
        exact=[]
        for item in candidates:
            cash,fx=_cash(item,txn)
            if cash is None:continue
            prior=[h for h in getattr(ctx,'history',{}).get(item['item_id'],())
                   if h.get('exception') not in ('RETURNED','DUPLICATE_PAYMENT')]
            historical_match=any(abs(cash-dec(h.get('amount')))<=CENT for h in prior)
            explicit_partial=len(valid_refs)==1 and cash<=dec(item.get('total_amount'))
            if historical_match or explicit_partial or abs(cash-dec(item.get('total_amount')))<=CENT:
                exact.append((item,cash))
        if len(exact)==1:return [row(txn,exact[0][0]['item_id'],exact[0][1],'RETURNED')]
        return None
    # Explicit multi-invoice references; credit netting remains another module's job.
    open_refs=[i for i in valid_refs if dec(i.get('open_amount'))>0]
    if len(open_refs)>1 and all(i.get('currency')==txn.get('currency') for i in open_refs):
        total=sum((dec(i['open_amount']) for i in open_refs),D(0))
        if abs(total-abs(dec(txn['amount'])))<=CENT:
            return [row(txn,i['item_id'],i['open_amount']) for i in sorted(open_refs,key=lambda x:x['item_id'])]
    # The same quoted invoices settled through a documented difference: instructed amount in
    # the invoice currency (FX_DIFFERENCE), discounts or withholding (POLICIES 3).
    if len(open_refs)>1 and not returning and dec(txn['amount'])>0:
        from .adjusted import explain_set
        quoted_invoices=[i for i in open_refs if i.get('kind')=='invoice' and i.get('side')=='AR']
        quoted_credits=[i for i in open_refs if i.get('kind')=='credit']
        if len(quoted_invoices)+len(quoted_credits)==len(open_refs):
            adjusted_rows=explain_set(txn,ctx,quoted_invoices,quoted_credits)
            if adjusted_rows is not None:
                return adjusted_rows
    # Netting subtracts credit notes economically, but every output amount is positive.
    if parties and not returning:
        side='AR' if dec(txn['amount'])>0 else 'AP'
        net_candidates=[ctx.items[i] for i in scoped if ctx.items[i].get('side')==side
                        and ctx.items[i].get('currency')==txn.get('currency')
                        and ctx.items[i].get('issue_date','')<=txn.get('booking_date','9999')
                        and dec(ctx.items[i].get('open_amount'))>0
                        and ctx.items[i].get('entity')==txn.get('entity')]
        net_refs=[i for i in net_candidates if i['item_id'] in refs]
        anchored = [i for i in net_refs if i.get('kind') != 'credit']
        anchored_parties = {i['party_id'] for i in anchored}
        credits = [i for i in net_candidates if i.get('kind') == 'credit' and i['party_id'] in anchored_parties]
        net_rows = _net_referenced(txn, anchored, credits, refs, ctx)
        if net_rows is not None:
            return net_rows
        if any(i.get('kind')=='credit' for i in net_refs):
            net=sum((dec(i['open_amount'])*(-1 if i.get('kind')=='credit' else 1) for i in net_refs),D(0))
            if abs(net-abs(dec(txn['amount'])))<=CENT:
                return [row(txn,i['item_id'],i['open_amount']) for i in net_refs]
        # An unmentioned credit can be established by a unique exact net against a referenced invoice.
        pairs=[]
        for inv in open_refs:
            if inv.get('kind')=='credit':continue
            for credit in net_candidates:
                if credit.get('kind')!='credit' or credit['party_id']!=inv['party_id']:continue
                if abs(dec(inv['open_amount'])-dec(credit['open_amount'])-abs(dec(txn['amount'])))<=CENT:
                    pairs.append((inv,credit))
        if len(pairs)==1:
            return [row(txn,i['item_id'],i['open_amount']) for i in pairs[0]]
    # POLICIES 5.5: bank texts truncate names and customers have lookalike names. A full
    # document number of one other customer identifies the payer whose book completes the list.
    rest_scope = scoped
    ref_parties = {i['party_id'] for i in open_refs}
    if (open_refs and len(ref_parties) == 1 and not ref_parties & set(parties)
            and all(i['item_id'] in ids for i in open_refs) and _name_echo(ctx, next(iter(ref_parties)), compact)):
        rest_scope = list(scoped) + list(ctx.by_party.get(next(iter(ref_parties)), ()))
    gap_rows = _gap_fill(txn, open_refs, candidates_of(rest_scope, txn, ctx), ctx)
    if gap_rows is None and not open_refs and prefix_hits:
        # Truncated references are weak: they only seed quoted items whose total, alone
        # or with a FIFO-shaped fill, equals the cash; they never act as a single reference.
        seeds=[ctx.items[i] for i in sorted(prefix_hits) if _eligible(ctx.items[i],txn)
               and dec(ctx.items[i].get('open_amount'))>0 and ctx.items[i].get('kind')!='credit']
        if seeds and len({i['party_id'] for i in seeds})==1:
            if all(i.get('currency')==txn.get('currency') for i in seeds) and \
                    abs(sum((dec(i['open_amount']) for i in seeds),D(0))-abs(dec(txn['amount'])))<=CENT:
                return [row(txn,i['item_id'],i['open_amount']) for i in sorted(seeds,key=lambda x:x['item_id'])]
            gap_rows = _gap_fill(txn, seeds, candidates_of(scoped, txn, ctx), ctx)
    if gap_rows is not None:
        return gap_rows
    if len(valid_refs)==1:
        item=valid_refs[0];cash,fx=_cash(item,txn)
        if cash is not None:
            opened=dec(item.get('open_amount'))
            if opened <= 0:
                # Restored closed identities are a candidate catalogue, not proof of
                # a duplicate. Bank references can collide with printed numbers.
                prior = [h for h in getattr(ctx, 'history', {}).get(item['item_id'], ())
                         if h.get('exception') not in ('RETURNED', 'DUPLICATE_PAYMENT')]
                prior_amount = any(abs(cash-dec(h.get('amount'))) <= CENT for h in prior)
                if item.get('historical_closed'):
                    party_ok = item.get('party_id') in parties
                    total_ok = abs(cash-dec(item.get('total_amount'))) <= CENT
                    if not (party_ok and (total_ok or prior_amount)):
                        return None
                elif not prior_amount:
                    return None
            # An accidental reference must not assign an arbitrarily huge overpayment.
            if opened>0 and cash>opened*D('1.2') and cash-opened>D(100):return None
            exception,applied=_exception(item,cash,txn,ctx,fx)
            return [row(txn,item['item_id'],applied,exception)]
    candidates=[ctx.items[i] for i in scoped if _eligible(ctx.items[i],txn)]
    exact=[]
    for item in candidates:
        cash,fx=_cash(item,txn)
        if cash is not None and dec(item.get('open_amount'))>0 and abs(cash-dec(item['open_amount']))<=CENT:
            exact.append((item,cash,fx))
    if exact and dec(txn['amount']) > 0:
        quoted = _quoted_by_sibling(ctx, txn, [x[0] for x in exact if x[0].get('side') == 'AR'])
        if quoted:
            exact = [x for x in exact if x[0]['item_id'] not in quoted]
    if exact:
        if len({x[0].get('party_id') for x in exact})!=1:
            # Several identified payers have an exact open item: the best-ranked payer
            # (IBAN/name evidence order from party_candidates) pays, as POLICIES 5.6 FIFO
            # applies within that payer.
            rank={pid:n for n,pid in enumerate(parties)}
            best=min(rank.get(x[0].get('party_id'),len(rank)) for x in exact)
            exact=[x for x in exact if rank.get(x[0].get('party_id'),len(rank))==best]
            if len({x[0].get('party_id') for x in exact})!=1:return None
        # Explicit service month outranks FIFO. PDF facts take precedence; the
        # documented next-month invoicing rule works without enabling PDFs.
        if len(exact)>1:
            stated = _stated_service_month(raw, txn.get('booking_date'))
            if stated:
                wanted, explicit_year = stated
                monthly = []
                for candidate in exact:
                    facts = pdf_facts.get(candidate[0]['item_id']) if pdf_facts is not None else {}
                    service = facts.get('service_month') or _invoice_service_month(candidate[0])
                    # Yearless text can mean an older unpaid month. Do not pick
                    # the latest year if two years remain equally plausible.
                    matches = service == wanted if explicit_year else bool(service and service[-2:] == wanted[-2:] and service <= wanted)
                    if matches:
                        monthly.append(candidate)
                if len(monthly) != 1:
                    return None
                exact = monthly
        if len(exact)>1:
            # POLICIES 5.10: after a returned direct debit the customer pays that invoice by
            # transfer; the re-opened item outranks older same-amount invoices (FIFO).
            reopened=[x for x in exact if any(h.get('exception')=='RETURNED'
                      for h in getattr(ctx,'history',{}).get(x[0]['item_id'],()))]
            if len(reopened)==1:
                exact=reopened
        exact.sort(key=lambda x:(x[0].get('issue_date',''),x[0]['item_id']))
        item,cash,fx=exact[0]
        return [row(txn,item['item_id'],item['open_amount'],'FX_DIFFERENCE' if fx and item.get('kind')!='credit' else '')]
    # A bounded exact subset search recovers truncated multi-invoice references.
    # It is restricted to one well-identified party and same-currency open invoices.
    byparty=defaultdict(list)
    cash=abs(dec(txn['amount']))
    # FX receipts carry the instructed amount in the invoice currency (orig_amount):
    # several invoices in that currency are matched against it, flagged FX_DIFFERENCE.
    orig_ccy=txn.get('orig_currency') or ''
    orig_cash=abs(dec(txn['orig_amount'])) if orig_ccy and orig_ccy!=txn.get('currency') and txn.get('orig_amount') else None
    for item in candidates:
        opened=dec(item.get('open_amount'))
        if item.get('kind')!='invoice':
            continue
        if item.get('currency')==txn.get('currency') and 0<opened<cash:
            byparty[(item['party_id'],False)].append(item)
        elif orig_cash is not None and item.get('currency')==orig_ccy and 0<opened<orig_cash:
            byparty[(item['party_id'],True)].append(item)
    solutions=[]
    for (party,fx_group),group in byparty.items():
        target=orig_cash if fx_group else cash
        group.sort(key=lambda i:i['item_id'])
        found = subset_solutions([int(dec(i['open_amount'])*100) for i in group], int(target*100), minimum=2)
        if found is None:
            continue
        if len(found) > 1:
            solutions.extend([None, None])
            break
        # A noisy printed number can collide with another customer's invoice;
        # only this party's references constrain this party's subset.
        anchors = {i['item_id'] for i in open_refs if i.get('party_id') == party}
        # Large amount-only combinations can coincide with a net payment that
        # included an omitted credit note. Demand at least two invoice anchors.
        supported_size = found and (len(found[0]) <= 6 or len(anchors) >= 2)
        if supported_size and anchors.issubset({group[i]['item_id'] for i in found[0]}):
            solutions.append((tuple(group[i] for i in found[0]),fx_group))
    if len(solutions)==1 and solutions[0] is not None:
        chosen,fx_group=solutions[0]
        return [row(txn,item['item_id'],item['open_amount'],'FX_DIFFERENCE' if fx_group else '') for item in chosen]
    explained=[]
    for item in candidates:
        cash,fx=_cash(item,txn)
        if cash is None or dec(item.get('open_amount'))<=0:continue
        exception,applied=_exception(item,cash,txn,ctx,fx)
        if exception in ('SHORT_DISCOUNT','SHORT_WITHHOLDING','SHORT_BANK_CHARGES'):
            explained.append((item,applied,exception))
    if len(explained)==1:
        item,amount,exception=explained[0]
        return [row(txn,item['item_id'],amount,exception)]
    quoted_invoices = [i for i in open_refs if i.get('kind') != 'credit']
    ladder = _known_payer_ladder(txn, ctx, parties, candidates, quoted_invoices)
    if ladder is not None:
        return ladder
    # Closed exact matches can be duplicate payments, but only with history evidence.
    dup=[]
    for item in candidates:
        cash,fx=_cash(item,txn)
        if cash is not None and dec(item.get('open_amount'))==0 and getattr(ctx,'history',{}).get(item['item_id']) and abs(cash-dec(item['total_amount']))<=CENT:
            dup.append((item,cash))
    if len(dup)==1:return [row(txn,dup[0][0]['item_id'],dup[0][1],'DUPLICATE_PAYMENT')]
    return None


OVERPAY_ROUND = {D(x) for x in ('1', '2', '5', '10', '20', '25', '50', '100', '200', '250', '500', '1000')}


def _days_between(a, b):
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return 99999


def _ordered_sets(group, target, booking):
    found = _ordered_sets_raw(group, target, booking)
    return found if found and _credits_supported(found) else None


def _credits_supported(items):
    """A credit note joins a set only with the invoice it references, and never cancels it fully
    (POLICIES 5.7: a cancelled invoice and its credit note are not settled by bank)."""
    invoices = {norm(i.get('document_number')): i for i in items if i.get('kind') != 'credit'}
    for credit in (i for i in items if i.get('kind') == 'credit'):
        related = invoices.get(norm(credit.get('related_document')))
        if related is None or dec(credit.get('open_amount')) >= dec(related.get('open_amount')):
            return False
    return True


def _ordered_sets_raw(group, target, booking):
    """FIFO-shaped exact sets over one payer's open items (signed cents, credits negative).

    Order: oldest-first prefix, due-date prefix, then the contiguous run (by issue or due
    order) whose latest due date is closest to the payment. Never a free subset-sum:
    unconstrained exact sums produce spurious matches on large open books.
    """
    n = len(group)
    if n < 2:
        return None
    vals = [int(dec(i['open_amount']) * 100) * (-1 if i.get('kind') == 'credit' else 1) for i in group]
    invoice = [i.get('kind') != 'credit' for i in group]
    by_issue = list(range(n))
    by_due = sorted(by_issue, key=lambda k: (group[k].get('due_date') or group[k].get('issue_date', ''),
                                             group[k].get('issue_date', ''), group[k]['item_id']))
    for order in (by_issue, by_due):
        run = 0
        for j, k in enumerate(order):
            run += vals[k]
            if run == target and j >= 1 and any(invoice[x] for x in order[:j + 1]):
                return [group[x] for x in order[:j + 1]]
    if n > 400:
        return None
    dist = [_days_between(i.get('due_date') or i.get('issue_date'), booking) for i in group]
    best = None
    for order in (by_issue, by_due):
        seen = {0: [0]}
        run = 0
        for j, k in enumerate(order, 1):
            run += vals[k]
            for a in seen.get(run - target, ()):
                if j - a >= 2:
                    idx = order[a:j]
                    if any(invoice[x] for x in idx):
                        key = (max(dist[x] for x in idx), len(idx))
                        if best is None or key < best[0]:
                            best = (key, idx)
            seen.setdefault(run, []).append(j)
    return [group[x] for x in best[1]] if best else None


def _known_payer_ladder(txn, ctx, parties, candidates, quoted_invoices=()):
    """Resolve a receipt/payment of an identified payer that no unique rule explained.

    Payers are tried in evidence order; the first FIFO-shaped exact set wins. Then a
    single invoice overpaid by a round amount or exactly 1 %/10 % (OVERPAYMENT).
    """
    if not parties or not candidates:
        return None
    cash = abs(dec(txn['amount']))
    orig_ccy = txn.get('orig_currency') or ''
    orig_cash = abs(dec(txn['orig_amount'])) if orig_ccy and orig_ccy != txn.get('currency') and txn.get('orig_amount') else None
    rank = {pid: n for n, pid in enumerate(parties)}
    groups = defaultdict(list)
    for item in candidates:
        if dec(item.get('open_amount')) <= 0 or item.get('entity') != txn.get('entity'):
            continue
        if item.get('currency') == txn.get('currency'):
            groups[(rank.get(item['party_id'], len(rank)), item['party_id'], False)].append(item)
        elif orig_cash is not None and item.get('currency') == orig_ccy:
            groups[(rank.get(item['party_id'], len(rank)), item['party_id'], True)].append(item)
    for key in sorted(groups):
        fx_group = key[2]
        target = orig_cash if fx_group else cash
        group = sorted(groups[key], key=lambda i: (i.get('issue_date', ''), i['item_id']))
        found = _ordered_sets(group, int(target * 100), txn.get('booking_date', ''))
        if found:
            return [row(txn, i['item_id'], i['open_amount'],
                        'FX_DIFFERENCE' if fx_group and i.get('kind') != 'credit' else '')
                    for i in sorted(found, key=lambda i: i['item_id'])]
    # POLICIES 3: OVERPAYMENT is more cash than the open amount of a single invoice. A
    # receipt that quotes two or more open invoices is not a single-invoice overpayment.
    if len(quoted_invoices) >= 2:
        return None
    for key in sorted(groups):
        if key[2]:
            continue
        best = None
        for item in groups[key]:
            if item.get('kind') == 'credit':
                continue
            opened = dec(item['open_amount'])
            excess = cash - opened
            if excess <= 0:
                continue
            signature = excess in OVERPAY_ROUND or any(
                abs(excess - (opened * r).quantize(CENT, rounding=ROUND_HALF_UP)) <= CENT for r in (D('0.01'), D('0.10')))
            if signature and (best is None or excess < best[0] or (excess == best[0] and item.get('issue_date', '') < best[1].get('issue_date', ''))):
                best = (excess, item)
        if best:
            return [row(txn, best[1]['item_id'], best[1]['open_amount'], 'OVERPAYMENT')]
    return None


def candidates_of(scoped, txn, ctx):
    return [ctx.items[i] for i in scoped if _eligible(ctx.items[i], txn)]


def _related_credit(ctx, txn, party):
    """Whether the payer has an open credit note that references one of its open invoices.

    POLICIES 2 and 5.9: such a receipt may pay invoices net of the credit note. Readings that
    pay only invoices (unique rest, quoted-invoice fallbacks) abstain, so the credit-note
    families (credit_groups) decide. Cancelling credit notes (5.7) never net a receipt.
    """
    if ctx is None or not party:
        return False
    entity, currency, booked = txn.get('entity'), txn.get('currency'), txn.get('booking_date', '9999')
    numbers, related = set(), set()
    for iid in ctx.by_party.get(party, ()):
        item = ctx.items.get(iid)
        if (item is None or item.get('side') != 'AR' or dec(item.get('open_amount')) <= 0
                or item.get('entity') != entity or item.get('currency') != currency
                or item.get('issue_date', '') > booked):
            continue
        if item.get('kind') == 'credit':
            if 'ANULACION' not in words(item.get('reason')):
                key = norm(item.get('related_document'))
                if key:
                    related.add(key)
        else:
            numbers.add(norm(item.get('document_number')))
    return bool(related & numbers)


def _name_echo(ctx, party, compact):
    """The bank text carries the first ten characters of one of the party's names (lookalike
    or truncated name, POLICIES 5.5), not merely a colliding document number."""
    aliases = _index(ctx)[2]
    return any(len(a) >= 10 and a[:10] in compact for a in aliases.get(party, ()))


def _credit_family_solves(ctx, txn, party):
    """The payer has an open related credit note and a credit-note family (credit_groups,
    which runs later) explains the receipt: the invoice-only unique rest defers to it."""
    if not _related_credit(ctx, txn, party):
        return False
    from .credit_groups import reconcile as credit_reconcile
    return credit_reconcile(txn, ctx) is not None


def _gap_fill(txn, open_refs, candidates, ctx=None):
    """Quoted invoices of one payer plus the oldest other open items that make up the rest.

    Bank texts truncate reference lists: the cash covers the quoted invoices and more of
    the same payer's open items. The remainder must be one exact oldest open item or a
    FIFO-shaped exact set (see _ordered_sets).
    """
    invoices = [i for i in open_refs if i.get('kind') != 'credit']
    if not invoices or len({i['party_id'] for i in open_refs}) != 1:
        return None
    currency = open_refs[0].get('currency')
    if any(i.get('currency') != currency for i in open_refs):
        return None
    # FX receipts: the instructed original amount is the cash in the invoice currency (POLICIES 3).
    from .adjusted import target_for
    cash, fx = target_for(txn, currency)
    if cash is None:
        return None
    gap = cash - sum((dec(i['open_amount']) * (-1 if i.get('kind') == 'credit' else 1) for i in open_refs), D(0))
    if gap <= CENT:
        return None
    quoted = {i['item_id'] for i in open_refs}
    party = open_refs[0]['party_id']
    rest = sorted((i for i in candidates if i['party_id'] == party and i['item_id'] not in quoted
                   and dec(i.get('open_amount')) > 0 and i.get('currency') == currency
                   and i.get('entity') == txn.get('entity')),
                  key=lambda i: (i.get('issue_date', ''), i['item_id']))
    fill = [i for i in rest if i.get('kind') != 'credit' and abs(dec(i['open_amount']) - gap) <= CENT][:1]
    if not fill:
        fill = _ordered_sets(rest, int(gap * 100), txn.get('booking_date', '')) or []
    if not fill and not _credit_family_solves(ctx, txn, party):
        fill = _unique_rest(rest, gap, txn.get('booking_date', '')) or []
    extra = None
    if not fill:
        from .adjusted import discount_rest
        extra = discount_rest(txn, ctx, open_refs, rest, gap, fx)
        if extra is None:
            return None
    rows = [row(txn, i['item_id'], i['open_amount'], 'FX_DIFFERENCE' if fx and i.get('kind') != 'credit' else '')
            for i in open_refs + fill]
    if extra is not None:
        rows.append(row(txn, extra[0]['item_id'], extra[1], extra[2]))
    return sorted(rows, key=lambda r: r['item_id'])


# Bank statement formats wrap long free text into fixed-width segments joined by ' | '
# (Norma 43 concept lines are 38 characters). A full-width segment continues in the next one.
_WRAP_WIDTHS = (38,)


def _unwrapped(text):
    parts = str(text or '').split(' | ')
    out = parts[0] if parts else ''
    for prev, part in zip(parts, parts[1:]):
        out += part if len(prev) in _WRAP_WIDTHS else ' ' + part
    return out


def _field_fragments(raw):
    raw = _unwrapped(raw)
    # SWIFT-style tags and the instructed amount / exchange rate are not references.
    raw = re.sub(r'/OCMT/[A-Z]{3}[0-9.,]+|/EXCH/[0-9.,]+', ' ', raw, flags=re.I)
    raw = re.sub(r'/(?:ORDP|REMI|EREF|REF|RFB|OBI|ORG|MOTIF|CHGS|BENM)/', ' ', raw, flags=re.I)
    tokens = []
    for match in re.finditer(r'[A-Za-z0-9][A-Za-z0-9/._-]*', raw):
        token = norm(match[0])
        if len(token) >= 5 and sum(c.isdigit() for c in token) >= 4:
            tokens.append(token)
    return tokens


def _fragment_tokens(txn):
    """Printed reference fragments: alphanumeric tokens with at least 4 digits, >= 5 chars."""
    tokens = []
    for key in ('text', 'ref1', 'ref2', 'remittance_info'):
        tokens += _field_fragments(txn.get(key))
    return list(dict.fromkeys(tokens))


def _printed_list(txn):
    """The longest single printed list (one field), keeping repeated fragments: one per document."""
    return max((_field_fragments(txn.get(key)) for key in ('text', 'ref1', 'ref2', 'remittance_info')),
               key=len)


def _anchor_fill(txn, anchors, candidates, currency, cash, ctx=None, fx=False, discounted=None):
    """_gap_fill with an explicit settlement currency and cash (orig_amount for FX receipts)."""
    party = anchors[0]['party_id']
    gap = cash - sum((dec(i['open_amount']) * (-1 if i.get('kind') == 'credit' else 1) for i in anchors), D(0))
    if gap <= CENT:
        return None
    quoted = {i['item_id'] for i in anchors}
    rest = sorted((i for i in candidates if i['party_id'] == party and i['item_id'] not in quoted
                   and dec(i.get('open_amount')) > 0 and i.get('currency') == currency
                   and i.get('entity') == txn.get('entity')),
                  key=lambda i: (i.get('issue_date', ''), i['item_id']))
    fill = [i for i in rest if i.get('kind') != 'credit' and abs(dec(i['open_amount']) - gap) <= CENT][:1]
    if not fill:
        fill = _ordered_sets(rest, int(gap * 100), txn.get('booking_date', '')) or []
    if not fill:
        fill = _unique_rest(rest, gap, txn.get('booking_date', '')) or []
    if not fill and discounted is not None:
        from .adjusted import discount_rest
        extra = discount_rest(txn, ctx, anchors, rest, gap, fx)
        if extra is not None:
            discounted[extra[0]['item_id']] = extra[1:]
            fill = [extra[0]]
    return sorted(anchors + fill, key=lambda i: i['item_id']) if fill else None


WITHHOLDING_COUNTRIES = ('MX', 'BR', 'MA')
_WHT_RATES = (D('0.0125'), D('0.015'), D('0.10'))


def _withholding_fill(txn, ctx, anchors, candidates, cash):
    """Quoted invoices plus the unique rest of the payer's open invoices, all net of one tax rate.

    POLICIES 3 SHORT_WITHHOLDING: some customers in Mexico, Brazil and Morocco withhold
    1.25 %, 1.5 % or 10 % of each invoice total; every invoice is fully cleared at its open
    amount. A truncated list (5.5) quotes only some of them. Accept the reading only when
    exactly one (rate, rest) combination makes (anchors + rest) x (1 - rate) equal the cash
    to the cent; the rest is a bounded exact subset of whole invoices (_unique_rest pool).
    """
    party = anchors[0]['party_id']
    if getattr(ctx, 'customers', {}).get(party, {}).get('country') not in WITHHOLDING_COUNTRIES:
        return None
    if any(i.get('kind') == 'credit' or dec(i['open_amount']) != dec(i.get('total_amount')) for i in anchors):
        return None
    quoted = {i['item_id'] for i in anchors}
    rest = sorted((i for i in candidates if i['party_id'] == party and i['item_id'] not in quoted
                   and i.get('kind') != 'credit' and dec(i.get('open_amount')) > 0
                   and dec(i['open_amount']) == dec(i.get('total_amount'))
                   and i.get('currency') == txn.get('currency') and i.get('entity') == txn.get('entity')),
                  key=lambda i: (i.get('issue_date', ''), i['item_id']))
    if len(rest) > _REST_MAX_ITEMS:
        try:
            limit = (date.fromisoformat(txn['booking_date']) - timedelta(days=_REST_WINDOW_DAYS)).isoformat()
        except (KeyError, TypeError, ValueError):
            return None
        rest = [i for i in rest if i.get('issue_date', '') >= limit]
    if len(rest) > _REST_MAX_ITEMS:
        return None
    base = int(sum(dec(i['open_amount']) for i in anchors) * 100)
    cash_c = int(cash * 100)
    values = [int(dec(i['open_amount']) * 100) for i in rest]
    readings = set()
    for rate in _WHT_RATES:
        keep = D(1) - rate
        lo = int(((D(cash_c) - D('1.5')) / keep).to_integral_value(rounding=ROUND_FLOOR))
        for total in range(lo, lo + int(D(4) / keep) + 2):
            net = (D(total) * keep).to_integral_value(rounding=ROUND_HALF_UP)
            if abs(net - cash_c) > 1 or total < base:
                continue
            if total == base:
                readings.add(())
                continue
            found = positive_subsets(values, total - base, maximum_items=_REST_MAX_ITEMS)
            if found is None or len(found) > 1:
                return None
            if found:
                readings.add(tuple(sorted(rest[k]['item_id'] for k in found[0])))
    if len(readings) != 1:
        return None
    extra = set(next(iter(readings)))
    return sorted(anchors + [i for i in rest if i['item_id'] in extra], key=lambda i: i['item_id'])


def _discount_fill(txn, ctx, anchors, candidates, gap):
    """Quoted invoices in full plus the one unquoted invoice paid net of its early-payment discount.

    POLICIES 3 SHORT_DISCOUNT (cash = total - HALF_UP(total x pct), booked within the discount
    days + 3; the newest invoice of a group keeps 10 more days) and 5.5 (truncated lists). The
    gap must be the discounted amount of exactly one open, untouched invoice of the payer.
    Returns anchors + [that invoice] (last), or None.
    """
    from .adjusted import discount_terms, newest_grace, _late
    party = anchors[0]['party_id']
    quoted = {i['item_id'] for i in anchors}
    hits = []
    for item in candidates:
        if (item['party_id'] != party or item['item_id'] in quoted or item.get('kind') == 'credit'
                or item.get('currency') != txn.get('currency') or item.get('entity') != txn.get('entity')):
            continue
        terms = discount_terms(item, ctx)
        if terms is None or abs(terms[0] - gap) > D('0.004'):
            continue
        if _late(txn, terms[1], newest_grace(item, list(anchors) + [item])):
            continue
        hits.append(item)
    if len(hits) != 1:
        return None
    return sorted(anchors, key=lambda i: i['item_id']) + hits


def fragment_partial(txn, ctx, min_anchors=1, min_fragments=2):
    """Partial allocation of a large receipt whose text lists truncated document numbers.

    POLICIES 5.5: references are truncated (16/35/38 characters) and printed without prefix;
    5.4: the deductions of such receipts are usually only visible in a remittance advice.
    When no advice and no exact set explains the cash, every printed fragment contained in
    exactly one open document of the identified payer anchors that document, at its full
    open amount. Ambiguous fragments are ignored; the unexplained rest stays unallocated.
    Never a free subset search: only printed evidence selects documents.
    FX receipts (POLICIES 3, FX_DIFFERENCE) settle documents in the instructed currency
    (orig_currency/orig_amount); their invoice rows carry FX_DIFFERENCE.
    """
    if dec(txn.get('amount')) <= 0 or classify(txn, ctx):
        return None
    raw = words(text_of(txn))
    if re.search(r'DEVOLUCION|RUECKLASTSCHRIFT|RETURNED|IMPAYE|CHARGEBACK|CONTRACARGO|RETURN', raw):
        return None
    parties = party_candidates(txn, ctx)
    if not parties:
        return None
    tokens = _fragment_tokens(txn)
    if len(tokens) < min_fragments:
        return None
    currency, cash, fx = txn.get('currency'), abs(dec(txn['amount'])), False
    orig_ccy = txn.get('orig_currency') or ''
    if orig_ccy and orig_ccy != currency and txn.get('orig_amount'):
        currency, cash, fx = orig_ccy, abs(dec(txn['orig_amount'])), True
    pool = []
    for pid in parties:
        for iid in ctx.by_party.get(pid, ()):
            item = ctx.items[iid]
            if (item.get('side') == 'AR' and item.get('currency') == currency
                    and item.get('entity') == txn.get('entity')
                    and item.get('issue_date', '') <= txn.get('booking_date', '')
                    and dec(item.get('open_amount')) > 0):
                pool.append((norm(item.get('document_number')), item))
    if not pool:
        return None
    # POLICIES 5.7: a printed cancelled number settles its documented replacement.
    pdf_facts = getattr(ctx, 'pdf_facts', None)
    pool_ids = {item['item_id'] for _, item in pool}
    anchors, matched = {}, 0
    for token in tokens:
        hits = [item for number, item in pool if len(number) >= len(token) and token in number]
        if hits:
            matched += 1
        if len(hits) == 1:
            iid = hits[0]['item_id']
            if pdf_facts is not None:
                iid = pdf_facts.resolve(iid)
                if iid not in pool_ids:
                    continue
            anchors[iid] = ctx.items[iid]
    if matched < min_fragments or len(anchors) < min_anchors:
        return None
    # Each printed fragment stands for a different document: a fragment whose other
    # candidates are all anchored already names the one that is left.
    sets = [{item['item_id'] for number, item in pool if len(number) >= len(token) and token in number}
            for token in _printed_list(txn)]
    progress = True
    while progress:
        progress = False
        for hits in sets:
            rest = hits - set(anchors)
            if len(hits) > 1 and len(rest) == 1:
                iid = next(iter(rest))
                if pdf_facts is not None:
                    iid = pdf_facts.resolve(iid)
                    if iid not in pool_ids or iid in anchors:
                        continue
                anchors[iid] = ctx.items[iid]
                progress = True
    if len({i['party_id'] for i in anchors.values()}) != 1:
        return None
    signed = sum((dec(i['open_amount']) * (-1 if i.get('kind') == 'credit' else 1) for i in anchors.values()), D(0))
    if signed <= 0:
        return None
    chosen = [anchors[iid] for iid in sorted(anchors)]
    discounted = {}
    scoped = [iid for pid in parties for iid in ctx.by_party.get(pid, ())]
    if signed > cash + CENT:
        held = None if fx else _withholding_fill(txn, ctx, chosen, candidates_of(scoped, txn, ctx), cash)
        return [row(txn, i['item_id'], i['open_amount'], 'SHORT_WITHHOLDING') for i in held] if held else None
    if signed < cash - CENT:
        # The anchored documents plus a FIFO-shaped exact remainder explain the whole cash.
        filled = _anchor_fill(txn, chosen, candidates_of(scoped, txn, ctx), currency, cash, ctx, fx, discounted)
        if filled:
            chosen = filled
        elif not fx:
            held = _withholding_fill(txn, ctx, chosen, candidates_of(scoped, txn, ctx), cash)
            if held:
                return [row(txn, i['item_id'], i['open_amount'], 'SHORT_WITHHOLDING') for i in held]
            net = _discount_fill(txn, ctx, chosen, candidates_of(scoped, txn, ctx), cash - signed)
            if net:
                return [row(txn, i['item_id'], i['open_amount'], 'SHORT_DISCOUNT' if i is net[-1] else '')
                        for i in net]
    multi = len(chosen) >= 2
    return [row(txn, i['item_id'], *discounted[i['item_id']]) if i['item_id'] in discounted else
            row(txn, i['item_id'], i['open_amount'],
                'FX_DIFFERENCE' if fx and i.get('kind') != 'credit'
                else ('SHORT_DISCOUNT' if multi and _in_discount_window(i, txn, ctx) else ''))
            for i in chosen]


def _in_discount_window(item, txn, ctx):
    """An unpaid invoice with an early-payment term, settled within its discount window.

    POLICIES §3 SHORT_DISCOUNT, amount = full open. TRAIN multi-invoice receipts outside the
    October/January windows: such items are SHORT_DISCOUNT in 58,010 of 58,017 cases (the
    payer deducts the discount from each invoice). Single-invoice receipts keep the exact
    discounted-cash rule of _exception (a full payment inside the window is no discount).
    """
    if item.get('kind') == 'credit' or item.get('side') != 'AR':
        return False
    terms = getattr(ctx, 'payment_terms', {})
    term = terms.get(item.get('payment_terms'), {}) if isinstance(terms, dict) else {}
    if not dec(term.get('early_payment_discount_pct')) or dec(item.get('open_amount')) != dec(item.get('total_amount')):
        return False
    try:
        deadline = date.fromisoformat(item['issue_date']) + timedelta(days=int(term['discount_days']) + 3)
        return date.fromisoformat(txn['booking_date']) <= deadline
    except (ValueError, KeyError, TypeError):
        return False


_REST_MAX_ITEMS = 30
_REST_WINDOW_DAYS = 180
_NM_MAX_ITEMS = 20


def party_partial_subset(txn, ctx):
    """Last resort: a receipt of an identified payer that pays several open invoices, one of
    them at a TRAIN partial share (40/50/60 % of its total, PARTIAL with the applied amount)
    and the others in full (POLICIES 2, 3; TRAIN ar_n_to_m).

    Only after every other handler abstained. The payer's book must be small (at most 20
    open invoices, else those of the last 180 days); no all-in-full combination may make up
    the cash, and the partial reading must be unique across every invoice, share and payer
    candidate. A payer with an open related credit note abstains (credit-note families).
    """
    cash = dec(txn['amount'])
    if cash <= 0 or classify(txn, ctx):
        return None
    if re.search(_RETURN_RE, words(text_of(txn))):
        return None
    parties = party_candidates(txn, ctx)
    if not parties:
        return None
    try:
        limit = (date.fromisoformat(txn['booking_date']) - timedelta(days=_REST_WINDOW_DAYS)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    cents = lambda amount: int((amount * 100).to_integral_value())
    readings = []
    for pid in parties:
        pool = [ctx.items[i] for i in ctx.by_party.get(pid, ()) if i in ctx.items]
        pool = [i for i in pool if i.get('kind') != 'credit' and i.get('side') == 'AR'
                and i.get('currency') == txn.get('currency') and i.get('entity') == txn.get('entity')
                and _eligible(i, txn) and dec(i.get('open_amount')) > 0]
        if not pool:
            continue
        if _related_credit(ctx, txn, pid):
            return None
        if len(pool) > _NM_MAX_ITEMS:
            pool = [i for i in pool if i.get('issue_date', '') >= limit]
        if len(pool) > _NM_MAX_ITEMS:
            return None
        smaller = sorted((i for i in pool if dec(i['open_amount']) < cash), key=lambda i: i['item_id'])
        plain = SubsetIndex([cents(dec(i['open_amount'])) for i in smaller], cents(cash), _NM_MAX_ITEMS)
        if not plain.ok or plain.masks(cents(cash), limit=1):
            return None   # an all-in-full reading exists: not a partial group
        # The other invoices of the group are fresh (TRAIN: no earlier partial residuals).
        full = [i for i in smaller if dec(i['open_amount']) == dec(i.get('total_amount'))]
        index = SubsetIndex([cents(dec(i['open_amount'])) for i in full], cents(cash), _NM_MAX_ITEMS)
        position = {i['item_id']: k for k, i in enumerate(full)}
        for item in pool:
            opened, total = dec(item['open_amount']), dec(item.get('total_amount'))
            if opened != total:
                continue
            values = {(total * f).quantize(CENT, rounding=r) for f in _PARTIAL_SHARES
                      for r in (ROUND_HALF_UP, ROUND_FLOOR)}
            exclude = 1 << position[item['item_id']] if item['item_id'] in position else 0
            for value in values:
                if not (D(0) < value < opened and value < cash):
                    continue
                for mask in index.masks(cents(cash - value), exclude=exclude, limit=2):
                    readings.append((item, value, [full[k] for k in range(len(full)) if mask >> k & 1]))
            if len(readings) > 1:
                return None
    if len(readings) != 1:
        return None
    partial, value, others = readings[0]
    rows = [row(txn, i['item_id'], i['open_amount']) for i in others]
    rows.append(row(txn, partial['item_id'], value, 'PARTIAL'))
    return sorted(rows, key=lambda r: r['item_id'])


def _unique_rest(rest, gap, booking):
    """The single exact combination of the payer's other open invoices that makes up the gap.

    Bank texts list the paid invoices in arbitrary order and cut the list (POLICIES 5.5), so
    the unquoted rest can be any open invoices of the quoting payer, each paid in full. Only a
    combination that is unique among the candidates is accepted. A payer with more than 30
    candidates is searched among invoices issued in the last 180 days (TRAIN: invoices paid by
    one multi-invoice transfer are almost always younger); a larger pool abstains.
    """
    pool = [i for i in rest if i.get('kind') != 'credit' and D(0) < dec(i['open_amount']) <= gap]
    if len(pool) > _REST_MAX_ITEMS:
        try:
            limit = (date.fromisoformat(booking) - timedelta(days=_REST_WINDOW_DAYS)).isoformat()
        except (TypeError, ValueError):
            return None
        pool = [i for i in pool if i.get('issue_date', '') >= limit]
    found = positive_subsets([int(dec(i['open_amount']) * 100) for i in pool], int(gap * 100),
                             maximum_items=_REST_MAX_ITEMS)
    if not found or len(found) != 1:
        return None
    return [pool[k] for k in found[0]]


SETTLEMENT_RE = re.compile(r'(?<![A-Z])(?:SETTLEMENT|REGULARIZACION)\s+([A-Z0-9/._-]*\d[A-Z0-9/._-]*)')
CARD_WORDS = ('CARD', 'TARJETA', 'TPV', 'REMESA', 'COMERCIO', 'MERCHANT', 'ACQUIR', 'CARTE', 'KARTE')


def dispute_settlement(txn, ctx):
    """POLICIES 3 SHORT_DISPUTE: the disputed part stays open and may be paid later; that
    later line has no exception. TRAIN: every 'SETTLEMENT <our number>' / 'REGULARIZACION
    <our number>' receipt is such a line. When an earlier dispute was not recognised the
    open balance is still larger than the cash, and ordinary matching would call the
    settlement PARTIAL; the applied amount stays the cash."""
    if dec(txn.get('amount')) <= 0:
        return None
    raw = text_of(txn)
    tokens = SETTLEMENT_RE.findall(str(raw).upper())
    if not tokens or any(w in words(raw) for w in CARD_WORDS):
        return None
    rows = reconcile(txn, ctx)
    if not rows or len(rows) != 1 or rows[0].get('exception') != 'PARTIAL':
        return None
    item = ctx.items.get(rows[0]['item_id'])
    if item is None or item.get('side') != 'AR' or item.get('kind') != 'invoice':
        return None
    number = norm(item.get('document_number'))
    digits = re.sub(r'^[A-Z]+', '', number)
    quoted = any(norm(t) == number or (len(digits) >= 6 and norm(t).endswith(digits)) for t in tokens)
    if not quoted:
        quoted = len(number) >= 7 and number in norm(raw) and any(len(norm(t)) >= 5 for t in tokens)
    if not quoted:
        return None
    fx = item.get('currency') != txn.get('currency')
    return [dict(rows[0], exception='FX_DIFFERENCE' if fx else '')]
