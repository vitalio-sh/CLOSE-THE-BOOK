"""Single ordered inference path shared by release generation and evaluation."""
from collections import Counter, defaultdict
from decimal import Decimal
import json
import re

from .validate import FIELDS


def build_handlers(ctx, enabled=('batches', 'documents', 'matching')):
    handlers = []
    document_stats = None
    if 'batches' in enabled:
        from .batches import BatchMatcher
        handlers.append(('batches', BatchMatcher(ctx).reconcile))
    if 'documents' in enabled:
        from .documents import DocumentMatcher
        matcher = DocumentMatcher(ctx)
        document_stats = matcher.stats
        handlers.append(('documents', matcher.reconcile))
    if 'matching' in enabled:
        from .matching import reconcile, classify
        from .nonitem import NonItemClassifier
        # Lines that TRAIN shows never settle an item go first, so no matcher can consume
        # an open item for them (POLICIES §3 UNIDENTIFIED, §4 categories and reversals).
        handlers.insert(0, ('nonitem', NonItemClassifier(ctx).reconcile))
        handlers.append(('category', lambda txn: classify(txn, ctx)))
        from .return_groups import reconcile_return_group
        handlers.append(('return_groups', lambda txn: reconcile_return_group(txn, ctx)))
        from .schedules import Schedules
        handlers.append(('schedules', Schedules(ctx).reconcile))
        # A quoted number that is another customer's invoice, one typo from the named payer's (5.5).
        from . import refnoise
        handlers.append(('refnoise_typo', lambda txn: refnoise.typo_owner(txn, ctx)))
        handlers.append(('matching', lambda txn: reconcile(txn, ctx)))
        from .financial_sequences import FinancialSequences
        handlers.append(('financial_sequences', FinancialSequences(ctx).reconcile))
        # Receipts net of credit notes (POLICIES 2, 5.9) that no ordinary rule explained.
        from .credit_groups import reconcile as credit_reconcile
        handlers.append(('credit_groups', lambda txn: credit_reconcile(txn, ctx)))
        # Unreferenced cross-border receipts short by a fixed intermediary fee (POLICIES §3).
        from .residual_fees import ResidualFees
        handlers.append(('residual_fees', ResidualFees(ctx).reconcile))
        handlers.append(('refnoise', lambda txn: refnoise.reconcile(txn, ctx)))
        # Quoted invoices with one TRAIN partial share or a unique rest (cut lists, ar_n_to_m).
        from .matching import quoted_groups, quoted_only
        handlers.append(('quoted_groups', lambda txn: quoted_groups(txn, ctx)))
        # Last resort: uniquely anchored printed fragments of a payer's documents (partial).
        from .matching import fragment_partial
        handlers.append(('fragments', lambda txn: fragment_partial(txn, ctx)))
        from .duplicates import ClosedDuplicates
        handlers.append(('closed_duplicate', ClosedDuplicates(ctx).reconcile))
        handlers.append(('quoted_only', lambda txn: quoted_only(txn, ctx)))
        # Last resort: unique payer group with one TRAIN partial share (ar_n_to_m).
        from .matching import party_partial_subset
        handlers.append(('party_partial', lambda txn: party_partial_subset(txn, ctx)))
        # Terms discount taken after the window: unique open invoice, PARTIAL at cash (POLICIES §3).
        from . import late_discount
        handlers.append(('late_discount', lambda txn: late_discount.reconcile(txn, ctx)))
    return handlers, document_stats


def exact_reference_items(txn, ctx):
    """Recognize full alphanumeric document tokens without consulting balances.

    Numeric suffixes, PO numbers and amount-like strings do not establish this
    priority. Closed items and credit notes stay in the identity catalogue.
    """
    from .matching import norm
    side = 'AR' if Decimal(txn['amount']) > 0 else 'AP'
    found = set()
    for field in ('text', 'ref1', 'ref2', 'remittance_info', 'end_to_end_id'):
        for match in re.finditer(r'[A-Za-z0-9][A-Za-z0-9/._-]*', str(txn.get(field) or '')):
            token = match[0].rstrip('.,')
            if not (re.search('[A-Za-z]', token) and re.search('[0-9]', token)):
                continue
            for iid in ctx.by_number.get(norm(token), ()):
                item = ctx.items[iid]
                if item.get('side') != side or item.get('issue_date', '') > txn['booking_date']:
                    continue
                owner = txn.get('entity')
                if item.get('entity') != owner and not (owner == 'ES00' and side == 'AP'
                        and item.get('entity') in ('ES01', 'ES02', 'PT01')):
                    continue
                found.add(iid)
    facts = getattr(ctx, 'pdf_facts', None)
    if facts is not None:
        found = {facts.resolve(iid) for iid in found}
    return found


