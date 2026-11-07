"""MD06 returns whose collection sits in a single-row direct-debit batch (the r6 precedence bug fixed in dcb125e).

    python solution/validation/md06_single_row.py outputs/q2root [outputs/runs/r6_q2.csv.gz]

Data only (batch files + golden). For every golden MD06 return (segment ar_dd_returned):
  golden batch      batch row with the golden item's document number, latest collection before the return
  single-row        that batch has exactly one row
  bug trigger       a single-row batch with total == returned amount collected 0-3 days before the return exists
                    (the reverse batch-total path that ran before the MD06 21-day rule)
With a submission: how many of each group the solver got wrong.
"""
import csv, glob, gzip, os, re, sys, collections
from datetime import date
root = sys.argv[1]
sub_path = sys.argv[2] if len(sys.argv) > 2 else None
def rows(p):
    with gzip.open(p, 'rt', encoding='utf-8') as fh: yield from csv.DictReader(fh)
L = os.path.join(root, 'live')
bank = {t['bank_txn_id']: t for t in rows(L + '/bank_transactions.csv.gz')}
gold = collections.defaultdict(list)
for r in rows(L + '/golden_allocations.csv.gz'): gold[r['bank_txn_id']].append(r)
md06 = {}
for k, g in gold.items():
    if g[0]['segment'] != 'ar_dd_returned': continue
    t = bank[k]
    text = ' '.join(t.get(c) or '' for c in ('text', 'ref1', 'ref2', 'remittance_info')).upper()
    if re.search(r'(?<![A-Z0-9])MD06(?![A-Z0-9])', text): md06[k] = g
need = {r['item_id'] for g in md06.values() for r in g if r['item_id']}
docnum = {}
for p in glob.glob(os.path.join(root, '*', 'ar_invoices.csv.gz')) + [L + '/open_items_at_start.csv.gz']:
    for r in rows(p):
        if r['item_id'] in need: docnum[r['item_id']] = r['document_number']
size, total, by_doc, by_total = collections.Counter(), {}, collections.defaultdict(list), collections.defaultdict(list)
for f in glob.glob(os.path.join(root, '*', 'direct_debit_batches', '*.csv')):
    for r in csv.DictReader(open(f, encoding='utf-8')):
        size[r['batch_id']] += 1
        total[r['batch_id']] = (r['collection_date'], round(float(r['batch_total']), 2), r['account_id'])
        by_doc[r['remittance_info'].strip()].append((r['collection_date'], r['batch_id']))
for b, (d, tot, acc) in total.items():
    if size[b] == 1: by_total[tot].append((d, b, acc))
sub = collections.defaultdict(set)
if sub_path:
    for r in rows(sub_path):
        if r['item_id']: sub[r['bank_txn_id']].add((r['item_id'], r['exception']))
st = collections.Counter()
for k, g in md06.items():
    t = bank[k]; day = t['booking_date']; amt = round(abs(float(t['amount'])), 2)
    gi = {r['item_id'] for r in g if r['item_id']}
    cands = [c for i in gi for c in by_doc.get(docnum.get(i, '#'), []) if c[0] < day]
    gb = max(cands)[1] if cands else None
    single = gb is not None and size[gb] == 1
    trig = any(acc == t['account_id'] and 0 <= (date.fromisoformat(day) - date.fromisoformat(d)).days <= 3
               for d, b, acc in by_total.get(amt, []))
    wrong = sub_path and sub[k] != {(i, 'RETURNED') for i in gi}
    st['MD06 returns'] += 1
    st['golden batch not found'] += gb is None
    st['golden collection in single-row batch'] += single
    st['bug trigger (single-row same-total batch 0-3d before, same account)'] += trig
    if sub_path:
        st['solver wrong (all MD06)'] += bool(wrong)
        st['solver wrong & golden single-row'] += bool(wrong and single)
        st['solver wrong & bug trigger'] += bool(wrong and trig)
for k, v in st.items(): print(f'{k}: {v}')
