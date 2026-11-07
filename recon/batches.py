"""Evidence-driven batch matching. No labels are read and balances are never mutated."""
from collections import defaultdict
import csv
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from types import MappingProxyType
import re
import unicodedata

D = Decimal
ZERO = D('0')
CENT = D('.01')
# Return narratives (normalised, no spaces): Spanish/English/French plus German SEPA
# R-transactions ('RUECKLASTSCHRIFT AM04 MREF+...').
RETURN_WORDS = ('DEVOL', 'RETURN', 'IMPAY', 'REJECT', 'RUECKLASTSCHRIFT', 'RUCKLASTSCHRIFT')

def norm(value):
    return re.sub('[^A-Z0-9]', '', unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().upper())

def money(value):
    return D(str(value or '0'))

def _days(a, b):
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (ValueError, TypeError):
        return 99999

def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)

def next_business_day(iso):
    """Roll a calendar date to the next TARGET business day (POLICIES §2 FX note uses
    TARGET days; bank statements carry no bookings on 1 Jan, Good Friday, Easter
    Monday, 1 May, 25/26 Dec or weekends)."""
    try:
        day = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ''
    while True:
        easter = _easter(day.year)
        if (day.weekday() < 5 and (day.month, day.day) not in ((1, 1), (5, 1), (12, 25), (12, 26))
                and day not in (easter - timedelta(days=2), easter + timedelta(days=1))):
            return day.isoformat()
        day += timedelta(days=1)

def unique_small_subset(amounts, target, count, max_candidates=256, max_checks=100000):
    """Prove a unique 2–4 member subset in integer cents; fail closed on budget.

    Pair sums avoid exponential search for the small beneficiary groups explicitly
    named in bank text. The bound is deterministic, independent of machine speed.
    """
    if count not in (2, 3, 4) or len(amounts) > max_candidates:
        return None
    pairs = defaultdict(list)
    for i, amount in enumerate(amounts):
        for j in range(i + 1, len(amounts)):
            total = amount + amounts[j]
            if total <= target:
                pairs[total].append((i, j))
    solutions = set()
    checks = 0
    if count == 2:
        candidates = pairs.get(target, ())
        return candidates[0] if len(candidates) == 1 else None
    for i, amount in enumerate(amounts):
        if count == 3:
            complements = pairs.get(target - amount, ())
            for j, k in complements:
                checks += 1
                if checks > max_checks:
                    return None
                if i < j:
                    solutions.add((i, j, k))
                    if len(solutions) > 1:
                        return None
        else:
            for j in range(i + 1, len(amounts)):
                for k, l in pairs.get(target - amount - amounts[j], ()):
                    checks += 1
                    if checks > max_checks:
                        return None
                    if j < k:
                        solutions.add((i, j, k, l))
                        if len(solutions) > 1:
                            return None
    return next(iter(solutions)) if len(solutions) == 1 else None