def explicit_reference_items(txn, ctx):
    """Also accept typed numeric invoice lists within identified payer scope.

    Read original spans before normalization: decimal/thousands amounts, dates,
    percentages, bank IDs and untyped numbers never become invoice references.
    """
    from datetime import date
    from .matching import norm, party_candidates
    found = exact_reference_items(txn, ctx)
    suffixes = set()
    cues = {'FACTURA', 'FACTURAS', 'FAC', 'FACS', 'FRA', 'FRAS', 'FACT',
            'FACTS', 'INVOICE', 'INVOICES', 'INV', 'RECHNUNG', 'RECHNUNGEN',
            'RENR', 'FACTURE', 'FACTURES', 'FATTURA', 'FATTURE'}
    currencies = r'EUR|USD|GBP|CHF|CAD|AUD|PLN|MXN|BRL|CNY|ZAR|MAD|JPY|SEK|NOK|DKK'
    cash = abs(Decimal(txn['amount']))
    for field in ('text', 'ref1', 'ref2', 'remittance_info', 'end_to_end_id'):
        raw = str(txn.get(field) or '')
        mode = False
        end = 0
        for match in re.finditer(r'[A-Za-z0-9][A-Za-z0-9/._-]*', raw):
            token = match[0].rstrip('.,')
            if not re.fullmatch(r'[\s,;:/#]*', raw[end:match.start()]):
                mode = False
            end = match.end()
            key = norm(token)
            if key in cues:
                mode = True
                continue
            if mode and key in {'NO', 'NR', 'N'}:
                continue
            if not mode or not token.isdigit() or len(token) < 5:
                mode = False
                continue
            tail = raw[match.end():]
            if (re.match(r'[.,]\d{1,3}(?!\d)', tail)
                    or re.match(r'\s*[$€£¥%]', tail)
                    or re.match(r'\s*(?:' + currencies + r')\b', tail, re.I)):
                mode = False
                continue
            if Decimal(token) == cash or Decimal(token) / 100 == cash:
                continue
            is_date = False
            if len(token) == 8:
                for y, m, d in ((token[:4], token[4:6], token[6:]),
                                 (token[4:], token[2:4], token[:2])):
                    if 1900 <= int(y) <= 2100:
                        try:
                            date(int(y), int(m), int(d))
                        except ValueError:
                            pass
                        else:
                            is_date = True
            if not is_date:
                suffixes.add(token)
    if suffixes:
        side = 'AR' if Decimal(txn['amount']) > 0 else 'AP'
        owner = txn.get('entity')
        for party in party_candidates(txn, ctx):
            for iid in ctx.by_party.get(party, ()):
                item = ctx.items[iid]
                if item.get('side') != side or item.get('issue_date', '') > txn['booking_date']:
                    continue
                if item.get('entity') != owner and not (owner == 'ES00' and side == 'AP'
                        and item.get('entity') in ('ES01', 'ES02', 'PT01')):
                    continue
                number = norm(item.get('document_number'))
                if any(number.endswith(suffix) for suffix in suffixes):
                    found.add(iid)
    facts = getattr(ctx, 'pdf_facts', None)
    return {facts.resolve(iid) for iid in found} if facts is not None else found


