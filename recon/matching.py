"""Evidence-based ordinary reconciliation. No labels are accessed at inference."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
import unicodedata
from .subsets import unique_subset, subset_solutions

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
    # Historical printed names are necessary when current masters have been renamed.
    for item in ctx.items.values():
        pid = item.get('party_id')
        if not pid: continue
        for key in ('customer_name','supplier_name','party_name'):
            n = norm(item.get(key))
            if len(n) >= 6 and n not in aliases[pid]:
                aliases[pid].add(n); prefixes[n[:10]].add(pid)
    cached = (prefixes,ibans,aliases,children,parties)
    ctx._matching_index = cached
    return cached

def party_candidates(txn,ctx):
    prefixes,ibans,aliases,children,parties = _index(ctx)
    raw = text_of(txn); compact=norm(raw)
    candidates=set()
    for i in range(max(0,len(compact)-9)):
        candidates.update(prefixes.get(compact[i:i+10],()))
    for token in re.findall('[A-Za-z0-9-]{8,}',raw):
        candidates.update(ibans.get(norm(token),()))
    acct=norm(txn.get('counterparty_account'))
    candidates.update(ibans.get(acct,()))
    ranked=[]
    explicit=norm(txn.get('counterparty_name'))
    for pid in candidates:
        p=parties.get(pid,{})
        account_hit=any(norm(p.get(k)) and norm(p.get(k)) in compact for k in ('iban','account_number','sepa_mandate_id'))
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
    ranked.sort(reverse=True)
    threshold=max(42,ranked[0][0]-8)
    selected=[pid for score,pid in ranked if score>=threshold][:12]
    for pid in list(selected): selected.extend(children.get(pid,()))
    return list(dict.fromkeys(selected))

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

def _exception(item, cash, txn, ctx, fx=False):
    opened=dec(item.get('open_amount',item.get('total_amount')))
    if opened <= 0:return 'DUPLICATE_PAYMENT',cash
    if abs(cash-opened)<=CENT:return ('FX_DIFFERENCE' if fx and item.get('kind')!='credit' else ''),opened
    if cash > opened:
        return ('FX_DIFFERENCE' if fx else 'OVERPAYMENT'),opened
    # Discounts must use booking date and the total, not remaining balance.
    terms=getattr(ctx,'payment_terms',{})
    term=terms.get(item.get('payment_terms'),{}) if isinstance(terms,dict) else {}
    pct=dec(term.get('early_payment_discount_pct'))
    total=dec(item.get('total_amount'))
    if pct and opened==total:
        expected=(total*(D(1)-pct/D(100))).quantize(CENT,rounding=ROUND_HALF_UP)
        try:
            deadline=date.fromisoformat(item['issue_date'])+timedelta(days=int(term['discount_days'])+3)
            if abs(cash-expected)<=CENT and date.fromisoformat(txn['booking_date'])<=deadline:
                return ('FX_DIFFERENCE' if fx else 'SHORT_DISCOUNT'),opened
        except (ValueError,KeyError,TypeError):pass
    customer=getattr(ctx,'customers',{}).get(item.get('party_id'),{})
    if customer.get('country') in ('MX','BR','MA') and opened==total:
        for rate in (D('0.0125'),D('0.015'),D('0.10')):
            if abs(cash-(total*(1-rate)).quantize(CENT,rounding=ROUND_HALF_UP))<=CENT:
                return ('FX_DIFFERENCE' if fx else 'SHORT_WITHHOLDING'),opened
    text=words(text_of(txn))
    if any(x in text for x in ('DISPUT','RECLAM','DAMAGED','PENALTY','DEDUCCION')):
        return 'SHORT_DISPUTE',cash
    # Intermediary charges on a documented non-SEPA cross-border receipt.
    # A small difference alone never establishes this exception.
    sepa={'AD','AL','AT','BE','BG','CH','CY','CZ','DE','DK','EE','ES','FI','FR','GB','GI','GR','HR','HU','IE','IS','IT','LI','LT','LU','LV','MC','MD','ME','MK','MT','NL','NO','PL','PT','RO','RS','SE','SI','SK','SM','VA'}
    country=customer.get('country','').upper()
    bank_text=words(' | '.join(str(txn.get(k) or '') for k in ('text','ref1','ref2','remittance_info')))
    transfer=bool(re.search(r'\b(?:SWIFT|TRANSFER EN DIV|TRANSFERENCIA INTERNACIONAL|INTERNATIONAL TRANSFER|WIRE TRANSFER|GASTOS (?:SHA|BEN)|CHGS)\b',bank_text))
    # ISO 20022 bank transaction code PMNT/RCDT/XBCT = cross-border credit transfer.
    transfer=transfer or 'XBCT' in str(txn.get('bank_code') or '').upper()
    if (not fx and item.get('side')=='AR' and item.get('kind')!='credit'
            and country and country not in sepa and transfer):
        short=opened-cash
        currency=item.get('currency')
        rate=D(1) if currency=='EUR' else None
        if rate is None:
            rates=getattr(ctx,'fx_rates',{})
            dates=[day for day,values in rates.items() if day<=txn.get('booking_date','') and currency in values]
            if dates:rate=dec(rates[max(dates)][currency])
        if rate and rate>0 and 0<short<=opened*D('0.02') and short/rate<=D(60):
            return 'SHORT_BANK_CHARGES',opened
    return 'PARTIAL',cash

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
    for index in selected:
        amount, kind, item = deductions[index]
        if kind == 'credit':
            rows[item['item_id']] = row(txn, item['item_id'], item['open_amount'])
        else:
            exc, applied = _exception(item, dec(item['open_amount'])-amount, txn, ctx)
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

def reconcile(txn,ctx):
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
    for iid in scoped:
        item=ctx.items[iid]
        n=norm(item.get('document_number'))
        if len(n)>=5 and n in compact:refs.add(iid)
        elif any(len(n)>len(t)>=max(7,len(n)*0.6) and n.startswith(t) for t in truncated_tokens):refs.add(iid)
        elif any(len(v)>=5 and n.endswith(v) for v in numeric_tokens):refs.add(iid)
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
    gap_rows = _gap_fill(txn, open_refs, candidates_of(scoped, txn, ctx))
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
    ladder = _known_payer_ladder(txn, ctx, parties, candidates)
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


def _known_payer_ladder(txn, ctx, parties, candidates):
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


def _gap_fill(txn, open_refs, candidates):
    """Quoted invoices of one payer plus the oldest other open items that make up the rest.

    Bank texts truncate reference lists: the cash covers the quoted invoices and more of
    the same payer's open items. The remainder must be one exact oldest open item or a
    FIFO-shaped exact set (see _ordered_sets).
    """
    invoices = [i for i in open_refs if i.get('kind') != 'credit']
    if not invoices or len({i['party_id'] for i in open_refs}) != 1:
        return None
    if any(i.get('currency') != txn.get('currency') for i in open_refs):
        return None
    cash = abs(dec(txn['amount']))
    gap = cash - sum((dec(i['open_amount']) * (-1 if i.get('kind') == 'credit' else 1) for i in open_refs), D(0))
    if gap <= CENT:
        return None
    quoted = {i['item_id'] for i in open_refs}
    party = open_refs[0]['party_id']
    rest = sorted((i for i in candidates if i['party_id'] == party and i['item_id'] not in quoted
                   and dec(i.get('open_amount')) > 0 and i.get('currency') == txn.get('currency')
                   and i.get('entity') == txn.get('entity')),
                  key=lambda i: (i.get('issue_date', ''), i['item_id']))
    fill = [i for i in rest if i.get('kind') != 'credit' and abs(dec(i['open_amount']) - gap) <= CENT][:1]
    if not fill:
        fill = _ordered_sets(rest, int(gap * 100), txn.get('booking_date', '')) or []
    if not fill:
        return None
    return [row(txn, i['item_id'], i['open_amount']) for i in sorted(open_refs + fill, key=lambda i: i['item_id'])]
