"""Evidence-based ordinary reconciliation. No labels are accessed at inference."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
import unicodedata

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
    return 'PARTIAL',cash

def classify(txn,ctx):
    """Only explicit category evidence; return None for unknown lines."""
    t=words(text_of(txn));positive=dec(txn['amount'])>0
    rules=[
      ('BANK_REVERSAL',r'ANULACION OPERACION INDEBIDA|BANK ERROR|ERROR REVERSAL|ERRONEOUS'),
      ('CASH_POOL',r'CASH POOL|CASHPOOL|ZBA SWEEP|ZERO BALANC|NIVELACION SALDO|TRASPASO PROPIO|TRASPASO ENTRE CUENTAS ES00'),
      ('INTERCOMPANY',r'INTRAGRUPO|INTERCOMPANY|FINANCIACION IC|PRESTAMO IC|TRANSF IC'),
      ('INTERNAL_TRANSFER',r'TRASPASO (?:A|DE) CTA|OWN ACCOUNT|INTERNAL TRANSFER|ENTRE CUENTAS PROPIAS'),
      ('CUSTOMER_ADVANCE',r'PROFORMA|ANTICIPO PF'),
      ('CARD_EXPENSES',r'ADEUDO MENSUAL DE TARJETA|CORPORATE CARD|CARD STATEMENT'),
      ('PAYROLL',r'NOMINA|PAYROLL|SALARIES|GEHALT|LOHN|SALAIRES'),
      ('SOCIAL_SECURITY',r'SEGURIDAD SOCIAL|SEG SOCIAL|SOCIAL CHARGES|SOCIAL SECURITY|URSSAF|SOZIAL'),
      ('LEASING',r'LEASING|RENTING|ARRENDAMIENTO|CUOTA LSG'),
      ('INSURANCE',r'PRIMA SEGURO|INSURANCE PREMIUM|POLIZA SEGURO|ASSURANCE'),
      ('LOAN_REPAYMENT',r'AMORTIZACION|LOAN REPAYMENT|CUOTA PRESTAMO|PRES [A-Z]{2}[0-9]'),
      ('LOAN_DRAWDOWN',r'DISPOSICION PRESTAMO|LOAN DRAWDOWN|ABONO PRESTAMO'),
      ('FX_DEAL',r'FX DEAL|FOREX DEAL|COMPRA DIVISAS|VENTA DIVISAS|COMPRAVENTA DIVISAS|OPERACION CAMBIO'),
    ]
    category=None
    for cat,pattern in rules:
        if re.search(pattern,t):category=cat;break
    if category is None and re.search(r'INTERE[ST]|ZINS',t):category='INTEREST_INCOME' if positive else 'INTEREST_EXPENSE'
    if category is None and re.search(r'IMPUEST|TAX PAYMENT|TAX REFUND|VAT RETURN|DEVOLUCION HACIENDA',t):category='TAX_REFUND' if positive else 'TAX_PAYMENT'
    if category is None and re.search(r'COMISION|COMMISSION|BANK FEES|SERVICE CHARGE|GEBUHR|FRAIS|MAINTENANCE FEE|LIQUIDACION CTA CTO|COM TRANSF|SERV EM TRANSF',t):category='BANK_FEES'
    if category is None and re.search(r'DEVOLUCION PAGO DUPLICADO|ABONO PAGO DOBLE|REEMBOLSO|DUPLICATE REFUND|REFUND DUPLICATE PAYMENT',t):category='SUPPLIER_REFUND' if positive else 'CUSTOMER_REFUND'
    return [row(txn,category=category)] if category else None

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
    for iid in scoped:
        item=ctx.items[iid]
        n=norm(item.get('document_number'))
        if len(n)>=5 and n in compact:refs.add(iid)
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
        if len({x[0].get('party_id') for x in exact})!=1:return None
        # Service-month evidence overrides FIFO only when explicitly stated in bank text and PDF.
        if pdf_facts is not None and len(exact)>1:
            from .pdf_facts import MONTHS
            w=words(raw)
            month_match=re.search(r'(?:SERVICE|SERVICIOS?|MANT|MAINTENANCE|MES|MONTH)\s+(?:DE\s+)?([A-Z]+)\s+(20\d{2}|\d{2})\b',w)
            if month_match and month_match[1] in MONTHS:
                year=month_match[2] if len(month_match[2])==4 else '20'+month_match[2]
                wanted=f'{year}-{MONTHS[month_match[1]]:02d}'
                monthly=[x for x in exact if pdf_facts.get(x[0]['item_id']).get('service_month')==wanted]
                if len(monthly)==1:exact=monthly
        exact.sort(key=lambda x:(x[0].get('issue_date',''),x[0]['item_id']))
        item,cash,fx=exact[0]
        return [row(txn,item['item_id'],item['open_amount'],'FX_DIFFERENCE' if fx and item.get('kind')!='credit' else '')]
    # A bounded exact subset search recovers truncated multi-invoice references.
    # It is restricted to one well-identified party and same-currency open invoices.
    byparty=defaultdict(list)
    cash=abs(dec(txn['amount']))
    for item in candidates:
        opened=dec(item.get('open_amount'))
        if item.get('kind')=='invoice' and item.get('currency')==txn.get('currency') and 0<opened<cash:
            byparty[item['party_id']].append(item)
    solutions=[]
    for party,group in byparty.items():
        if len(group)>24:continue
        amounts=defaultdict(list)
        for idx,item in enumerate(group):amounts[dec(item['open_amount'])].append(idx)
        for i,a in enumerate(group):
            rest=cash-dec(a['open_amount'])
            for j in amounts.get(rest,()):
                if j>i:solutions.append((a,group[j]))
            for j in range(i+1,len(group)):
                rest2=rest-dec(group[j]['open_amount'])
                for k in amounts.get(rest2,()):
                    if k>j:solutions.append((a,group[j],group[k]))
            if len(solutions)>1:break
        if len(solutions)>1:break
    if len(solutions)==1:
        return [row(txn,item['item_id'],item['open_amount']) for item in solutions[0]]
    explained=[]
    for item in candidates:
        cash,fx=_cash(item,txn)
        if cash is None or dec(item.get('open_amount'))<=0:continue
        exception,applied=_exception(item,cash,txn,ctx,fx)
        if exception in ('SHORT_DISCOUNT','SHORT_WITHHOLDING'):
            explained.append((item,applied,exception))
    if len(explained)==1:
        item,amount,exception=explained[0]
        return [row(txn,item['item_id'],amount,exception)]
    # Closed exact matches can be duplicate payments, but only with history evidence.
    dup=[]
    for item in candidates:
        cash,fx=_cash(item,txn)
        if cash is not None and dec(item.get('open_amount'))==0 and getattr(ctx,'history',{}).get(item['item_id']) and abs(cash-dec(item['total_amount']))<=CENT:
            dup.append((item,cash))
    if len(dup)==1:return [row(txn,dup[0][0]['item_id'],dup[0][1],'DUPLICATE_PAYMENT')]
    return None
