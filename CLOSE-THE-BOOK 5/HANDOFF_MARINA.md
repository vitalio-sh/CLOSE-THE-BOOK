# Handoff: Marina Moncayola · bank-reconciliation solver (Close the Books)

Branch: `solution/marinamoncayola`. Rule-based solver in `recon/`, entry point `run.py`. Every number below
was produced by a run in this work session. Nothing is estimated.

## Cases I focused on

1. **Getting the starter skeleton to run on the real data**: `.csv.gz`, real column names, POLICIES.md applied in
   `recon/settle.py` (amounts in item currency, discount by payment terms + 3 days, withholding 1.25/1.5/10 % only
   for MX/BR/MA, bank charges ≤ 60 EUR and ≤ 2 %, FX on every invoice row).
2. **Document-driven lines, where most allocation pairs are** (`recon/evidence.py`): remittance advices in
   PDF/CSV/XLSX/email, SEPA direct-debit batches, card settlements, cheque deposits, payment runs, and confirming
   (one run debited in several lines = N whole transfers). Linked globally before the date loop; their items are
   reserved.
3. **Exceptions worth ~2 points each**: RETURNED (mandate / cheque / card), DUPLICATE_PAYMENT, SHORT_DISCOUNT /
   WITHHOLDING / BANK_CHARGES with a known payer (`party_near`), PARTIAL from PDF clauses (instalments,
   retention, advance) and "cancels and replaces" (`recon/pdfclauses.py`).
4. **References**: any real document prefix (FV/AB/FA/AV/RE/GS/INV…), bare suffix numbers, one item per cited
   number (the same printed number exists in several group companies).
5. **Payer identification** (latest, biggest gain): rank candidate payers by entity of the bank account, common
   prefix with the full name (truncated names), IBAN and tax id.

## Rules implemented (order in `recon/rules/__init__.py`)

`evidence` → `keywords` (no-invoice categories, BANK_REVERSAL pairs) → `returned` → `by_reference` (with
completion of truncated texts, receivables only) → `category_strict` → `party_exact` (FX only with original amount)
→ `pdf_partial` → `duplicate` → `party_fifo` (oldest-first run of non-stale invoices, exact) → `party_subset`
→ `party_near` (gap explained by discount / withholding / bank charges) → `category_loose` → default
(receipts → UNIDENTIFIED, payments → BANK_FEES).

## How to run (exact commands)

```bash
pip install -r requirements.txt          # + system package poppler-utils (pdftotext)
# data (not in the repo): extract the official zips and link them
#   data/masters -> masters/, data/sample -> sample/, data/train -> train/ (train-lite), data/live -> live/
python run.py --data data/sample --masters data/masters --eval                        # 1 month, ~1 min
python run.py --data data/live   --masters data/masters --out out/live.csv --json out/live.json   # LIVE, ~25 min
python tools/eval.py --tag X && python tools/gate.py out/eval/X                        # regression gate (~15 min)
```
`run.py` validates its own output with the official validator (`recon/bankrec_score.py`, the server code) and
renames the file to `*.INVALID.csv*` if it fails. Runs are deterministic from commit 496b762 on
(PYTHONHASHSEED pinned).

## Scores actually run (official scorer)

| Commit | What | Sample 2025-06 | TRAIN 2025-06 | TRAIN 2025-03 | TRAIN mean | LIVE leaderboard |
|---|---|---|---|---|---|---|
| 8cc9192 | real data + POLICIES | 40.94 | — | — | — | — |
| 35b8abb | document evidence | 62.79 | — | — | — | — |
| 299da61 | categories + returns (+ date-windowed evidence) | 70.73 | 67.93 | — | — | — |
| ba6f35a | near amounts + duplicates | 76.16 | 72.69 | 74.65 | — | — |
| edc3b23 | PDF clauses + one item per number | 80.38 | 75.08 | — | — | **76** (`live_submission_v2.csv.gz`) |
| 4d668da | same code, deterministic (baseline) | 80.44 | 75.29 | 77.17 | 76.23 | — |
| e58863c | E1 `party_fifo` | 80.66 | 75.34 | 77.21 | 76.275 | — |
| 69e0580 | E2 + E3 payer identification | **83.34** | **76.73** | **78.76** | **77.745** | not submitted |

- LIVE v1 (`live_submission_v1.csv.gz`) was produced from an intermediate, uncommitted state; I have no
  leaderboard score for it.
- LIVE v2 was produced before runs were deterministic: re-running edc3b23 can differ slightly (≈ 0.02 points
  on the sample).
- TRAIN windows rebuild the open items at the window start from the golden; the TRAIN mean for v2's code (76.23)
  matched its LIVE score (76).
- Leader on the LIVE leaderboard at the last check: 95.6.

Per-window details (exception F1, rule precision, segments, timing) are in `out/eval/<tag>/summary.json`
(generated, not committed) and summarised in `EXPERIMENTS.md`.

## Known failures (TRAIN 2025-06, latest commit)

- Exception F1: OVERPAYMENT 0.06, SHORT_BANK_CHARGES 0.21, UNIDENTIFIED 0.32, WITHHOLDING 0.40, SHORT_DISPUTE 0.47.
- ~5,000 lines/month still default to UNIDENTIFIED; only ~23 % of them really are unidentified. Before E3, 2,205 of
  them were exact sums of the payer's invoices. E3 recovered part of that; the rest is not re-measured yet.
- Some big remittance advices never link to their bank line (example: US01 Fairhaven wire of 548,367.20 USD on
  2025-06-02; no parsed advice totals that amount).
- US invoice numbers are purely numeric and the bank text cites them as `RMR*IV*103251*…`; these are not parsed as
  references.
- Runtime: LIVE ≈ 25 min wall (v2). E3 makes payer identification slower (sample engine 4 s → 13 s); LIVE time
  for E3 is not yet measured.
- `pdf_partial` mislabels some retention *releases* as PARTIAL when the first payment was not matched.

## Unfinished work

- **LIVE v3** (commit b49f703, E3) is being generated (`out/live3.csv.gz`). It is not yet validated or submitted.
- E2 was not evaluated on its own (it was measured together with E3).
- The experiments in "Known failures" above have not been started.

## My next task

1. Finish LIVE v3 and check `VALIDACIÓN: OK`, coverage 203,083/203,083 and wall time; upload it and record the
   leaderboard score next to commit b49f703 in `EXPERIMENTS.md`.
2. Re-run `python tools/analyze.py errors out/eval/E3-payer --window train_2506` and attack the largest remaining
   block of known-payer lines (short/partial payments and multi-invoice sums), one gated experiment at a time
   (`.claude/agents/bankrec-rule-improver.md`).

## What is NOT in the repository (on purpose)

Raw datasets (`data/`), generated outputs (`out/`), caches (`models/`), submission files (`*.csv.gz`) and
credentials. See `.gitignore`.
