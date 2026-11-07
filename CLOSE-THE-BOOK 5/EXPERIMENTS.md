# Experiment log

Decision metric: mean official score over TRAIN 2025-06 and 2025-03 (`tools/eval.py` + `tools/gate.py`).
LIVE leaderboard is recorded when available. TRAIN mean has tracked LIVE closely (76.23 vs 76).

| ID | Change | Sample | TRAIN 06 | TRAIN 03 | TRAIN mean | Gate | LIVE | Kept |
|---|---|---|---|---|---|---|---|---|
| base | commit 4d668da (= live_submission_v2) | 80.44 | 75.29 | 77.17 | 76.23 | — | 76 | ✓ |
| E1 | `party_fifo`: several invoices without reference = oldest-first run of the payer's open invoices, excluding never-paid decoys (> 400 days), exact to the cent | 80.66 | 75.34 | 77.21 | 76.275 (+0.045) | PASS | — | ✓ |
| E2 | `settle`: an invoice that ends up completely unpaid is removed from the payment instead of a 0.00 SHORT_DISPUTE row (1,121 such rows in LIVE v2) | 80.68 (E1+E2) | evaluated together with E3 | | | | | ✓ |
| E3 | Payer identification ranked: same entity as the bank account first (ES00 → ES01/ES02/PT01 for payments), score = common prefix with the FULL name (truncated names), IBAN and tax id as direct keys. Before: arbitrary order, "POOL SUPPLY" matched dozens of customers in other countries | 83.34 | 76.73 | 78.76 | **77.745 (+1.47)** | PASS | — | ✓ |

## Diagnosis (TRAIN 2025-06, 38k lines)

1. **Known payer, no invoice choice.** ~3,800 lines/month (28k pairs ≈ half of all misses) end UNIDENTIFIED although the
   name matcher finds the right customer in 89 % of them; 2,205 are an exact sum of that customer's invoices.
   E1 only recovered a few hundred → the invoices are probably not in the candidate pool at all (being traced with
   `tools/trace.py`).
2. **Short / partial payments with a known payer** (~1,100 lines/month): 333 are textbook 2 % early-payment discounts
   of the US entity (D2N30US, paid in ~5 days) that should already match → bug being traced. US never appears in the
   sample month, so it was invisible there.
3. **Unlinked big remittance advices / payment runs** (a few thousand pairs) and the 1,121 zero-amount dispute rows (E2).