def order_transactions(ctx, txns):
    """Prioritize explicit full invoices; preserve stateful payer-day sequences.

    Scheduling reads identities, static totals and bank evidence only. Matchers
    retain all allocation decisions and Context.apply remains the balance writer.
    """
    from .matching import party_candidates, text_of, words, _cash
    txns = sorted(txns, key=lambda txn: (txn['booking_date'], txn['bank_txn_id']))
    evidence, blocked = {}, defaultdict(set)
    boundaries = set()
    stateful = re.compile(
        r'RETURN|DEVOL|RUECK|CHARGEBACK|CONTRACARGO|IMPAY|REJECT|RECHAZ|STORNO|'
        r'DUPLIC|DOBLE|REPET|PARTIAL|PARCIAL|A CUENTA|ACOMPTE|INSTAL|ECHEANCE|'
        r'VENCIMIENTO|RETENC|RETENTION|GARANT|DISPUT|RECLAM|DEDUC|ANTICIP|PROFORMA')
    for txn in txns:
        tid = txn['bank_txn_id']
        refs = explicit_reference_items(txn, ctx)
        parties = set(party_candidates(txn, ctx))
        parties.update(ctx.items[i].get('party_id') for i in refs if i in ctx.items)
        parties.discard(None)
        evidence[tid] = refs, parties
        if stateful.search(words(text_of(txn))):
            # Returns may name an invoice on the opposite AR/AP side.
            reverse = {**txn, 'amount': str(-Decimal(txn['amount']))}
            reverse_refs = explicit_reference_items(reverse, ctx)
            parties.update(ctx.items[i].get('party_id') for i in reverse_refs if i in ctx.items)
            parties.discard(None)
            if parties:
                blocked[txn['booking_date']].update(parties)
            else:
                boundaries.add(tid)
    def priority(txn):
        refs, parties = evidence[txn['bank_txn_id']]
        if len(refs) != 1 or not parties or parties & blocked[txn['booking_date']]:
            return False
        item = ctx.items.get(next(iter(refs)), {})
        if item.get('kind') != 'invoice':
            return False
        cash, fx = _cash(item, txn)
        total = Decimal(str(item.get('total_amount', '0')))
        return cash is not None and total > 0 and abs(cash - total) <= Decimal('0.01')
    # Keep unknown-event barriers within independent entity books. The four
    # payment-factory entities share one book for scheduling because ES00 can
    # settle subsidiary AP items. Preserve slots of all other entity books.
    groups, memberships, spans = defaultdict(list), {}, defaultdict(int)
    for txn in txns:
        tid = txn['bank_txn_id']
        entity = txn.get('entity')
        scope = 'ES_FACTORY' if entity in ('ES00', 'ES01', 'ES02', 'PT01') else entity
        key = txn['booking_date'], scope
        if tid in boundaries:
            spans[key] += 1
        group = key + (spans[key],)
        memberships[tid] = group
        groups[group].append(txn)
        if tid in boundaries:
            spans[key] += 1
    streams = {key: iter(sorted(group, key=lambda txn: (not priority(txn), txn['bank_txn_id'])))
               for key, group in groups.items()}
    return [next(streams[memberships[txn['bank_txn_id']]]) for txn in txns]


def execute(ctx, txns, handlers, writer, audit, progress=None):
    """Emit each decision, then apply it exactly once before the next transaction.

    Inputs are sorted here so every caller has the same chronological contract.
    Handlers only propose rows; invalid proposals fall through to later handlers.
    """
    counts, rejected = Counter(), Counter()
    ordered = order_transactions(ctx, txns)
    ctx.phase_txns = ordered  # read-only view for phase-level pairing (bank reversals)
    from .reversal_pairs import bank_error_pairs
    reversal_ids = bank_error_pairs(ordered)
    from .matching import index_same_day
    index_same_day(ctx, ordered)
    for n, txn in enumerate(ordered, 1):
        rows = None
        source = 'unidentified'
        active_handlers = handlers
        if txn['bank_txn_id'] in reversal_ids:
            from .matching import row
            active_handlers = [('bank_error_pair', lambda t: [row(t, category='BANK_REVERSAL')])]
        for name, handler in active_handlers:
            rows = handler(txn)
            if rows and not ctx.proposal_valid(txn, rows):
                rejected[name] += 1
                rows = None
            if rows:
                source = name
                break
        if not rows:
            # POLICIES §3: UNIDENTIFIED exception means "a receipt for which
            # no open item exists".  Every matcher abstained here, so most such lines are
            # missed matches; claim the exception only with text evidence of an unknown
            # payer (generic reference, cash deposit, initials-only payer, round amount).
            # In TRAIN the UNIDENTIFIED category always carries the exception, so the
            # category-only row is kept as an honest abstention.
            from .nonitem import unidentified_evidence, fallback_category, unknown_payer_receipt
            # UNIDENTIFIED is a receipt-only category: in TRAIN no outgoing line carries it.
            outgoing = Decimal(txn['amount']) < 0
            exc = ('UNIDENTIFIED' if not outgoing and (unidentified_evidence(txn) or unknown_payer_receipt(txn, ctx))
                   else '')
            known = getattr(ctx, 'categories', ()) or ()
            category = 'UNIDENTIFIED' if exc else fallback_category(txn)
            if category not in known:
                category = 'BANK_FEES' if outgoing and 'BANK_FEES' in known else 'UNIDENTIFIED'
            rows = [dict(zip(FIELDS, [txn['bank_txn_id'], '', '', category, exc]))]
        rows = [{key: str(row.get(key, '') or '') for key in FIELDS} for row in rows]
        writer.writerows(rows)
        ctx.apply(txn, rows)
        counts[source] += 1
        audit.write(json.dumps({'bank_txn_id': txn['bank_txn_id'], 'module': source,
                                'items': [row['item_id'] for row in rows]}) + '\n')
        if progress is not None:
            progress(n, len(ordered), counts)
    return counts, rejected
