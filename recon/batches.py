"""Evidence-driven batch matching. No labels are read and balances are never mutated."""
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import unicodedata

D = Decimal
ZERO = D('0')
CENT = D('.01')

def norm(value):
    return re.sub('[^A-Z0-9]', '', unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().upper())

def money(value):
    return D(str(value or '0'))

def _days(a, b):
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (ValueError, TypeError):
        return 99999

class BatchMatcher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.groups = []
        self.by_amount = defaultdict(list)
        self.by_ref = defaultdict(list)
        self.by_account = defaultdict(list)
        self.return_details = defaultdict(list)
        self.name_party = defaultdict(set)
        for mapping in (getattr(ctx, 'customers', {}), getattr(ctx, 'suppliers', {})):
            for pid, party in mapping.items():
                for key in ('name', 'trade_name'):
                    if party.get(key):
                        self.name_party[norm(party[key])].add(pid)
        dirs = [Path(ctx.phase_dir)] + [Path(p) for p in getattr(ctx, 'history_dirs', [])]
        seen = set()
        for folder, idcol, datecol in [('direct_debit_batches','batch_id','collection_date'),
                                     ('card_settlements','settlement_id','settlement_date'),
                                     ('cheque_deposits','deposit_id','deposit_date'),
                                     ('payment_runs','run_id','execution_date')]:
            rows_by_group = defaultdict(list)
            for root in dirs:
                for path in sorted((root / folder).glob('*.csv')):
                    if path.name.startswith('._'):
                        continue
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        for row in csv.DictReader(stream):
                            # Snapshot directories may overlap (sample is a subset of TRAIN).
                            unique = (folder, tuple(row.items()))
                            if unique in seen:
                                continue
                            seen.add(unique)
                            row['_entity'] = path.name.split('_')[0]
                            row['_source'] = str(path)
                            key = row[idcol]
                            if folder == 'payment_runs' and row['run_type'] == 'transfers':
                                key = row['end_to_end_id']
                            if folder == 'payment_runs' and row['run_type'] == 'confirming':
                                key = row['end_to_end_id']
                            rows_by_group[key].append(row)
            for key, rows in rows_by_group.items():
                first = rows[0]
                account = first.get('account_id', first.get('debtor_account_id'))
                currency = first['currency']
                if folder == 'card_settlements':
                    total = sum((money(r['net']) for r in rows), ZERO)
                elif folder == 'payment_runs':
                    # Document amounts already carry applied cash and credit signs.
                    total = sum((money(r['document_amount']) for r in rows), ZERO)
                else:
                    total = sum((money(r['amount']) for r in rows), ZERO)
                reference = first['run_id'] if folder == 'payment_runs' and first['run_type'] == 'confirming' else key
                when = key[1] if isinstance(key, tuple) and key[1] else first[datecol]
                group = {'kind':folder, 'ref':norm(reference), 'account':account,
                         'currency':currency, 'date':when, 'amount':total, 'rows':rows,
                         'direction':-1 if folder == 'payment_runs' else 1,
                         'confirming': folder == 'payment_runs' and first['run_type'] == 'confirming'}
                idx = len(self.groups)
                self.groups.append(group)
                self.by_ref[group['ref']].append(idx)
                self.by_amount[(account, currency, total)].append(idx)
                self.by_account[account].append(idx)
                if folder == 'direct_debit_batches':
                    for row in rows:
                        for field in ('end_to_end_id','mandate_id'):
                            self.return_details[norm(row[field])].append((folder,row))
                elif folder == 'cheque_deposits':
                    for row in rows:
                        self.return_details[norm(row['cheque_number'])].append((folder,row))

        self.ref_lengths = {len(key) for key in self.by_ref if key}
        self.return_lengths = {len(key) for key in self.return_details if key}

    def _resolve(self, row, kind, txn):
        number_field = {'payment_runs':'document_number','direct_debit_batches':'remittance_info',
                        'card_settlements':'order_number','cheque_deposits':'stub_reference'}[kind]
        number = norm(row[number_field])
        ids = list(self.ctx.by_number.get(number, []))
        side = 'AP' if kind == 'payment_runs' else 'AR'
        person = row.get('beneficiary', row.get('debtor_name', row.get('drawer','')))
        known_parties = self.name_party.get(norm(person), set())
        if not ids and known_parties:
            for pid in known_parties:
                ids.extend(self.ctx.by_party.get(pid, []))
        amount = abs(money(row.get('document_amount', row.get('gross', row.get('amount','0')))))
        candidates = []
        for iid in set(ids):
            item = self.ctx.items[iid]
            if item.get('side', iid[:2]) != side:
                continue
            if item.get('currency') != row['currency']:
                continue
            effective_date = row.get('collection_date',row.get('operation_date',row.get('deposit_date', '9999')))
            if txn:
                effective_date = min(effective_date,txn.get('booking_date','9999'))
            if item.get('issue_date','') > effective_date:
                continue
            if kind == 'payment_runs':
                is_credit = money(row['document_amount']) < 0 or row.get('document_type') == 'CREDIT_NOTE'
                if ('C' in iid[:3]) != is_credit:
                    continue
            # File entity scopes printed numbers, but payment factories intentionally
            # pay invoices for subsidiaries, so party evidence may override entity.
            score = 0
            printed = norm(item.get('document_number', item.get('supplier_invoice_number','')))
            if printed == number:
                score += 8
            elif any(len(token) >= 5 and printed.endswith(token) for token in re.findall(r'[0-9]+', row[number_field])):
                score += 6
            if item.get('party_id') in known_parties:
                score += 8
            names = [item.get(k,'') for k in ('customer_name','supplier_name','party_name')]
            if norm(person) and norm(person) in [norm(n) for n in names]:
                score += 8
            if item.get('entity') == row['_entity']:
                score += 2
            total = money(item.get('total_amount'))
            opened = money(item.get('open_amount', total))
            if abs(total-amount) <= CENT or abs(opened-amount) <= CENT:
                score += 4
            elif amount > max(total, opened) + CENT:
                continue
            if score >= 10:
                candidates.append((score, iid, item))
        candidates.sort(key=lambda v: (-v[0], v[1]))
        if not candidates:
            return None
        if len(candidates)>1 and candidates[0][0] == candidates[1][0]:
            tied = [v for v in candidates if v[0] == candidates[0][0]]
            # Documented FIFO applies only within the same known payer and exact amount.
            if len({v[2].get('party_id') for v in tied}) != 1 or not known_parties:
                return None
            tied = [v for v in tied if money(v[2].get('open_amount')) == amount]
            if not tied:
                return None
            tied.sort(key=lambda v:(v[2].get('issue_date',''),v[1]))
            return tied[0][2]
        return candidates[0][2]

    def _allocate(self, group, txn, returned=False):
        out = {}
        for detail in group['rows']:
            item = self._resolve(detail, group['kind'], txn)
            if item is None:
                return None
            iid = item['item_id']
            amount = abs(money(detail.get('document_amount', detail.get('gross',detail.get('amount')))))
            credit = 'C' in iid[:3]
            opened = money(item.get('open_amount', item['total_amount']))
            exception = ''
            if returned:
                exception = 'RETURNED'
            elif opened <= ZERO and not credit:
                exception = 'DUPLICATE_PAYMENT'
            elif amount < opened - CENT and not credit:
                exception = 'PARTIAL'
            elif item['currency'] != txn['currency'] and not credit:
                exception = 'FX_DIFFERENCE'
            if iid in out:
                out[iid]['amount'] = f"{money(out[iid]['amount']) + amount:.2f}"
            else:
                out[iid] = {'bank_txn_id':txn['bank_txn_id'], 'item_id':iid,
                            'amount':f'{amount:.2f}', 'category':'', 'exception':exception}
        return list(out.values())

    def reconcile(self, txn):
        amount = money(txn['amount'])
        text = norm(' '.join(str(txn.get(k,'') or '') for k in
                            ('text','ref1','ref2','end_to_end_id','remittance_info')))
        candidates = set()
        # Exact bank amount + account + currency is a strong batch key only when
        # its date agrees; it supports truncated/absent reference text.
        for currency, amt in [(txn['currency'],abs(amount)),
                              (txn.get('orig_currency'),abs(money(txn.get('orig_amount'))))]:
            if currency:
                candidates.update(self.by_amount.get((txn['account_id'], currency, amt), []))
        # Hash lookups avoid scanning all historical batch IDs per transaction.
        for length in self.ref_lengths:
            for start in range(len(text)-length+1):
                for idx in self.by_ref.get(text[start:start+length], []):
                    if self.groups[idx]['account'] == txn['account_id']:
                        candidates.add(idx)
        ranked = []
        for idx in candidates:
            group = self.groups[idx]
            reverse = amount * group['direction'] < 0
            if reverse and not any(word in text for word in ('DEVOL','RETURN','IMPAY','REJECT')):
                continue
            if reverse and group['date'] > txn['booking_date']:
                continue
            delta = _days(txn['booking_date'], group['date'])
            ref = group['ref'] in text
            bank_amount = abs(amount)
            if group['currency'] != txn['currency']:
                if txn.get('orig_currency') != group['currency'] or not txn.get('orig_amount'):
                    continue
                bank_amount = abs(money(txn['orig_amount']))
            if abs(bank_amount-group['amount']) > CENT:
                continue
            if delta > (120 if ref and (group['confirming'] or reverse) else 10 if ref else 3):
                continue
            ranked.append((int(ref)*100-delta,idx,reverse))
        ranked.sort(reverse=True)
        if ranked and not (len(ranked)>1 and ranked[0][0] == ranked[1][0]):
            result = self._allocate(self.groups[ranked[0][1]],txn,returned=ranked[0][2])
            if result:
                return result
        # Only explicit return text activates reverse-direction receipt matching.
        if amount < 0 and any(word in text for word in ('DEVOL','RETURN','IMPAY','REJECT')):
            matches = []
            references = {text[start:start+length] for length in self.return_lengths
                          for start in range(len(text)-length+1)}
            for reference in references:
                if reference in self.return_details:
                    for kind, detail in self.return_details[reference]:
                        if detail.get('account_id') != txn['account_id']:
                            continue
                        if detail['currency'] != txn['currency']:
                            continue
                        if abs(money(detail['amount'])-abs(amount)) > CENT:
                            continue
                        detail_date = detail.get('collection_date',detail.get('deposit_date',''))
                        if detail_date > txn['booking_date']:
                            continue
                        result = self._allocate({'kind':kind,'rows':[detail]},txn,returned=True)
                        if result:
                            matches.append((detail_date,result))
            latest = max((d for d,_ in matches),default='')
            unique = {tuple((r['item_id'],r['amount']) for r in m): m for d,m in matches if d == latest}
            if len(unique) == 1:
                return next(iter(unique.values()))
        return None