class BatchMatcher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.groups = []
        self.mixed_run_groups = defaultdict(list)
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
        mixed_seen = set()
        for folder, idcol, datecol in [('direct_debit_batches','batch_id','collection_date'),
                                     ('card_settlements','settlement_id','settlement_date'),
                                     ('cheque_deposits','deposit_id','deposit_date'),
                                     ('payment_runs','run_id','execution_date')]:
            rows_by_group = defaultdict(list)
            mixed_run_rows = defaultdict(list)
            for root in dirs:
                for path in sorted((root / folder).glob('*.csv')):
                    if path.name.startswith('._'):
                        continue
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        mixed_occurrences = defaultdict(int)
                        for row in csv.DictReader(stream):
                            # Snapshot directories may overlap (sample is a subset of TRAIN).
                            # Retain a compact digest rather than a second full row
                            # tuple for every historical detail. Preserve field order
                            # exactly as the previous tuple(row.items()) identity did.
                            unique = hashlib.sha256(json.dumps(
                                [folder, list(row.items())], ensure_ascii=False,
                                separators=(',', ':')).encode('utf-8')).digest()
                            if folder == 'payment_runs' and row['run_type'] == 'aggregated':
                                # Preserve repeated detail fragments inside one source.
                                # Snapshot copies share each payload's occurrence index.
                                entity = path.name.split('_')[0]
                                payload = hashlib.sha256(json.dumps(
                                    [entity, sorted(row.items())], ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).digest()
                                mixed_occurrences[payload] += 1
                                occurrence = (payload, mixed_occurrences[payload])
                                if occurrence not in mixed_seen:
                                    mixed_seen.add(occurrence)
                                    detail = dict(row, _entity=entity, _source=str(path))
                                    # Run IDs repeat across entities and bank accounts.
                                    mixed_run_rows[(row['run_id'], row['debtor_account_id'],
                                                    entity, row['execution_date'])].append(detail)
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
            for (run_id, account, entity, execution_date), details in mixed_run_rows.items():
                if len({row['currency'] for row in details}) > 1:
                    self.mixed_run_groups[(norm(run_id), account)].append(details)
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
                         'confirming': folder == 'payment_runs' and first['run_type'] == 'confirming',
                         'mixed_aggregated': folder == 'payment_runs' and first['run_type'] == 'aggregated'
                                             and len({r['currency'] for r in rows}) > 1}
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

    def _resolve(self, row, kind, txn, reserved=None, require_exact=False):
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
        remaining = {}
        reserved = reserved or {}
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
            explicit = printed == number or any(len(token) >= 5 and printed.endswith(token) for token in re.findall(r'[0-9]+', row[number_field]))
            if require_exact and not explicit:
                # A return re-opens the printed collected invoice ('FV24-0217685' or
                # 'RECIBO 0217685'), never a substitute chosen by payer name.
                continue
            if printed == number:
                score += 8
            elif explicit:
                score += 6
            if item.get('party_id') in known_parties:
                score += 8
            names = [item.get(k,'') for k in ('customer_name','supplier_name','party_name')]
            if norm(person) and norm(person) in [norm(n) for n in names]:
                score += 8
            if item.get('entity') == row['_entity']:
                score += 2
            total = money(item.get('total_amount'))
            opened = money(item.get('open_amount', total)) - reserved.get(iid, ZERO)
            remaining[iid] = opened
            # Unreferenced rows cannot spend the same FIFO balance twice in one
            # proposed group. Explicit repeated document rows may aggregate parts.
            if reserved.get(iid, ZERO) and not explicit and (opened <= ZERO or amount > opened + CENT):
                continue
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
            tied = [v for v in tied if remaining[v[1]] == amount]
            if not tied:
                return None
            tied.sort(key=lambda v:(v[2].get('issue_date',''),v[1]))
            return tied[0][2]
        return candidates[0][2]

    def _allocate(self, group, txn, returned=False, allow_partial=False):
        out = {}
        reserved = defaultdict(lambda: ZERO)
        unresolved = 0
        for detail in group['rows']:
            item = self._resolve(detail, group['kind'], txn, reserved=reserved, require_exact=returned)
            if item is None:
                # The batch total already matched this line exactly: keep the rows we can
                # resolve instead of dropping every pair because one row is unknown.
                if not allow_partial:
                    return None
                unresolved += 1
                continue
            iid = item['item_id']
            amount = abs(money(detail.get('document_amount', detail.get('gross',detail.get('amount')))))
            reserved[iid] += amount
            if iid in out:
                out[iid]['amount'] = f"{money(out[iid]['amount']) + amount:.2f}"
            else:
                out[iid] = {'bank_txn_id':txn['bank_txn_id'], 'item_id':iid,
                            'amount':f'{amount:.2f}', 'category':'', 'exception':''}
        if not out or unresolved * 2 > len(group['rows']):
            return None
        # Classify the complete allocation, not each fragment: two explicitly
        # referenced partial detail rows can together fully settle one invoice.
        for iid, row in out.items():
            item = self.ctx.items[iid]
            amount = money(row['amount'])
            credit = 'C' in iid[:3]
            opened = money(item.get('open_amount', item['total_amount']))
            if returned:
                row['exception'] = 'RETURNED'
            elif opened <= ZERO and not credit:
                row['exception'] = 'DUPLICATE_PAYMENT'
            elif amount < opened - CENT and not credit:
                row['exception'] = 'PARTIAL'
            elif item['currency'] != txn['currency'] and not credit:
                row['exception'] = 'FX_DIFFERENCE'
        return list(out.values())

    def _confirming_subset(self, txn, text, candidate_ids):
        """Recover a small, explicitly referenced multi-beneficiary debit.

        Invoice due dates do not define confirming maturity. Use the bank's run
        reference, beneficiary count and exact net cash instead. Never emit a
        partial group, infer missing FX evidence, or reuse already settled items.
        """
        raw = ' '.join(str(txn.get(k, '')) for k in
                       ('text', 'ref1', 'ref2', 'end_to_end_id', 'remittance_info'))
        count_match = re.search(r'\b([2-4])\s+PROVEEDORES\b', raw.upper())
        if money(txn['amount']) >= 0 or not count_match:
            return None
        count = int(count_match[1])
        runs = defaultdict(list)
        for idx in sorted(candidate_ids):
            group = self.groups[idx]
            if not group['confirming'] or not group['ref'] or group['ref'] not in text:
                continue
            if group['account'] != txn['account_id'] or _days(txn['booking_date'], group['date']) > 120:
                continue
            if group['currency'] == txn['currency']:
                cash = abs(money(txn['amount']))
            elif txn.get('orig_currency') == group['currency'] and txn.get('orig_amount'):
                cash = abs(money(txn['orig_amount']))
            else:
                continue
            if ZERO < group['amount'] <= cash:
                runs[(group['ref'], group['account'], group['currency'], cash)].append(group)
        # Multiple explicit run references are not grounds for choosing one.
        if len(runs) != 1:
            return None
        key, groups = next(iter(runs.items()))
        cash = key[-1]
        amounts = [g['amount'] / CENT for g in groups]
        if cash / CENT != (cash / CENT).to_integral_value() or any(a != a.to_integral_value() for a in amounts):
            return None
        subset = unique_small_subset([int(a) for a in amounts], int(cash / CENT), count)
        if subset is None:
            return None
        selected = [groups[i] for i in subset]
        parties = {norm(r.get('beneficiary', '')) for g in selected for r in g['rows']}
        if '' in parties or len(parties) != count:
            return None
        rows = self._allocate({'kind': 'payment_runs', 'rows': [r for g in selected for r in g['rows']]}, txn)
        if not rows or any(r['exception'] == 'DUPLICATE_PAYMENT' or
                           money(r['amount']) > money(self.ctx.items[r['item_id']]['open_amount']) + CENT
                           for r in rows):
            return None
        return rows

    def _confirming_by_maturity(self, txn, text, candidate_ids):
        """POLICIES §5.3: a confirming debit clears the documents of its run.

        The bank text names the run and how many beneficiaries it pays on this
        date ('REMESA CF010024092529 9 PROVEEDORES'). TRAIN history: a confirming
        run debits each beneficiary transfer on its maturity, the latest due date
        of its invoices rolled to the next TARGET business day. The selection is
        proven by the beneficiary count and the exact cash; otherwise abstain.
        """
        raw = ' '.join(str(txn.get(k, '') or '') for k in
                       ('text', 'ref1', 'ref2', 'end_to_end_id', 'remittance_info')).upper()
        count_match = re.search(r'\b(\d{1,3})\s+PROVEEDORES\b', raw)
        amount = money(txn['amount'])
        if amount >= 0 or not count_match or int(count_match[1]) < 1:
            return None
        count = int(count_match[1])
        runs = defaultdict(list)
        for idx in sorted(candidate_ids):
            group = self.groups[idx]
            if not group['confirming'] or not group['ref'] or group['ref'] not in text:
                continue
            if group['account'] != txn['account_id'] or group['currency'] != txn['currency']:
                continue
            if _days(txn['booking_date'], group['date']) > 120:
                continue
            runs[group['ref']].append(group)
        if len(runs) != 1:
            return None
        due_today, overdue = [], []
        for group in next(iter(runs.values())):
            if group['amount'] <= ZERO:
                continue
            rows = self._allocate(group, txn)
            # A transfer whose documents are already settled was debited earlier.
            if not rows or any(r['exception'] == 'DUPLICATE_PAYMENT' or
                               money(r['amount']) > self.ctx.items[r['item_id']]['open_amount'] + CENT
                               for r in rows):
                continue
            dues = [self.ctx.items[r['item_id']].get('due_date', '') for r in rows
                    if 'C' not in r['item_id'][:3]]
            dues = [d for d in dues if d]
            if not dues:
                continue
            maturity = next_business_day(max(dues))
            if maturity == txn['booking_date']:
                due_today.append((group, rows))
            elif maturity and maturity < txn['booking_date']:
                overdue.append((group, rows))
        cash = abs(amount)
        total = lambda selection: sum((group['amount'] for group, _ in selection), ZERO)
        chosen = None
        if len(due_today) == count:
            if total(due_today) == cash:
                chosen = due_today
        elif len(due_today) > count:
            # Some transfers maturing today belong to another debit: drop at most 3.
            if len(due_today) - count <= 3:
                surplus = total(due_today) - cash
                drops = [set(id(g) for g, _ in drop)
                         for drop in combinations(due_today, len(due_today) - count)
                         if total(drop) == surplus]
                if len(drops) == 1:
                    chosen = [entry for entry in due_today if id(entry[0]) not in drops[0]]
        elif count - len(due_today) <= 2:
            # Overdue transfers are paid with the next debit of their run.
            rest = cash - total(due_today)
            adds = [list(add) for add in combinations(overdue, count - len(due_today))
                    if total(add) == rest]
            if len(adds) == 1:
                chosen = due_today + adds[0]
        if not chosen:
            return None
        beneficiaries = {norm(r.get('beneficiary', '')) for group, _ in chosen for r in group['rows']}
        rows = [row for _, group_rows in chosen for row in group_rows]
        if '' in beneficiaries or len(beneficiaries) != count or len({r['item_id'] for r in rows}) != len(rows):
            return None
        return rows

    def _mixed_currency_run(self, txn, text):
        from .batch_fx import mixed_run_cash_evidence, mixed_run_rows_valid, mixed_run_allocate, signature, run_reference_ids
        matches = []
        references = run_reference_ids(txn)
        if len(references) != 1:
            return None
        for (reference, account), groups in self.mixed_run_groups.items():
            if account != txn['account_id'] or reference not in references:
                continue
            for details in groups:
                cash_evidence = mixed_run_cash_evidence(self.ctx, txn, details)
                if not cash_evidence:
                    continue
                rows = mixed_run_allocate(self.ctx, txn, details)
                if rows and mixed_run_rows_valid(self.ctx, txn, rows, cash_evidence, details):
                    matches.append((rows, details, cash_evidence))
        if len(matches) != 1:
            return None
        rows, details, cash_evidence = matches[0]
        self.ctx._mixed_run_proposal_evidence = {
            'signature': signature(txn, rows),
            'details': tuple(MappingProxyType(dict(detail)) for detail in details),
            'cash': cash_evidence,
        }
        return rows

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
        mixed = self._mixed_currency_run(txn, text)
        if mixed:
            return mixed
        ranked = []
        for idx in candidates:
            group = self.groups[idx]
            # Raw amounts in different currencies must never be summed as cash.
            if group.get('mixed_aggregated'):
                continue
            reverse = amount * group['direction'] < 0
            if reverse and not any(word in text for word in RETURN_WORDS):
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
            result = self._allocate(self.groups[ranked[0][1]],txn,returned=ranked[0][2],
                                    allow_partial=not ranked[0][2])
            if result:
                return result
        confirming = (self._confirming_by_maturity(txn, text, candidates)
                      or self._confirming_subset(txn, text, candidates))
        if confirming:
            return confirming
        # Only explicit return text activates reverse-direction receipt matching.
        if amount < 0 and any(word in text for word in RETURN_WORDS):
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
                        # A return always follows its collection; a same-day receipt of the
                        # same mandate and amount is the next collection, not the returned one.
                        if detail_date >= txn['booking_date']:
                            continue
                        result = self._allocate({'kind':kind,'rows':[detail]},txn,returned=True)
                        if result:
                            batch = norm(detail.get('batch_id', detail.get('deposit_id', '')))
                            matches.append((detail_date,result,len(batch) >= 8 and batch in text))
            # A return quoting the original batch ('REM 02241004067') reverses a collection
            # of that batch; otherwise the latest earlier collection is the returned one.
            if any(named for _,_,named in matches):
                matches = [m for m in matches if m[2]]
            latest = max((d for d,_,_ in matches),default='')
            unique = {tuple((r['item_id'],r['amount']) for r in m): m for d,m,_ in matches if d == latest}
            if len(unique) == 1:
                return next(iter(unique.values()))
        return None
