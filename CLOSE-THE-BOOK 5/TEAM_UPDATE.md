# Close the Books: team update (7 Nov, afternoon)

## Where we are

| | Score |
|---|---|
| Naive baseline (organizers) | ≈ 19 |
| "Well-tuned rules" (organizers) | ≈ 61 |
| **Us, sample month 2025-06** | **80.38** |
| **Us, TRAIN 2025-06** (38k lines, 700k open items) | **75.08** |
| **Us, TRAIN 2025-03** (second check) | **74.65** |

All scores come from the **official scorer** (`bankrec_score.py`, the same code the server runs). Every output file is
also passed through the official validator before it's considered usable.

## How we got here

| Step | Sample | TRAIN 06 | What changed |
|---|---|---|---|
| Original skeleton | 0 (crashed) | — | Looked for `.csv`; real data is `.csv.gz` |
| Real data + POLICIES.md | 40.94 | — | Real column names; `settle` follows POLICIES exactly (amounts in item currency, discount by payment terms + 3 days, withholding 1.25/1.5/10 %, charges ≤ 60 EUR & 2 %) |
| **Document evidence** | 62.79 | — | Remittance advices (PDF/CSV/XLSX/email), SEPA DD batches, card settlements, cheque deposits and payment runs are linked to bank lines **before** the date loop; their items are reserved |
| Confirming + fees | 66.22 | — | Fee lines quoting a batch no longer match the batch |
| Categories + returns | 70.73 | 67.93 | Keyword/bank-code categories, both legs of BANK_REVERSAL, RETURNED via mandate / cheque no. / card |
| Near amounts + duplicates | 76.16 | 72.69 | Discount / withholding / bank charges when the amount isn't exact; DUPLICATE_PAYMENT; FX only with the original amount; withholding only MX/BR/MA customers |
| Confirming as transfer subsets, FX payment runs | 77.66 | 73.15 | One confirming run is debited in several lines = N whole transfers (text says "N PROVEEDORES") |
| Any-prefix references + completing truncated texts | 81.33 | 73.52 |
| **Constrained completion + PDF clauses + one item per cited number** | **80.38** | **75.08** | `RE…`/`GS…`/`FA…` numbers, bare suffixes (`RE250106232 106234`), and the rest of a payment found from the customer's recent open items |

## Key insights (why this works)

1. **The score is pair-weighted.** One line can settle 585 invoices. In the sample, 75 "dispute" lines were worth
   +14 points, more than 2,155 noisy-reference lines (+5). So documents (advices, batches) come first.
2. **Each exception class is ~2 points** (macro-F1 over 10 classes). Rare classes (withholding, duplicates,
   bank charges) are cheap points; we went from ~0 to 0.3–0.9 on most of them.
3. **All inputs are available at once.** Advices arrive days after the cash, so we link globally before processing
   by date instead of going strictly chronologically.
4. **Precision gates matter.** Every loose rule was measured on TRAIN; two that looked fine on the sample had
   0.28 precision on TRAIN and were tightened (FX without original amount, overpayment without reference).

> ✅ Fixed: completion now only for customer receipts missing whole invoices; PDF clauses (instalments,
> retention, advances, "cancels and replaces") at 0.95 precision; the same printed number in several companies no
> longer produces duplicate rows. Engine ~2x faster.
>
> (History) The completion step first gained +3.7 on the sample but only +0.4 on TRAIN: with 700k open items, "find other open
> invoices of the customer that sum exactly to the rest" also finds accidental sums (precision fell 0.97 → 0.89).
> Being tightened now. Lesson: **always validate on TRAIN, not just the sample.**

## Exception F1 (TRAIN 2025-06, latest)

RETURNED 0.88 · SHORT_DISCOUNT 0.85 · DUPLICATE_PAYMENT 0.75 · FX_DIFFERENCE 0.75 · PARTIAL 0.53 ·
SHORT_DISPUTE 0.43 · SHORT_WITHHOLDING 0.36 · UNIDENTIFIED 0.28 · SHORT_BANK_CHARGES 0.18 · OVERPAYMENT 0.06

## Risks

- **Runtime.** One TRAIN month ≈ 3 min load + 2.5 min evidence + 3–5 min engine. LIVE is 6 months (≈ 210k lines):
  expect ~25–35 min. FINAL data arrives near the end with one shot, so **we must do a full LIVE dress rehearsal
  and time it.**
