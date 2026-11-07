# Team 10 — Close the Books

**Team:** Vitalii (lead and integrator), Luke Mininno, Ivan (eltriki-X), Marina.
**Repository:** https://github.com/vitalio-sh/CLOSE-THE-BOOK. Final solution: `review/team-integ-r11` (6c29aaa) [DA CONFERMARE: the final branch/commit merged to `main`].

## Problem
Grupo Valtermia has 17 companies, 12 currencies and 13 banks. Each bank line must be matched to the open invoices and credit notes it settles, with the settled amounts. A line that settles nothing must get a category, and every exception must be flagged (PARTIAL, RETURNED, FX_DIFFERENCE, DUPLICATE_PAYMENT…).

Several things make this hard:
- invoice numbers rarely appear cleanly in the bank text;
- one bank line can be a whole SEPA batch, a card settlement or a payment run;
- remittance advices arrive days later;
- key facts (instalments, "cancels and replaces", withholding, advances) are only in PDFs;
- FINAL drifts away from TRAIN: a new subsidiary (ZA01, ZAR), a new bank file format, renamed customers.

## Approach
A **deterministic chain of rules**, ordered from the most certain evidence to the weakest:
1. **Batch files.** SEPA/BACS/ACH direct-debit batches, card settlements, cheque deposits and payment runs are expanded to their invoices. Returns are matched through the mandate and the reason code.
2. **Remittance advices** (CSV, XLSX, text, PDF) are linked to the cash they explain, including deductions and their reasons.
3. **Document references and payer identity:**
   - noisy invoice numbers;
   - truncated payer names and IBANs;
   - parent companies, factoring and confirming banks;
   - same-amount invoices resolved FIFO, or by the service month named in the text.
4. **Exceptions from the policy text:** early-payment discount within terms, bank charges, withholding, FX difference against ECB rates, partial payments, duplicates.
5. **PDF facts** (instalments, retentions, advances, replaced invoices) and **categories** for lines that settle no item, learned from TRAIN history.

Two principles hold across all steps:
- **Never force a match.** When the evidence is ambiguous, the solver abstains or flags UNIDENTIFIED.
- **General rules only**, so that the solution survives the drift into FINAL.

## Key ideas
- **Multi-agent development.** Vitalii ran a Claude orchestrator that coordinates several agents. Each teammate's work was integrated in rounds (r6 → r11). Every round is checked on held-out data before it is accepted.
- **Fair holdout** (`make_holdout.py`). It cuts TRAIN at 2025-03-31 and turns April–June 2025 into a LIVE-shaped phase, with opening balances and PDFs for that window. Any solution runs on it unchanged, and the official scorer reports per-segment results.
- **Adversarial validation** before integration. Example: returned direct debits by reason code. r6 got 1,796 of 1,835 right (97.9%), and the errors were concentrated in MD06 refund requests: 27 wrong picks and 25 false DUPLICATE_PAYMENT later on. Unit tests encode the policy (9 tests); they exposed a precedence bug, which is now fixed.
- **Entity-first payer resolution** (Marina): customers are searched within the company that received the cash before anything else. [DA CONFERMARE: short description of the idea]
- **Categories from TRAIN history** (Ivan): cash pool, FX deal, bank reversal, taxes and similar lines are classified from TRAIN history.

## Results
| Time | Official LIVE score |
|---|---|
| 11:18 | 81.13 |
| | 84.06 |
| | 87.81 |
| r7 | 92.96 |
| r8 | 93.41 |
| r11 (16:19) | **94.76**, 2nd place |

On the Q2-2025 holdout, r6 scored 89.14, against 77.22 for the 11:18 baseline. FINAL score: [DA CONFERMARE].

## Stack and models
- **Solver:** Python 3.12; `pdftotext` for PDFs; the official scorer and validator. [DA CONFERMARE: other libraries; whether any LLM is called at run time — we believe not]
- **Development:** Claude Code, with Claude Opus as the main coding agent and Claude Sonnet as read-only sub-agents (exploration, review, FINAL readiness checks). Vitalii's orchestrator: [DA CONFERMARE: models].

## What we would do next
- **MD06 returns.** Link the refund to the right monthly collection through `re_presentation=Y`; dates alone cannot separate the candidates. This would remove 27 errors and 25 false DUPLICATE_PAYMENT.
- **Remittance advices.** Better linking of late or reformatted advices to their cash.
- **Noisy references.** Receipts with noisy references, many-to-many matches, and receipts net of credit notes.
