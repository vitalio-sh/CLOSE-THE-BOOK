"""Cash evidence for complete, explicitly referenced mixed-currency AP runs."""
from collections import defaultdict
from decimal import Decimal as D, ROUND_HALF_UP, InvalidOperation
import re
from .context import norm

CENT = D('.01')
ZERO = D(0)


def money(value):
    result = D(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not result.is_finite():
        raise ValueError('Non-finite money')
    return result


def signature(txn, rows):
    return (tuple((key, str(txn.get(key, ''))) for key in
                  ('bank_txn_id', 'amount', 'currency', 'orig_amount', 'orig_currency',
                   'entity', 'account_id', 'booking_date')),
            tuple(sorted(tuple(str(row.get(key, '')) for key in
                               ('item_id', 'amount', 'category', 'exception')) for row in rows)))


def complete_reference_ids(txn, prefix):
    """Parse whole whitespace-delimited identifiers, never valid prefixes.

    The only internal formatting admitted is a pipe, slash or hyphen between
    digit chunks, optionally spaced as in bank C34 text. Any other attached
    punctuation/letter invalidates the whole identifier. Unknown continuations
    are not stripped to expose a shorter, valid run ID.
    """
    ids = set()
    starter = re.compile(re.escape(prefix) + r'[0-9]')
    grammar = re.compile(re.escape(prefix) + r'[0-9]+(?:[|/-][0-9]+)*')
    for key in ('text', 'ref1', 'ref2', 'end_to_end_id', 'remittance_info'):
        tokens = str(txn.get(key, '') or '').upper().split()
        for index, token in enumerate(tokens):
            if not starter.match(token):
                continue
            lexeme = token
            cursor = index + 1
            while cursor < len(tokens):
                following = tokens[cursor]
                if lexeme[-1:] in ('|', '/', '-') and following[:1].isdigit():
                    lexeme += following
                    cursor += 1
                elif following in ('|', '/', '-') and cursor + 1 < len(tokens) and tokens[cursor + 1][:1].isdigit():
                    lexeme += following + tokens[cursor + 1]
                    cursor += 2
                elif following[:1] in ('|', '/', '-') and following[1:2].isdigit():
                    lexeme += following
                    cursor += 1
                else:
                    break
            if not grammar.fullmatch(lexeme):
                return set()
            ids.add(norm(lexeme))
    return ids


def run_reference_ids(txn):
    return complete_reference_ids(txn, 'PR')


def reference_rate(ctx, currency, day):
    if currency == 'EUR':
        return D(1), 'identity'
    rates = getattr(ctx, 'fx_rates', {})
    days = [d for d, values in rates.items() if d <= day and currency in values]
    if not days:
        return None
    rate_day = max(days)
    value = D(str(rates[rate_day][currency]))
    return (value, rate_day) if value.is_finite() and value > ZERO else None


def mixed_run_cash_evidence(ctx, txn, details):
    """Validate run identity and the foreign cash component, never total tolerance.

    Domestic cash is exact. Only the remaining foreign component may incur an
    adverse FX spread, capped at 3% of its independently converted reference sum.
    """
    if not details or money(txn['amount']) >= ZERO:
        return None
    raw = ' '.join(str(txn.get(key, '') or '') for key in
                   ('text', 'ref1', 'ref2', 'end_to_end_id', 'remittance_info'))
    counts = {int(value) for value in re.findall(r'\b(\d+)\s+(?:TRANSFERS\b|ORD\b)', raw.upper())}
    if len(counts) != 1:
        return None
    run_ids = {norm(row.get('run_id')) for row in details}
    if len(run_ids) != 1:
        return None
    run_id = next(iter(run_ids))
    if not run_id or run_reference_ids(txn) != {run_id}:
        return None
    if any(row.get('run_type') != 'aggregated'
           or row.get('debtor_account_id') != txn['account_id']
           or row.get('_entity') != txn['entity']
           or row.get('execution_date') != txn['booking_date'] for row in details):
        return None
    transfers = defaultdict(list)
    for row in details:
        transfer_id = row.get('end_to_end_id')
        if not transfer_id:
            return None
        transfers[transfer_id].append(row)
    if len(transfers) != next(iter(counts)):
        return None
    totals = defaultdict(lambda: ZERO)
    for transfer in transfers.values():
        currencies = {row.get('currency') for row in transfer}
        declared = {money(row['transfer_amount']) for row in transfer}
        if len(currencies) != 1 or not next(iter(currencies)) or len(declared) != 1:
            return None
        amount = sum((money(row['document_amount']) for row in transfer), ZERO)
        if amount <= ZERO or amount != next(iter(declared)):
            return None
        totals[next(iter(currencies))] += amount
    bank_currency = txn['currency']
    if len(totals) != 2 or bank_currency not in totals:
        return None
    bank_rate = reference_rate(ctx, bank_currency, txn['booking_date'])
    if bank_rate is None:
        return None
    rate_evidence = {bank_currency: bank_rate}
    foreign_reference = ZERO
    for currency, amount in totals.items():
        if currency == bank_currency:
            continue
        rate = reference_rate(ctx, currency, txn['booking_date'])
        if rate is None or amount <= ZERO:
            return None
        rate_evidence[currency] = rate
        foreign_reference += amount * bank_rate[0] / rate[0]
    foreign_reference = money(foreign_reference)
    foreign_cash = abs(money(txn['amount'])) - totals[bank_currency]
    if foreign_reference <= ZERO or foreign_cash <= ZERO:
        return None
    adverse = foreign_cash - foreign_reference
    if adverse < -CENT or adverse > foreign_reference * D('.03') + CENT:
        return None
    return dict(run_id=run_id, transfer_count=len(transfers), totals=dict(totals),
                rate_evidence=rate_evidence, domestic_cash=totals[bank_currency],
                foreign_reference=foreign_reference, foreign_cash=foreign_cash,
                adverse_spread=adverse / foreign_reference)


def _document_allocations(ctx, txn, details):
    """Resolve original details; beneficiary may explicitly be a factor.

    Ordinary transfers require the supplier's name. Factoring/confirming files
    can name their intermediary instead, but every document still needs a unique
    exact number/currency/amount/date identity and one supplier per transfer.
    """
    expected = defaultdict(lambda: ZERO)
    transfers = defaultdict(list)
    for detail in details:
        transfers[detail['end_to_end_id']].append(detail)
    for transfer in transfers.values():
        beneficiaries = {norm(row.get('beneficiary')) for row in transfer}
        if len(beneficiaries) != 1 or not next(iter(beneficiaries)):
            return None
        beneficiary = next(iter(beneficiaries))
        factor = bool(re.search(r'\b(?:FACTOR(?:ING)?|CONFIRMING|EFC)\b',
                                str(transfer[0].get('beneficiary', '')).upper()))
        documents = defaultdict(lambda: ZERO)
        for detail in transfer:
            number = norm(detail.get('document_number'))
            value = money(detail['document_amount'])
            credit = detail.get('document_type') == 'CREDIT_NOTE'
            if (not number or not value or credit != (value < ZERO)
                    or detail.get('document_type') not in ('INVOICE', 'CREDIT_NOTE')):
                return None
            documents[(number, detail['currency'], credit)] += abs(value)
        suppliers = set()
        pools = {}
        for (number, currency, credit), amount in documents.items():
            candidates, direct = [], []
            for iid in set(ctx.by_number.get(number, ())):
                item = ctx.items[iid]
                if (item.get('side') != 'AP' or item.get('currency') != currency
                        or (item.get('kind') == 'credit') != credit
                        or norm(item.get('document_number')) != number
                        or item.get('issue_date', '') > txn['booking_date']
                        or money(item.get('open_amount', 0)) < amount):
                    continue
                if item.get('entity') != txn['entity'] and not (txn['entity'] == 'ES00' and item.get('entity') in ('ES01', 'ES02', 'PT01')):
                    continue
                candidates.append(item)
                party = getattr(ctx, 'suppliers', {}).get(item.get('party_id'), {})
                aliases = {norm(party.get(key)) for key in ('name', 'trade_name')}
                aliases.update(norm(item.get(key)) for key in ('supplier_name', 'party_name'))
                if beneficiary in aliases:
                    direct.append(item)
            pools[(number, currency, credit)] = (amount, direct if direct else candidates if factor else [])
        # POLICIES §5.1/§5.3: one transfer pays one supplier. When a factor is the
        # named beneficiary, a printed number shared by several suppliers is
        # resolved by the only supplier that owns every document of the transfer.
        if factor and len(pools) > 1:
            common = None
            for amount, pool in pools.values():
                parties = {item.get('party_id') for item in pool if item.get('party_id')}
                common = parties if common is None else common & parties
            # Several suppliers can own the same printed numbers; the one whose
            # open amounts equal the transfer's document amounts is the payee.
            exact_hits = {party: sum(any(item.get('party_id') == party and
                                         (money(item['open_amount']) == amount or
                                          money(item['total_amount']) == amount) for item in pool)
                                     for amount, pool in pools.values())
                          for party in common or ()}
            best = max(exact_hits.values(), default=0)
            leaders = [party for party, hits in exact_hits.items() if hits == best]
            if best and len(leaders) == 1:
                party = leaders[0]
                pools = {key: (amount, [item for item in pool if item.get('party_id') == party])
                         for key, (amount, pool) in pools.items()}
        for (number, currency, credit), (amount, pool) in pools.items():
            # The supplied document amount distinguishes repeated printed numbers
            # from date/amount-incompatible invoices; equal eligible ties abstain.
            exact = [item for item in pool if money(item['open_amount']) == amount
                     or money(item['total_amount']) == amount]
            eligible = exact if exact else pool
            if len(eligible) != 1:
                return None
            item = eligible[0]
            if not item.get('party_id'):
                return None
            suppliers.add(item['party_id'])
            expected[item['item_id']] += amount
        if len(suppliers) != 1:
            return None
    return dict(expected)


def mixed_run_allocate(ctx, txn, details):
    """Make positive item-currency rows from the independently resolved details."""
    expected = _document_allocations(ctx, txn, details)
    if not expected:
        return None
    rows = []
    for iid, amount in expected.items():
        item = ctx.items[iid]
        opened = money(item['open_amount'])
        credit = item.get('kind') == 'credit'
        if amount > opened or (credit and amount != opened):
            return None
        exc = '' if credit else ('PARTIAL' if amount < opened else
              'FX_DIFFERENCE' if item['currency'] != txn['currency'] else '')
        rows.append(dict(bank_txn_id=txn['bank_txn_id'], item_id=iid,
                         amount=f'{amount:.2f}', category='', exception=exc))
    return rows


def mixed_run_rows_valid(ctx, txn, rows, cash_evidence, details):
    """Bind positive item-currency allocations to the exact run's signed totals."""
    expected_allocations = _document_allocations(ctx, txn, details)
    if expected_allocations is None:
        return False
    allocations = {}
    totals = defaultdict(lambda: ZERO)
    seen = set()
    for row in rows:
        iid = row.get('item_id')
        item = ctx.items.get(iid)
        if not item or iid in seen or row.get('bank_txn_id') != txn['bank_txn_id'] or row.get('category'):
            return False
        seen.add(iid)
        if item.get('side') != 'AP' or item.get('issue_date', '') > txn['booking_date']:
            return False
        if item.get('entity') != txn['entity'] and not (txn['entity'] == 'ES00' and item.get('entity') in ('ES01', 'ES02', 'PT01')):
            return False
        amount = money(row['amount'])
        opened = money(item['open_amount'])
        if D(str(row['amount'])) != amount or amount <= ZERO or amount > opened:
            return False
        credit = item.get('kind') == 'credit'
        if credit and amount != opened:
            return False
        exc = row.get('exception', '')
        expected = '' if credit else ('PARTIAL' if amount < opened else
                   'FX_DIFFERENCE' if item['currency'] != txn['currency'] else '')
        if exc != expected:
            return False
        totals[item['currency']] += -amount if credit else amount
        allocations[iid] = amount
    return bool(rows) and allocations == expected_allocations and dict(totals) == cash_evidence['totals']


def mixed_run_proposal_valid(ctx, txn, rows):
    """T10 hook: independently recheck signature, original detail evidence and cash.

    No mixed-currency exemption exists without a complete evidence record bound
    to precisely these bank fields and allocation rows.
    """
    try:
        evidence = getattr(ctx, '_mixed_run_proposal_evidence', {})
        if evidence.get('signature') != signature(txn, rows):
            return False
        cash = mixed_run_cash_evidence(ctx, txn, evidence.get('details', ()))
        return bool(cash and mixed_run_rows_valid(ctx, txn, rows, cash, evidence['details']))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
