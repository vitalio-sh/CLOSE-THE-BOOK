"""Deterministic FINAL CSV validator (FORMAT.md rules + financial invariants). Read-only; never uploads.

Usage: python3 -I validate_final_csv.py <submission.csv.gz> <final_dir> <masters_final_dir> [<open_items/invoices root>]
Prints a JSON report; exit code 0 only if every hard check passes.
"""
import csv
import gzip
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

EXCEPTIONS = {'', 'PARTIAL', 'OVERPAYMENT', 'SHORT_DISCOUNT', 'SHORT_BANK_CHARGES', 'SHORT_WITHHOLDING', 'SHORT_DISPUTE',
              'DUPLICATE_PAYMENT', 'UNIDENTIFIED', 'RETURNED', 'FX_DIFFERENCE'}
ITEM_RE = re.compile(r'^(ARI|ARC|API|APC)-\d{4}-\d{7}$')


def read_gz_csv(path):
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def main():
    sub, final_dir, masters = sys.argv[1], sys.argv[2], sys.argv[3]
    raw = open(sub, 'rb').read()
    data = gzip.decompress(raw) if sub.endswith('.gz') else raw
    rep = {'file': sub, 'sha256_file': hashlib.sha256(raw).hexdigest(), 'sha256_uncompressed': hashlib.sha256(data).hexdigest(),
           'bytes_compressed': len(raw), 'bytes_uncompressed': len(data)}
    hard = []
    text = data.decode('utf-8-sig')
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader)]
    rep['header'] = header
    if sorted(header) != sorted(['bank_txn_id', 'item_id', 'amount', 'category', 'exception']):
        hard.append('header is not exactly the five columns')
    idx = {h: i for i, h in enumerate(header)}
    rows = [r for r in reader]
    rep['rows'] = len(rows)
    if len(data) > 60 * 1024 * 1024:
        hard.append('uncompressed size above 60 MB')
    if len(rows) > 3_000_000:
        hard.append('more than 3,000,000 rows')
    cats = {r['code'] if 'code' in r else list(r.values())[0] for r in csv.DictReader(open(masters + '/categories.csv', encoding='utf-8'))}
    rep['categories_master'] = len(cats)
    phase_ids = [r['bank_txn_id'] for r in read_gz_csv(final_dir + '/bank_transactions.csv.gz')]
    phase = set(phase_ids)
    rep['phase_txns'] = len(phase)
    seen_pairs, noitem, itemrow = set(), Counter(), Counter()
    bad = Counter()
    exc_mix, cat_mix = Counter(), Counter()
    item_alloc = defaultdict(Decimal)
    for n, r in enumerate(rows, start=2):
        if len(r) != len(header):
            bad['wrong_field_count'] += 1
            continue
        t, it, am, ca, ex = (r[idx[k]].strip() for k in ('bank_txn_id', 'item_id', 'amount', 'category', 'exception'))
        if t not in phase:
            bad['txn_not_in_phase'] += 1
        if ex not in EXCEPTIONS:
            bad['bad_exception'] += 1
        exc_mix[ex] += 1
        if it:
            if not ITEM_RE.match(it):
                bad['bad_item_id_shape'] += 1
            if (t, it) in seen_pairs:
                bad['duplicate_pair'] += 1
            seen_pairs.add((t, it))
            itemrow[t] += 1
            if ca:
                bad['category_on_item_row'] += 1
            try:
                a = Decimal(am)
                if a < 0:
                    bad['negative_amount'] += 1
                if a.as_tuple().exponent < -2:
                    bad['more_than_2dp'] += 1
                item_alloc[it] += a
            except (InvalidOperation, ValueError):
                bad['amount_not_numeric'] += 1
        else:
            noitem[t] += 1
            if am:
                bad['amount_on_noitem_row'] += 1
            if ca not in cats:
                bad['bad_or_missing_category'] += 1
            cat_mix[ca] += 1
    both = sum(1 for t in noitem if t in itemrow)
    multi_noitem = sum(1 for t, c in noitem.items() if c > 1)
    covered = set(noitem) | set(itemrow)
    missing = phase - covered
    rep.update({'bad': dict(bad), 'txns_with_item_and_category': both, 'txns_with_multiple_noitem_rows': multi_noitem,
                'covered_txns': len(covered), 'missing_txns': len(missing), 'missing_sample': sorted(missing)[:5],
                'item_pairs': len(seen_pairs), 'noitem_rows': sum(noitem.values()), 'exception_mix': dict(exc_mix.most_common()),
                'category_mix': dict(cat_mix.most_common())})
    for k in ('txn_not_in_phase', 'bad_exception', 'duplicate_pair', 'category_on_item_row', 'negative_amount',
              'amount_not_numeric', 'amount_on_noitem_row', 'bad_or_missing_category', 'wrong_field_count', 'bad_item_id_shape'):
        if bad.get(k):
            hard.append(f'{k}: {bad[k]}')
    if both:
        hard.append(f'{both} txns mix item rows and a category row')
    if multi_noitem:
        hard.append(f'{multi_noitem} txns have more than one no-item row')
    if missing:
        hard.append(f'{len(missing)} phase txns not covered')
    # financial invariant: no item allocated beyond its open/total amount across the phase (soft check)
    if len(sys.argv) > 4:
        root = sys.argv[4]
        totals = {}
        for fn, key in (('open_items_at_start.csv.gz', None), ('ar_invoices.csv.gz', None), ('ap_invoices.csv.gz', None),
                        ('credit_notes.csv.gz', None)):
            try:
                for r in read_gz_csv(root + '/' + fn):
                    iid = r.get('item_id') or r.get('invoice_id') or r.get('id')
                    amt = r.get('open_amount') or r.get('total_amount') or r.get('amount')
                    if iid and amt:
                        try:
                            totals.setdefault(iid, Decimal(amt).copy_abs())
                        except InvalidOperation:
                            pass
            except FileNotFoundError:
                pass
        unknown = [i for i in item_alloc if i not in totals]
        over = [i for i, a in item_alloc.items() if i in totals and a > totals[i] * Decimal('1.005') + Decimal('0.01')]
        rep['items_known'] = len(totals)
        rep['allocated_items_unknown_to_final_inputs'] = len(unknown)
        rep['unknown_sample'] = unknown[:5]
        rep['items_over_allocated_soft'] = len(over)
        rep['over_sample'] = over[:5]
    rep['hard_failures'] = hard
    rep['valid'] = not hard
    print(json.dumps(rep, indent=1))
    return 0 if not hard else 1


if __name__ == '__main__':
    sys.exit(main())
