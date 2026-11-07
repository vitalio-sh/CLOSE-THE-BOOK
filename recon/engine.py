"""Single ordered inference path shared by release generation and evaluation."""
from collections import Counter
from decimal import Decimal
import json

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
        handlers.append(('category', lambda txn: classify(txn, ctx)))
        handlers.append(('matching', lambda txn: reconcile(txn, ctx)))
    return handlers, document_stats


def execute(ctx, txns, handlers, writer, audit, progress=None):
    """Emit each decision, then apply it exactly once before the next transaction.

    Inputs are sorted here so every caller has the same chronological contract.
    Handlers only propose rows; invalid proposals fall through to later handlers.
    """
    counts, rejected = Counter(), Counter()
    ordered = sorted(txns, key=lambda txn: (txn['booking_date'], txn['bank_txn_id']))
    for n, txn in enumerate(ordered, 1):
        rows = None
        source = 'unidentified'
        for name, handler in handlers:
            rows = handler(txn)
            if rows and not ctx.proposal_valid(txn, rows):
                rejected[name] += 1
                rows = None
            if rows:
                source = name
                break
        if not rows:
            # POLICIES §3: UNIDENTIFIED exception means "a receipt for which
            # no open item exists".  Clear the exception when the payer was
            # identified (party_candidates non-empty) or the line is outgoing
            # (negative cash), keeping only the UNIDENTIFIED category.
            from .matching import party_candidates
            # UNIDENTIFIED is a receipt-only category: in TRAIN no outgoing line carries
            # it, and BANK_FEES is by far the most common no-item outgoing category.
            outgoing = Decimal(txn['amount']) < 0
            exc = '' if outgoing or party_candidates(txn, ctx) else 'UNIDENTIFIED'
            category = 'BANK_FEES' if outgoing and 'BANK_FEES' in getattr(ctx, 'categories', ()) else 'UNIDENTIFIED'
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
