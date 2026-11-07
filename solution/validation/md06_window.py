"""MD06 returns: how many same-mandate, same-amount collections fall in the 21-75 day window, and how often
'latest' (r6), 'oldest' or 'abstain when ambiguous' would pick the golden invoice. Uses batch files + golden."""
import csv, glob, gzip, os, re, sys, collections
from datetime import date
root = sys.argv[1]
def rows(p):
    with gzip.open(p, 'rt', encoding='utf-8') as fh: yield from csv.DictReader(fh)
L = os.path.join(root, 'live')
bank = {t['bank_txn_id']: t for t in rows(L + '/bank_transactions.csv.gz')}
gold = collections.defaultdict(list)
for r in rows(L + '/golden_allocations.csv.gz'): gold[r['bank_txn_id']].append(r)
# collections (mandate, amount) -> [(date, batch_id, end_to_end)] from all batch files (train + live)
coll = collections.defaultdict(list)
for f in glob.glob(os.path.join(root, '*', 'direct_debit_batches', '*.csv')):
    for r in csv.DictReader(open(f, encoding='utf-8')):
        coll[(re.sub(r'[^A-Z0-9]', '', r['mandate_id'].upper()), round(float(r['amount']), 2))].append(r['collection_date'])
# golden collection date of the returned item (from golden: item allocated in a DD batch line before the return)
item_dates = collections.defaultdict(list)
for k, g in gold.items():
    if g[0]['segment'] == 'ar_dd_batch':
        for r in g: item_dates[r['item_id']].append(bank[k]['booking_date'])
stats = collections.Counter()
for k, g in gold.items():
    if g[0]['segment'] != 'ar_dd_returned': continue
    t = bank[k]; text = (t['text'] + ' ' + t['remittance_info'] + ' ' + t['ref1']).upper()
    if not re.search(r'(?<![A-Z0-9])MD06(?![A-Z0-9])', text): continue
    m = re.search(r'VTM-?[A-Z]{2}-?\d+', text)
    if not m: stats['no mandate'] += 1; continue
    key = (re.sub(r'[^A-Z0-9]', '', m.group(0)), round(abs(float(t['amount'])), 2))
    day = date.fromisoformat(t['booking_date'])
    win = sorted({d for d in coll.get(key, []) if 21 <= (day - date.fromisoformat(d)).days <= 75})
    stats[f'candidates in 21-75d: {min(len(win), 3)}'] += 1
    stats['total'] += 1
    # golden collection date = the candidate whose age matches the golden item's collection (approx: nearest)
    gid = g[0]['item_id']
    gd = [d for d in item_dates.get(gid, []) if d < t['booking_date']]
    if len(win) >= 2:
        stats['ambiguous'] += 1
        if gd:
            truth = max(gd)
            stats['ambiguous: latest right'] += win[-1] == truth
            stats['ambiguous: oldest right'] += win[0] == truth
print(dict(stats))
