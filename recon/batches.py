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
REJECTED_TRANSFER = re.compile(r'(?:RETURN(?:ED)?|DEVOLUCION TRANSFERENCIA|RUECKUEBERWEISUNG|RETOUR|REJECT(?:ED)?)'
                               r'\W+(?:[A-Z]{2}\d{2})\b')

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

def _printed_key(text):
    """Batch files print one invoice as 'FV24-0417472' or 'RECIBO 0417472': compare the
    trailing 5+ digit run when there is one, else the whole normalised token."""
    token = norm(text)
    tail = re.search(r'(\d{5,})$', token)
    return tail.group(1) if tail else token

def _same_printed(a, b):
    return a == b or (a.isdigit() and b.isdigit() and (a.endswith(b) or b.endswith(a)))

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
        self._ap_by_amount = None
        self._dd_by_amount = None
        self.groups = []
        self.mixed_run_groups = defaultdict(list)
        self.by_amount = defaultdict(list)
        self.by_ref = defaultdict(list)
        self.by_account = defaultdict(list)
        self.return_details = defaultdict(list)
        self.card_orders = defaultdict(list)
        self.represented = defaultdict(list)
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
                            if folder == 'payment_runs':
                                # Run and transfer IDs repeat across entities and debtor
                                # accounts (PR010024101092-0118 exists for PT01 and UK01):
                                # a group is one transfer of one paying account.
                                key = (key, row['debtor_account_id'])
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
                if folder == 'payment_runs':
                    reference = first['run_id'] if first['run_type'] == 'confirming' else key[0]
                else:
                    reference = key
                when = first[datecol]
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
                        if row.get('re_presentation') == 'Y':
                            self.represented[norm(row['mandate_id'])].append(row)
                elif folder == 'cheque_deposits':
                    for row in rows:
                        self.return_details[norm(row['cheque_number'])].append((folder,row))
                elif folder == 'card_settlements':
                    for row in rows:
                        last4 = re.sub(r'[^0-9]', '', row.get('card', ''))[-4:]
                        if last4:
                            self.card_orders[(row['_entity'], norm(row['merchant_id']), last4)].append(row)

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
            if kind == 'payment_runs' and not require_exact:
                # A printed number reused across years: settled (zero-balance) documents
                # cannot take a partial transfer row; the single tied document whose open
                # balance covers the row is the one paid (factor transfers, POLICIES 5.3).
                able = [v for v in tied if remaining[v[1]] >= amount - CENT]
                if len(able) == 1 and all(remaining[v[1]] <= ZERO for v in tied if v is not able[0]):
                    return able[0][2]
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
        resolved = []
        pending = []
        for detail in group['rows']:
            item = self._resolve(detail, group['kind'], txn, reserved=reserved, require_exact=returned)
            if item is None:
                pending.append(detail)
                continue
            resolved.append((detail, item))
            amount = abs(money(detail.get('document_amount', detail.get('gross',detail.get('amount')))))
            reserved[item['item_id']] += amount
        if pending and group['kind'] == 'payment_runs' and not returned and resolved:
            # POLICIES §5.3: one transfer pays one supplier. A printed number shared
            # by several suppliers (factor-named beneficiary, partial amount) is
            # resolved within the supplier that owns the transfer's other documents.
            transfers = {d.get('end_to_end_id') for d in group['rows']}
            parties = {item.get('party_id') for _, item in resolved}
            if len(transfers) == 1 and len(parties) == 1 and next(iter(parties)):
                party = next(iter(parties))
                still = []
                for detail in pending:
                    value = money(detail['document_amount'])
                    credit = value < 0 or detail.get('document_type') == 'CREDIT_NOTE'
                    number = norm(detail['document_number'])
                    found = [self.ctx.items[iid] for iid in set(self.ctx.by_number.get(number, []))
                             if self.ctx.items[iid].get('party_id') == party
                             and self.ctx.items[iid].get('side', iid[:2]) == 'AP'
                             and ('C' in iid[:3]) == credit
                             and self.ctx.items[iid].get('currency') == detail['currency']
                             and self.ctx.items[iid].get('issue_date', '') <= txn.get('booking_date', '9999')
                             and money(self.ctx.items[iid].get('open_amount', 0)) - reserved.get(iid, ZERO) >= abs(value)
                             and norm(self.ctx.items[iid].get('document_number', '')) == number]
                    if len(found) == 1:
                        resolved.append((detail, found[0]))
                        reserved[found[0]['item_id']] += abs(value)
                    else:
                        still.append(detail)
                pending = still
        if pending:
            # The batch total already matched this line exactly: keep the rows we can
            # resolve instead of dropping every pair because one row is unknown.
            if not allow_partial:
                return None
            unresolved = len(pending)
        order = {id(detail): n for n, detail in enumerate(group['rows'])}
        resolved.sort(key=lambda pair: order[id(pair[0])])
        for detail, item in resolved:
            iid = item['item_id']
            amount = abs(money(detail.get('document_amount', detail.get('gross',detail.get('amount')))))
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

    def _confirming_without_run(self, txn, text, candidate_ids):
        """One-beneficiary confirming debit whose run file is not available.

        POLICIES §5.3 + the maturity rule above: the debit pays on the invoice's
        due date (next TARGET business day). Without the run, accept only a
        single open supplier invoice of the paying entity (or, for the ES00
        payment factory, of ES01/ES02/PT01 — POLICIES §5.2) with exactly the
        debited amount and that maturity; any tie abstains.
        """
        raw = ' '.join(str(txn.get(k, '') or '') for k in
                       ('text', 'ref1', 'ref2', 'end_to_end_id', 'remittance_info')).upper()
        amount = money(txn['amount'])
        if amount >= 0 or not re.search(r'\bCONFIRMING\b', raw) or not re.search(r'\bREMESA\s+\S+\s+1\s+PROVEEDORES\b', raw):
            return None
        if any(self.groups[idx]['confirming'] and self.groups[idx]['account'] == txn['account_id']
               and self.groups[idx]['ref'] and self.groups[idx]['ref'] in text for idx in candidate_ids):
            return None
        if self._ap_by_amount is None:
            self._ap_by_amount = defaultdict(list)
            for iid, item in self.ctx.items.items():
                if item.get('side') == 'AP' and item.get('kind', 'credit' if 'C' in iid[:3] else 'invoice') == 'invoice':
                    for value in {money(item.get('total_amount')), money(item.get('open_amount'))}:
                        self._ap_by_amount[(item.get('currency'), value)].append(iid)
        cash = abs(amount)
        entities = {txn.get('entity')} | ({'ES01', 'ES02', 'PT01'} if txn.get('entity') == 'ES00' else set())
        matches = set()
        for iid in self._ap_by_amount.get((txn['currency'], cash), ()):
            item = self.ctx.items[iid]
            if (item.get('entity') in entities and item.get('open_amount') == cash
                    and item.get('issue_date', '') <= txn['booking_date']
                    and next_business_day(item.get('due_date', '')) == txn['booking_date']):
                matches.add(iid)
        if len(matches) != 1:
            return None
        iid = matches.pop()
        return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': iid, 'amount': f'{cash:.2f}',
                 'category': '', 'exception': ''}]

    def _returned_transfer(self, txn, text):
        """POLICIES §3 RETURNED: our transfer rejected by the beneficiary bank.

        The credit names the reason code and the beneficiary ('DEVOLUCION
        TRANSFERENCIA AC04 | FRANCO LOGISTICA S.A.', 'RETURN MD07 BP6 PUMPS GMBH').
        Every item of the returned operation gets a RETURNED row for the amount
        originally allocated (§2). Accept only one earlier debit of the same
        account whose AP allocations total exactly the returned cash and whose
        single supplier is named in the text.
        """
        raw = ' '.join(str(txn.get(k, '') or '') for k in ('text', 'counterparty_name')).upper()
        amount = money(txn['amount'])
        if amount <= 0 or not re.search(r'\b(?:DEVOLUCION TRANSFERENCIA|RETURN)\s+[A-Z]{2}\d{2}\b', raw):
            return None
        history = getattr(self.ctx, 'history', None)
        if not history:
            return None
        payments = defaultdict(list)
        returned = set()
        for iid, entries in history.items():
            item = self.ctx.items.get(iid)
            if not item or item.get('side') != 'AP':
                continue
            for entry in entries:
                if entry.get('exception') == 'RETURNED':
                    returned.add((iid, entry.get('booking_date', '')))
                    continue
                if (entry.get('account_id') == txn['account_id'] and entry.get('booking_date', '') < txn['booking_date']
                        and entry.get('exception') != 'DUPLICATE_PAYMENT'):
                    payments[entry['bank_txn_id']].append((iid, entry))
        named = norm(raw)
        matches = []
        for bank_txn_id, entries in payments.items():
            if sum((money(e['amount']) * (-1 if 'C' in iid[:3] else 1) for iid, e in entries), ZERO) != amount:
                continue
            parties = {self.ctx.items[iid].get('party_id') for iid, _ in entries}
            if len(parties) != 1:
                continue
            party = self.ctx.suppliers.get(next(iter(parties)), {}) if hasattr(self.ctx, 'suppliers') else {}
            names = {norm(party.get(k)) for k in ('name', 'trade_name')}
            names.update(norm(self.ctx.items[iid].get(k)) for iid, _ in entries for k in ('supplier_name', 'party_name'))
            if not any(len(n) >= 6 and n in named for n in names if n):
                continue
            paid = max(e.get('booking_date', '') for _, e in entries)
            if any(iid == r_iid and day >= paid for iid, _ in entries for r_iid, day in returned):
                continue
            matches.append(entries)
        if len(matches) != 1:
            return None
        return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': iid, 'amount': f"{money(e['amount']):.2f}",
                 'category': '', 'exception': 'RETURNED'} for iid, e in matches[0]]

    def _supplier_direct_debit(self, txn):
        """A supplier's SEPA direct debit charged to us ('PM8 MANTENIMIENT | Recibos varios').

        The bank prints the creditor name cut at 16 characters (POLICIES §5.5) and
        a 12-digit mandate reference. The ES00 payment factory is charged for
        ES01/ES02/PT01 invoices (§5.2). Accept only one open direct-debit invoice
        (payment_method 'dd') of exactly the debited amount whose supplier name
        starts with the printed name; otherwise abstain.
        """
        amount = money(txn['amount'])
        text = str(txn.get('text') or '')
        if amount >= 0 or ' | ' not in text or not re.fullmatch(r'\d{12}', str(txn.get('ref2') or '').strip()):
            return None
        printed = norm(text.split(' | ')[0])
        if len(printed) < 8:
            return None
        if self._dd_by_amount is None:
            self._dd_by_amount = defaultdict(list)
            for iid, item in self.ctx.items.items():
                if item.get('side') == 'AP' and 'C' not in iid[:3] and item.get('payment_method') == 'dd':
                    for value in {money(item.get('total_amount')), money(item.get('open_amount'))}:
                        self._dd_by_amount[(item.get('currency'), value)].append(iid)
        cash = abs(amount)
        entities = {txn.get('entity')} | ({'ES01', 'ES02', 'PT01'} if txn.get('entity') == 'ES00' else set())
        suppliers = getattr(self.ctx, 'suppliers', {})
        matches = set()
        for iid in self._dd_by_amount.get((txn['currency'], cash), ()):
            item = self.ctx.items[iid]
            if (item.get('entity') not in entities or item.get('open_amount') != cash
                    or item.get('issue_date', '') > txn['booking_date']):
                continue
            party = suppliers.get(item.get('party_id'), {})
            names = {norm(party.get('name')), norm(party.get('trade_name')), norm(item.get('supplier_name'))}
            if any(name and name.startswith(printed) for name in names):
                matches.add(iid)
        if len(matches) != 1:
            return None
        iid = matches.pop()
        return [{'bank_txn_id': txn['bank_txn_id'], 'item_id': iid, 'amount': f'{cash:.2f}',
                 'category': '', 'exception': ''}]

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

    def _card_chargeback(self, txn, amount):
        """A card chargeback reverses one card order (POLICIES 3, RETURNED).

        The bank text carries the masked card and the merchant id ('CHARGEBACK ****1159 MID 993110427');
        the settled order with that card, merchant and exact gross, operated before the chargeback,
        is the returned one. Ambiguity abstains.
        """
        if amount >= 0:
            return None
        raw = ' '.join(str(txn.get(k, '') or '') for k in ('text', 'ref1', 'ref2', 'remittance_info'))
        if not re.search(r'CHARGEBACK|CONTRACARGO|RUECKBELASTUNG|RETROCESION TARJETA|RIADDEBITO', raw.upper()):
            return None
        hits = {}
        for last4 in re.findall(r'\*{2,}\s*(\d{4})\b', raw):
            for mid in set(re.findall(r'\d{6,}', raw)):
                for row in self.card_orders.get((txn.get('entity', ''), mid, last4), ()):
                    if row['currency'] != txn['currency'] or abs(money(row['gross']) - abs(amount)) > CENT:
                        continue
                    if row.get('operation_date', row.get('settlement_date', '')) >= txn['booking_date']:
                        continue
                    hits[(row['settlement_id'], row['order_number'])] = row
        if len(hits) != 1:
            return None
        return self._allocate({'kind': 'card_settlements', 'rows': list(hits.values())}, txn, returned=True)

    def _returned_collection(self, txn, matches):
        """Choose which earlier direct-debit collection a return reverses (POLICIES 3 RETURNED, 5.10).

        The SEPA reason code fixes the delay: MD06 is a debtor refund request raised weeks after
        the debit (TRAIN golden: 21-75 days), every other R-code comes back within days (2-15).
        A later batch row of the same mandate and amount flagged re_presentation=Y names the
        returned invoice (POLICIES 5.10); when it names exactly one candidate, that candidate
        wins. Otherwise the latest collection inside the delay window, else all candidates.
        """
        if len(matches) < 2 or not all(m[3].get('mandate_id') for m in matches):
            return matches
        raw = ' '.join(str(txn.get(k) or '') for k in ('text', 'remittance_info', 'ref1', 'ref2')).upper()
        codes = set(re.findall(r'(?<![A-Z0-9])(AM04|MD06|MD01|MD07|AC01|AC04|AC06|MS02|MS03|SL01|AG01|AG02|'
                               r'RR04|BE05|FF01|MD02|RC01|RR01|RR02|RR03|SL02|SL03|SL11|SL12|SL13|SL14)(?![A-Z0-9])',
                               raw))
        try:
            booked = date.fromisoformat(txn['booking_date'])
        except (KeyError, ValueError):
            return matches
        def delay(m):
            try:
                return (booked - date.fromisoformat(m[0])).days
            except ValueError:
                return None
        pool = matches
        if len(codes) == 1:
            low, high = (21, 75) if codes == {'MD06'} else (1, 15)
            inside = [m for m in matches if delay(m) is not None and low <= delay(m) <= high]
            if inside:
                pool = inside
        cash = abs(money(txn['amount']))
        named = set()
        for m in pool:
            for row in self.represented.get(norm(m[3].get('mandate_id', '')), ()):
                if (row['currency'] == txn['currency'] and abs(money(row['amount']) - cash) <= CENT
                        and row.get('collection_date', '') > txn['booking_date']
                        and _days(row['collection_date'], txn['booking_date']) <= 60):
                    named.add(_printed_key(row.get('remittance_info', '')))
        if named:
            chosen = [m for m in pool if any(_same_printed(_printed_key(m[3].get('remittance_info', '')), n)
                                             for n in named)]
            if len({tuple(r['item_id'] for r in m[1]) for m in chosen}) == 1:
                return chosen
        return pool

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
        from .history import transfer_run_prefix
        # The debit quotes its run ID cut after the dash ('PR010024091060-0').
        _, cut_run = transfer_run_prefix(txn) if amount < 0 else ('', '')
        mixed = self._mixed_currency_run(txn, text)
        if mixed:
            return mixed
        # A rejected transfer carries an ISO reason code ('RETURN MD07', 'DEVOLUCION TRANSFERENCIA AC04');
        # 'DEVOLUCION SALDO A FAVOR' is a supplier refund, not a return.
        rejected_transfer = amount > 0 and bool(REJECTED_TRANSFER.search(
            ' '.join(str(txn.get(k) or '') for k in ('text', 'remittance_info')).upper()))
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
            # A supplier transfer is booked on or after its execution date (TRAIN October and
            # January: 0 of ~12,900 settled transfers booked earlier). A later-dated run is
            # another payment, never this line (e.g. a supplier direct debit of equal amount).
            if (not reverse and group['kind'] == 'payment_runs' and not group['confirming']
                    and group['rows'][0].get('run_type') == 'transfers' and group['date'] > txn['booking_date']):
                continue
            delta = _days(txn['booking_date'], group['date'])
            # Exact amount and account within the quoted run identify one transfer
            # (POLICIES 3 DUPLICATE_PAYMENT: a repeated transfer may be weeks old).
            explicit_transfer = (bool(cut_run) and group['kind'] == 'payment_runs' and not group['confirming']
                                 and group['ref'].startswith(cut_run))
            ref = group['ref'] in text or explicit_transfer
            # A rejected transfer often quotes only the beneficiary (POLICIES 3, RETURNED).
            beneficiary = norm(group['rows'][0].get('beneficiary', '')) if group['kind'] == 'payment_runs' else ''
            named = (reverse and not ref and not group['confirming'] and len(beneficiary) >= 6
                     and beneficiary in text and rejected_transfer)
            bank_amount = abs(amount)
            if group['currency'] != txn['currency']:
                if txn.get('orig_currency') != group['currency'] or not txn.get('orig_amount'):
                    continue
                bank_amount = abs(money(txn['orig_amount']))
            if abs(bank_amount-group['amount']) > CENT:
                continue
            if delta > (120 if ref and (group['confirming'] or reverse or explicit_transfer)
                        else 60 if named else 10 if ref else 3):
                continue
            ranked.append((int(ref)*100+int(named)*50-delta,idx,reverse))
        ranked.sort(reverse=True)
        if ranked and not (len(ranked)>1 and ranked[0][0] == ranked[1][0]):
            result = self._allocate(self.groups[ranked[0][1]],txn,returned=ranked[0][2],
                                    allow_partial=not ranked[0][2])
            if result:
                return result
        confirming = (self._confirming_by_maturity(txn, text, candidates)
                      or self._confirming_subset(txn, text, candidates)
                      or self._confirming_without_run(txn, text, candidates))
        if confirming:
            return confirming
        chargeback = self._card_chargeback(txn, amount)
        if chargeback:
            return chargeback
        returned_transfer = self._returned_transfer(txn, text)
        if returned_transfer:
            return returned_transfer
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
                            matches.append((detail_date,result,len(batch) >= 8 and batch in text,detail))
            # A return quoting the original batch ('REM 02241004067') reverses a collection
            # of that batch; otherwise the latest earlier collection is the returned one.
            if any(m[2] for m in matches):
                matches = [m for m in matches if m[2]]
            else:
                matches = self._returned_collection(txn, matches)
            latest = max((m[0] for m in matches),default='')
            unique = {tuple((r['item_id'],r['amount']) for r in m[1]): m[1] for m in matches if m[0] == latest}
            if len(unique) == 1:
                return next(iter(unique.values()))
        return self._supplier_direct_debit(txn)
