"""For wrong MD06 picks: days between the return and the golden / picked collection (from direct_debit_batches)."""
import csv, glob, gzip, json, os, re, sys
from collections import defaultdict
from datetime import date
data, phase, sub = sys.argv[1], sys.argv[2], sys.argv[3]
res = json.load(open(sys.argv[4], encoding='utf-8')) if len(sys.argv) > 4 else None
def rows(p):
    with gzip.open(p, 'rt', encoding='utf-8') as fh: yield from csv.DictReader(fh)
P = os.path.join(data, phase)
bank = {t['bank_txn_id']: t for t in rows(P + '/bank_transactions.csv.gz')}
gold = defaultdict(list); seg = {}
for r in rows(P + '/golden_allocations.csv.gz'): gold[r['bank_txn_id']].append(r); seg[r['bank_txn_id']] = r['segment']
subm = defaultdict(list)
for r in rows(sub): subm[r['bank_txn_id']].append(r)
# collections per (mandate, item): from golden allocations of DD batch lines (item -> collection dates)
coll = defaultdict(list)
for k, g in gold.items():
    if seg[k] in ('ar_dd_batch',):
        for r in g:
            if r['item_id']: coll[r['item_id']].append(bank[k]['booking_date'])
# also collections before the window: batch files in train/ and live/ by remittance -> not item; use golden of train
TR = os.path.join(data, 'train', 'golden_allocations.csv.gz')
need = set()
wrong = []
for k, g in gold.items():
    if seg[k] != 'ar_dd_returned': continue
    t = bank[k]; text = ' '.join(t.get(x) or '' for x in ('text', 'remittance_info')).upper()
    if not re.search(r'(?<![A-Z0-9])MD06(?![A-Z0-9])', text): continue
    gi = {r['item_id'] for r in g if r['item_id']}; si = {r['item_id'] for r in subm[k] if r['item_id']}
    if si and si != gi:
        wrong.append((k, t['booking_date'], gi, si)); need |= gi | si
trbank = {}
if need and os.path.exists(TR):
    ids = set()
    for r in rows(TR):
        if r['item_id'] in need: ids.add(r['bank_txn_id']); coll.setdefault(r['item_id'], [])
    for t in rows(os.path.join(data, 'train', 'bank_transactions.csv.gz')):
        if t['bank_txn_id'] in ids: trbank[t['bank_txn_id']] = t['booking_date']
    for r in rows(TR):
        if r['item_id'] in need and r['bank_txn_id'] in trbank and r['exception'] != 'RETURNED':
            coll[r['item_id']].append(trbank[r['bank_txn_id']])
d = lambda a, b: (date.fromisoformat(a) - date.fromisoformat(b)).days
out = []
for k, day, gi, si in wrong:
    g = next(iter(gi)); s = next(iter(si))
    gd = [d(day, x) for x in coll.get(g, []) if x < day]; sd = [d(day, x) for x in coll.get(s, []) if x < day]
    out.append((k, day, min(gd) if gd else None, min(sd) if sd else None))
    print(f"{k} return {day}: golden collected {min(gd) if gd else '?'} days before, picked collected {min(sd) if sd else '?'} days before")
gds = [o[2] for o in out if o[2] is not None]; sds = [o[3] for o in out if o[3] is not None]
print(f"wrong MD06: {len(out)} | golden collection age days: {sorted(gds)} | picked: {sorted(sds)}")
