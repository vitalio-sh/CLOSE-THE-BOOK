"""Matcher registry, in priority order (first claim on a bank line wins).

Ownership (one person / one agent per file, to avoid merge conflicts):
  batches.py     direct debits, card settlements, cheque deposits, payment runs
  remittance.py  customer remittance advices (pdf/csv/xlsx/txt)
  receipts.py    AR receipts: payer resolution, noisy references, subset-sum, exceptions
  payments.py    AP payments not covered by payment runs (single transfers, supplier DD, refunds)
  nonitem.py     lines that settle no item -> category
"""
from . import batches, nonitem

MATCHERS = [
    ("direct_debits", batches.direct_debits),
    ("card_settlements", batches.card_settlements),
    ("cheque_deposits", batches.cheque_deposits),
    ("payment_runs", batches.payment_runs),
    ("nonitem", nonitem.match),
]

# optional modules: registered when present so people can work in parallel
try:
    from . import remittance
    MATCHERS.insert(4, ("remittance", remittance.match))
except ImportError:
    pass
try:
    from . import receipts
    MATCHERS.insert(-1, ("receipts", receipts.match))
except ImportError:
    pass
try:
    from . import payments
    MATCHERS.insert(-1, ("payments", payments.match))
except ImportError:
    pass
# last: document numbers in the text that add up exactly to the cash, on lines nobody else claimed
from . import docref
MATCHERS.append(("docref", docref.match))
