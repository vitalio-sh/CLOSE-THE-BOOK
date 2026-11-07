from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import csv
import gzip
import re
import unicodedata

D = Decimal

def norm(value):
    return re.sub(r'[^A-Z0-9]', '', unicodedata.normalize('NFKD', str(value or '')).encode('ascii','ignore').decode().upper())

def read_csv(path):
    path = Path(path)
    if not path.exists():
        return
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)

class Context:
    def __init__(self, data_root, phase):
        self.data_root = Path(data_root).resolve()
        self.phase_dir = self.data_root / phase
        self.masters_dir = self.phase_dir / 'masters_final' if phase == 'final' else self.data_root / 'masters'
        if not self.masters_dir.exists():
            self.masters_dir = self.data_root / 'masters'
        self.history_dirs = [self.data_root / p for p in ('train', 'live') if p != phase and (phase == 'final' or p == 'train')]
        if phase == 'train': self.history_dirs = []
        self.items = {}
        self.by_number = defaultdict(list)
        self.by_party = defaultdict(list)
        self.history = defaultdict(list)
        for attr, filename, key in [('customers','customers','customer_id'),('suppliers','suppliers','supplier_id'),('accounts','bank_accounts','account_id'),('payment_terms','payment_terms','code')]:
            setattr(self,attr,{r[key]:r for r in read_csv(self.masters_dir/(filename+'.csv'))})
        self.categories = {r['category'] for r in read_csv(self.masters_dir/'categories.csv')}
        self.fx_rates = defaultdict(dict)
        for r in read_csv(self.masters_dir/'fx_rates.csv'):
            self.fx_rates[r['date']][r['currency']] = D(r['units_per_eur'])
        self.load_items()

    def load_items(self):
        for r in read_csv(self.phase_dir/'open_items_at_start.csv.gz'):
            self.add_item(r)
        # Opening snapshots carry balances, but omit payment terms and historic names.
        # Enrich only those IDs; never load historical settled invoices as open.
        opening_ids=set(self.items)
        if opening_ids:
            for folder in self.history_dirs:
                for name in ('ar_invoices','ap_invoices','credit_notes'):
                    for r in read_csv(folder/(name+'.csv.gz')):
                        if r['item_id'] in opening_ids:
                            self.add_item(r)
        for name in ('ar_invoices','ap_invoices','credit_notes'):
            for r in read_csv(self.phase_dir/(name+'.csv.gz')):
                self.add_item(r)
        for iid,r in self.items.items():
            self.by_number[norm(r['document_number'])].append(iid)
            self.by_party[r['party_id']].append(iid)

    def add_item(self,r):
        iid=r['item_id']; previous=self.items.get(iid,{})
        v={**previous,**r}
        v['document_number']=r.get('document_number') or r.get('supplier_invoice_number','')
        v['party_id']=r.get('party_id') or r.get('customer_id') or r.get('supplier_id','')
        v['side']='AR' if iid.startswith('AR') else 'AP'
        v['kind']='credit' if iid.startswith(('ARC','APC')) else 'invoice'
        v['total_amount']=D(r['total_amount'])
        v['open_amount']=D(str(r.get('open_amount',previous.get('open_amount',r['total_amount']))))
        self.items[iid]=v

    def proposal_valid(self, txn, rows):
        seen=set()
        for row in rows:
            iid=row.get('item_id','')
            if row.get('bank_txn_id') != txn['bank_txn_id'] or iid in seen:
                return False
            seen.add(iid)
            if not iid:
                if len(rows)!=1: return False
                continue
            item=self.items.get(iid)
            if item is None or item.get('issue_date','')>txn['booking_date']:
                return False
            amount=D(str(row['amount']))
            if not amount.is_finite() or amount<0: return False
            if row.get('exception') not in ('RETURNED','DUPLICATE_PAYMENT') and amount>item['open_amount']+D('0.01'):
                return False
        return True

    def apply(self,txn,rows):
        for row in rows:
            iid=row['item_id']
            if not iid: continue
            item=self.items[iid]; amount=D(row['amount']); exception=row['exception']
            if exception=='RETURNED':
                item['open_amount'] += amount
            elif exception!='DUPLICATE_PAYMENT':
                item['open_amount']=max(D(0),item['open_amount']-amount)
            self.history[iid].append({**row,'booking_date':txn['booking_date'],'account_id':txn['account_id']})
