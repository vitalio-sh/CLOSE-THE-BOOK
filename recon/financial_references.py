"""Bounded finance references with typed numeric lists and monetary masking.

The recall owner's strict-anchor contract is shared conceptually, but this module
has a distinct name/API and does not override matching.py helper definitions.
"""
from decimal import Decimal as D
import re
from .matching import norm,dec,party_candidates
from .reference_spans import classify_numeric_reference_span,numeric_reference_span_safe

DOC_CUES={'FACTURA','FACTURAS','FAC','FACS','FRA','FRAS','FACT','FACTS','INVOICE','INVOICES','INV','RECHNUNG','RECHNUNGEN','RENR','FACTURE','FACTURES','FATTURA','FATTURE'}
PO_CUES={'PO','OC','ORDER','PEDIDO','COMMANDE'}
CURRENCIES={'EUR','USD','GBP','CHF','CAD','AUD','PLN','MXN','BRL','CNY','ZAR','MAD','SEK','NOK','DKK','JPY'}


def finance_reference_sets(txn,ctx,facts=None):
    parties=set(party_candidates(txn,ctx));owner=txn.get('entity')
    byid={i:ctx.items[i] for p in parties for i in ctx.by_party.get(p,())
          if ctx.items[i].get('issue_date','')<=txn['booking_date'] and
          (ctx.items[i].get('entity')==owner or (owner=='ES00' and ctx.items[i].get('side')=='AP' and ctx.items[i].get('entity') in ('ES01','ES02','PT01')))}
    refs=set();exact=set();cash=abs(dec(txn['amount']))
    for field in ('text','ref1','ref2','remittance_info','end_to_end_id'):
        raw=str(txn.get(field) or '');mode=None;previous_end=0
        for match in re.finditer(r'[A-Za-z0-9][A-Za-z0-9/._-]*',raw):
            token=match[0].rstrip('.,');key=norm(token)
            if not re.fullmatch(r'[\s,;:/#]*',raw[previous_end:match.start()]):mode=None
            previous_end=match.end()
            if key in DOC_CUES:mode='invoice';continue
            if key in PO_CUES:mode='po';continue
            if mode=='invoice' and key in ('NO','NR','N'):continue
            doc_hits=set();po_hits=set()
            if re.search(r'[A-Za-z]',token) and re.search(r'[0-9]',token):
                doc_hits={i for i in ctx.by_number.get(key,()) if i in byid}
                po_hits={i for i,v in byid.items() if norm(v.get('customer_po'))==key}
            if doc_hits or po_hits:
                refs.update(doc_hits|po_hits);exact.update(doc_hits)
                mode='invoice' if doc_hits else 'po';continue
            if not token.isdigit() or len(token)<5 or mode is None:
                mode=None;continue
            span=classify_numeric_reference_span(raw,match.start(),match.end())
            known=set()
            if span.kind=='reference_list':
                for component in span.components:
                    if mode=='invoice':
                        hits={i for i,v in byid.items() if norm(v.get('document_number')).endswith(component)}
                    else:
                        hits={i for i,v in byid.items() if norm(v.get('customer_po')) in {component,'PO'+component,'OC'+component}}
                    if len(hits)==1:known.add(component)
            if not numeric_reference_span_safe(raw,match.start(),match.end(),known_reference_tokens=known):
                mode=None;continue
            if D(token)==cash or D(token)/100==cash:continue
            if mode=='invoice':
                doc_hits={i for i,v in byid.items() if norm(v.get('document_number'))==token}
                refs.update(i for i,v in byid.items() if norm(v.get('document_number')).endswith(token))
                exact.update(doc_hits)
            else:
                refs.update(i for i,v in byid.items() if norm(v.get('customer_po')) in {token,'PO'+token,'OC'+token})
    if facts is None:facts=getattr(ctx,'pdf_facts',None)
    if facts is not None and hasattr(facts,'resolve'):
        refs={facts.resolve(i) for i in refs};exact={facts.resolve(i) for i in exact}
    # Closed identities and credits remain in the ambiguity catalogue.
    valid={i for i in refs if i in ctx.items and ctx.items[i].get('party_id') in parties
           and ctx.items[i].get('issue_date','')<=txn['booking_date']
           and (ctx.items[i].get('entity')==owner or (owner=='ES00' and ctx.items[i].get('side')=='AP' and ctx.items[i].get('entity') in ('ES01','ES02','PT01')))}
    return {'all':valid,'exact_documents':exact&valid}