- The October 2025 bank format change (N43 → camt) and the September 2025 advice layout change aren't in TRAIN.
  The code is format-agnostic, but only LIVE will tell.

## Next steps (proposed)

1. Validate the latest change on TRAIN (running), then **first LIVE submission** (needs the LIVE download, 87 MB).
2. Speed: profile the engine; target < 15 min for LIVE.
3. Still missing: PARTIAL (instalments/retentions in PDFs), OVERPAYMENT, unlinked dispute advices, AP FX.
4. Freeze code at 17:00, then FINAL with the exact LIVE command.

## Guard-rails: agents, skills and the regression gate (new)

Every change is now measured the same way, and a change that makes things worse can't be kept by accident.

| Piece | What it does |
|---|---|
| `tools/eval.py --tag X` | Official score on sample + TRAIN 2025-06 + TRAIN 2025-03 (~15 min); `--quick` = sample only |
| `tools/gate.py out/eval/X` | PASS/FAIL vs `eval/baseline.json`: valid files, TRAIN mean not lower, no month −0.3, not >30 % slower |
| `tools/analyze.py errors / oracle` | Where we lose points (per segment / rule, with real examples) and how many points each segment is worth |
| Skill `bankrec-eval` | How to measure and read the numbers (sample never decides) |
| Skill `bankrec-improve` | The loop: biggest loss → one hypothesis → implement → gate → keep or revert → log |
| Skill `bankrec-submit` | LIVE and single-shot FINAL protocol (incl. `masters_final`) |
| Skill `bankrec-knowledge` | How documents link to bank lines, key policies, and the list of failed experiments |
| Agent `bankrec-evaluator` | Runs eval + gate, reports PASS/FAIL (read-only) |
| Agent `bankrec-error-analyst` | Ranks the next 5 improvements with examples (read-only) |
| Agent `bankrec-rule-improver` | Implements ONE hypothesis; commits only if the gate passes, else reverts and logs the lesson |
| Agent `bankrec-submission-guard` | Builds the LIVE/FINAL file and returns a GO/NO-GO checklist (never uploads) |

Runs are deterministic now (same commit → byte-identical file), so every delta the gate shows is real.
In Claude Code, inside the repo: *"use the bankrec-error-analyst agent"*, then *"use the bankrec-rule-improver agent on
hypothesis 1"*.

## LIVE

| File | Code | Lines | Runtime |
|---|---|---|---|
| `live_submission_v1.csv.gz` | before the last two fixes | 203,083 / 203,083 ✓ | 44 min (shared CPU) |
| `live_submission_v2.csv.gz` | TRAIN 75.08 version | 203,083 / 203,083 ✓ | 25 min |

**Leaderboard: v2 = 76** (leader 95.6). Our TRAIN mean for the same code is 76.23, so **local evaluation predicts LIVE
well** and the gate can be trusted.

## Why 76 and not 95 (evidence, TRAIN June)

1. **We know the payer but don't pick the invoices**: ~3,800 lines/month (≈ half of all missed pairs) end UNIDENTIFIED
   although the right customer is identified in 89 % of them; 2,205 are an exact sum of that customer's invoices.
2. **Short/partial payments with known payer** (~1,100 lines/month), incl. 333 US 2 % early-payment discounts that
   should already match (bug being traced; US never appears in the sample month).
3. **Big remittance advices / payment runs not linked**, and 1,121 zero-amount SHORT_DISPUTE rows in v2 (fixed in E2).

Experiments and their gate results: see `EXPERIMENTS.md`. E1 (FIFO run of non-stale invoices) passed: +0.05.

## How to run

```bash
pip install pandas openpyxl
python run.py --data data/sample --masters data/masters --eval                                   # 1 month, ~1 min
python run.py --data data/train  --masters data/masters --from 2025-06-01 --to 2025-06-30 --eval  # ~10 min
python run.py --data data/live   --masters data/masters --out out/live.csv                        # -> out/live.csv.gz
```

Upload only if the log says `VALIDACIÓN: OK`. If validation fails, the files are renamed to `*.INVALID.csv*` so they
can't be uploaded by mistake. `pdftotext` (poppler-utils) is needed for PDF advices.

Code: branch `claude/bankrec-engine` (see `close-the-book.bundle` / `close-the-book-code.zip`); README has the full progress table.
