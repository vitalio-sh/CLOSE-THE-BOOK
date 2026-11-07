# Team 10 — Close the Books

> Draft. Final wording and delivery are Vitalii's decision.

**Team:** Vitalii (lead and integrator), Luke Mininno, Ivan (eltriki-X), Marina Moncayola.
**Repository:** https://github.com/vitalio-sh/CLOSE-THE-BOOK. Final solution: `review/team-integ-r14` (8b00ac7).

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
1. **Batch files.** SEPA/BACS/ACH direct-debit batches, card settlements, cheque deposits and payment runs are expanded to their invoices. Returns are matched through the mandate and the reason code. Re-presentations are linked through a lookahead on `re_presentation=Y`.
2. **Remittance advices** (CSV, XLSX, text, PDF) are linked to the cash they explain, including deductions and their reasons.
3. **Document references and payer identity:**
   - noisy invoice numbers;
   - truncated payer names and IBANs;
   - parent companies, factoring and confirming banks;
   - same-amount invoices resolved FIFO, or by the service month named in the text.
4. **Exceptions from the policy text:** early-payment discount within terms, bank charges, withholding, FX difference against ECB rates, partial payments, duplicates.
5. **PDF facts** (instalments, retentions, advances, replaced invoices) and **categories** for lines that settle no item, looked up from TRAIN history.

Two principles hold across all steps:
- **Never force a match.** When the evidence is ambiguous, the solver abstains or flags UNIDENTIFIED.
- **General rules only**, so that the solution survives the drift into FINAL.

## Key ideas
- **Multi-agent development.** Vitalii ran a Claude orchestrator that coordinates several agents working in separate lanes. The work was integrated in rounds (r6 → r14), and every round was checked on held-out data before it was accepted.
- **Fair holdout** (`make_holdout.py`, Luke). It cuts TRAIN at 2025-03-31 and turns April–June 2025 into a LIVE-shaped phase, with opening balances and PDFs for that window. Any solution runs on it unchanged, and the official scorer reports per-segment results.
- **Adversarial validation** before integration (Luke). Example: returned direct debits by reason code, measured on r6. r6 got 1,796 of 1,835 right (97.9%); the errors were concentrated in MD06 refund requests, with 27 wrong picks and 25 false DUPLICATE_PAYMENT later on. Unit tests encode the policy (9 tests). They exposed a precedence bug on single-row batches; the fix and its test are in r14 (6c29aaa).
- **Independent temporal check** on March 2025, a month never used for tuning (opening balances from the golden, so not an official score): r9 93.51 → r11 94.20. That is +0.69, the same gain as the official LIVE difference from r9 to r11.
- **Entity-first payer identification** (idea: Marina Moncayola, dc4690e). When a bank line's text matches several customers, the solver ranks only those that belong to the company owning the bank account. For ES00 payments it also ranks customers of ES01, ES02 and PT01, the companies whose payments ES00 makes as payment factory. This way a same-named customer abroad can't outrank the real local payer. Measured: +0.05 (October) and +0.03 (January), with 23 + 10 fixes and 1 regression; included in every release since r11.
- **Ivan:** an independent bankrec pipeline, plus review findings and porting candidates. His rules were reviewed and the issues he found were fixed. The team solver's non-item category lookup (`recon/nonitem_lookup.py`) was built by a team lane, and Ivan's code is not in r14.

## Results
| Version | Official LIVE score |
|---|---|
| first submission (11:18) | 81.13 |
| | 84.06 |
| | 87.81 |
| r7 | 92.96 |
| r8 | 93.41 |
| r11 | 94.76 |
| r12 | 94.82 |
| r13 | 94.87 |
| r14 | **94.91**, rank 2 |

On the Q2-2025 holdout, r6 scored 89.14, against 77.22 for the 11:18 baseline. The FINAL score is hidden until the reveal.

## Stack and models
- **Solver (`recon/`):** Python standard library, plus `pdftotext` for PDFs. **No model is called at run time.**
- **Development:** Claude was used to build, test and coordinate the solution:
  - Claude Code, with Claude Opus as the main coding agent and Claude Sonnet as read-only sub-agents (exploration, review, FINAL readiness checks);
  - Vitalii's Claude orchestrator, which ran several agent lanes.

## What we would do next
- **MD06 returns.** The 27 errors and 25 false duplicates were measured on r6. The re-presentation lookahead in r9 and later targets them: in a TRAIN simulation it decides 744 of 1,264 ambiguous MD06 returns, with 0 wrong. Its effect on these Q2 errors is unmeasured.
- **Remittance advices.** Better linking of late or reformatted advices to their cash.
- **Noisy references.** Receipts with noisy references, many-to-many matches, and receipts net of credit notes.
- **English bank wording for ZA01.** Unmatched outflows such as "BANK CHARGES" currently fall back to CUSTOMER_REFUND instead of BANK_FEES. Fix: add English and South African fee, tax and payroll wording to the category rules.
