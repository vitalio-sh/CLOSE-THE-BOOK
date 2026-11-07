"""Unique opposite-cash pairs supported by explicit bank-error reversal text."""
from collections import defaultdict
from datetime import date
import re
from .matching import dec, words


def bank_error_pairs(txns):
    index = defaultdict(list)
    for t in txns:
        key = (t.get('entity'),t.get('account_id'),t.get('currency'),dec(t.get('amount')))
        index[key].append(t)
    proposed = []
    for t in txns:
        text = words(' '.join(str(t.get(k) or '') for k in ('text','ref1','ref2','remittance_info')))
        if not re.search(r'ANULACION OPERACION INDEBIDA|BANK ERROR REVERSAL|ERROR REVERSAL|REVERSAL OF ERRONEOUS',text):
            continue
        amount=dec(t.get('amount'))
        if not amount:
            continue
        candidates=[]
        for other in index.get((t.get('entity'),t.get('account_id'),t.get('currency'),-amount),()):
            if (other['booking_date'],other['bank_txn_id']) >= (t['booking_date'],t['bank_txn_id']):
                continue
            days=(date.fromisoformat(t['booking_date'])-date.fromisoformat(other['booking_date'])).days
            if 0 <= days <= 7:
                candidates.append(other['bank_txn_id'])
        if len(candidates)==1:
            proposed.append((t['bank_txn_id'],candidates[0]))
    counts=defaultdict(int)
    for pair in proposed:
        for tid in pair:counts[tid]+=1
    return {tid for pair in proposed if all(counts[x]==1 for x in pair) for tid in pair}
