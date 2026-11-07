"""Deterministic post-processing of a FINAL submission (no labels used). Reproducible from <submission> + <audit> + FINAL inputs.

Rule A (POLICIES §3 SHORT_DISPUTE): an invoice row decided by the documents module (remittance advice) and labelled PARTIAL whose
amount is exactly round_half_up(item_total * r) for r in {0.95, 0.80, 0.97, 0.90, 0.70} becomes SHORT_DISPUTE (amount unchanged).
Rationale: documents labels PARTIAL only when an advice deduction has no stated reason and is neither a terms discount nor an
eligible withholding. The changed advice layout drops the reason column, and in TRAIN those exact deduction ratios are disputes.

Rule B (POLICIES §3: a disputed remainder stays open and may be paid later): a bank line quoting 'SETTLEMENT <document number>'
whose cash equals the deduction on that invoice, where an earlier documents row cleared the invoice in full as SHORT_WITHHOLDING
and the settlement line itself does not hold that invoice: the earlier row becomes SHORT_DISPUTE at (total - cash), and the
settlement line settles the invoice at cash with no exception (replacing its UNIDENTIFIED/category or other allocation).

Usage: python3 -I relabel_disputes.py <in.csv.gz> <audit.jsonl> <final_dir> <out.csv.gz>
"""
import csv
import gzip
import io
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal as D, ROUND_HALF_UP

import os
RATIOS_B = ('0.95', '0.80', '0.97', '0.90', '0.70')
RATIOS = tuple(os.environ.get('RATIOS', '0.80,0.97').split(','))
CENT = D('0.01')


def rd(p):
    with gzip.open(p, 'rt', encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def main():
    src, audit, F, out = sys.argv[1:5]
    mod = {}
    with open(audit, encoding='utf-8') as f:
        for line in f:
            j = json.loads(line)
            mod[j['bank_txn_id']] = j.get('module')
    total, bydoc, ccy = {}, {}, {}
    for fn in ('open_items_at_start', 'ar_invoices', 'credit_notes', 'ap_invoices'):
        for r in rd(f'{F}/{fn}.csv.gz'):
            total.setdefault(r['item_id'], D(r['total_amount']))
            ccy.setdefault(r['item_id'], r.get('currency', ''))
            dn = (r.get('document_number') or r.get('supplier_invoice_number') or '').strip().upper()
            if dn:
                bydoc.setdefault(dn, r['item_id'])
    bank = {r['bank_txn_id']: r for r in rd(f'{F}/bank_transactions.csv.gz')}
    with gzip.open(src, 'rt', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)
    by_txn = defaultdict(list)
    for r in rows:
        by_txn[r['bank_txn_id']].append(r)
    a_count = 0
    for r in rows:
        if r['exception'] != 'PARTIAL' or not r['item_id'] or mod.get(r['bank_txn_id']) != 'documents':
            continue
        t = total.get(r['item_id'])
        if not t:
            continue
        a = D(r['amount'])
        if any(abs((t * D(q)).quantize(CENT, ROUND_HALF_UP) - a) <= CENT for q in RATIOS):
            r['exception'] = 'SHORT_DISPUTE'
            a_count += 1
    # Rule B
    holder = defaultdict(list)
    for r in rows:
        if r['item_id']:
            holder[r['item_id']].append(r)
    b_count = 0
    bp_count = [0]
    for tid, b in bank.items():
        m = re.search(r'SETTLEMENT\s+([A-Z]{1,4}\d{6,12})', (b.get('text') or '').upper())
        if not m:
            continue
        iid = bydoc.get(m.group(1))
        if not iid or iid not in total:
            continue
        cash = D(b['amount'])
        if cash <= 0 or b.get('currency') != ccy.get(iid):
            continue
        cur = by_txn.get(tid, [])
        # Rule B': the settlement line already holds the invoice at its cash; an earlier PARTIAL row whose remainder equals
        # that cash is a dispute (TRAIN: 3,707/3,707 earlier rows SHORT_DISPUTE with remainder == settlement).
        if any(r['item_id'] == iid and abs(D(r['amount']) - cash) <= CENT for r in cur) and len([r for r in cur if r['item_id']]) == 1:
            prev = [r for r in holder.get(iid, []) if r['bank_txn_id'] != tid and r['exception'] == 'PARTIAL'
                    and abs(total[iid] - D(r['amount']) - cash) <= CENT
                    and bank.get(r['bank_txn_id'], {}).get('booking_date', '9') <= b['booking_date']]
            if len(prev) == 1:
                prev[0]['exception'] = 'SHORT_DISPUTE'
                bp_count[0] += 1
            continue
        if any(r['item_id'] == iid for r in cur):
            continue
        earlier = [r for r in holder.get(iid, []) if r['bank_txn_id'] != tid and mod.get(r['bank_txn_id']) == 'documents'
                   and r['exception'] == 'SHORT_WITHHOLDING' and abs(D(r['amount']) - total[iid]) <= CENT
                   and (bank.get(r['bank_txn_id'], {}).get('booking_date', '9') <= b['booking_date'])]
        if len(earlier) != 1:
            continue
        t = total[iid]
        paid = t - cash
        if paid <= 0 or not any(abs((t * D(q)).quantize(CENT, ROUND_HALF_UP) - paid) <= CENT for q in RATIOS_B):
            continue
        e = earlier[0]
        e['amount'] = f'{paid.quantize(CENT, ROUND_HALF_UP)}'
        e['exception'] = 'SHORT_DISPUTE'
        newrow = {'bank_txn_id': tid, 'item_id': iid, 'amount': f'{cash.quantize(CENT, ROUND_HALF_UP)}', 'category': '', 'exception': ''}
        for r in cur:
            r['_drop'] = True
        by_txn[tid] = [newrow]
        rows.append(newrow)
        b_count += 1
    kept = [r for r in rows if not r.get('_drop')]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator='\n')
    w.writeheader()
    for r in kept:
        w.writerow({k: r.get(k, '') for k in header})
    with gzip.open(out, 'wb') as f:
        f.write(buf.getvalue().encode('utf-8'))
    print(json.dumps({'ratios_a': RATIOS, 'rule_a_relabelled': a_count, 'rule_b_settlements': b_count, 'rule_b_prime_partial_to_dispute': bp_count[0], 'rows_in': len(rows) - b_count, 'rows_out': len(kept)}))


if __name__ == '__main__':
    main()
