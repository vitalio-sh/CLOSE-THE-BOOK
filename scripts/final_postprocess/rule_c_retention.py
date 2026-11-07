"""Rule C (POLICIES §3: the release of a retention carries no exception). A bank line whose text states a retention release and
that settles exactly one item with exception PARTIAL gets an empty exception (amount unchanged; skipped across currencies).
Usage: python3 -I rule_c_retention.py <in.csv.gz> <phase_dir> <out.csv.gz>"""
import csv, gzip, io, json, re, sys
from collections import defaultdict
src, P, out = sys.argv[1:4]
PAT = re.compile(r'RETENTION\s+RELEASE|RELEASE\s+(OF\s+)?RETENTION|LIBERACI[OÓ]N\s+(DE\s+)?RETEN|RETENCI[OÓ]N\s+LIBER|DEVOLUCI[OÓ]N\s+(DE\s+)?RETEN')
bank = {}
with gzip.open(P + '/bank_transactions.csv.gz', 'rt', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        bank[r['bank_txn_id']] = (r['text'].upper(), r.get('currency', ''))
ccy = {}
for fn in ('open_items_at_start', 'ar_invoices', 'credit_notes', 'ap_invoices'):
    try:
        with gzip.open(f'{P}/{fn}.csv.gz', 'rt', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                ccy.setdefault(r['item_id'], r.get('currency', ''))
    except FileNotFoundError:
        pass
with gzip.open(src, 'rt', encoding='utf-8', newline='') as f:
    rd = csv.DictReader(f); header = rd.fieldnames; rows = list(rd)
by = defaultdict(list)
for r in rows:
    by[r['bank_txn_id']].append(r)
n = 0
for t, rs in by.items():
    text, bc = bank.get(t, ('', ''))
    items = [r for r in rs if r['item_id']]
    if len(items) == 1 and items[0]['exception'] == 'PARTIAL' and PAT.search(text) and ccy.get(items[0]['item_id']) == bc:
        items[0]['exception'] = ''
        n += 1
buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=header, lineterminator='\n'); w.writeheader()
for r in rows:
    w.writerow({k: r.get(k, '') for k in header})
with gzip.open(out, 'wb') as f:
    f.write(buf.getvalue().encode('utf-8'))
print(json.dumps({'rule_c_retention_release': n, 'rows': len(rows)}))
