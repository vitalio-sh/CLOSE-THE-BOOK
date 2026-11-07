# TALKY Hack — bank reconciliation at scale (Grupo Valtermia)

One-day hackathon (2026-11-07, Madrid time). For every bank line say which open items it settles, the amount of each
(in item currency), or a `category` if it settles none, plus an `exception`. Rules: `bankrec-docs-and-masters/POLICIES.md`
(the golden follows it LITERALLY — read it before changing any rule). Format: `FORMAT.md`. Columns: `DATA_DICTIONARY.md`.

Score = 100 × (0.5·pair F1 + 0.3·line accuracy + 0.2·exception macro-F1). Amount tolerance max(0.01, 0.5 %).
Reference: naive 19, "good heuristic" 61.

## Timeline (Madrid)
18:00 FINAL data released (`/data/v1/bankrec-final.tar.gz`) · **19:30 submissions close** · 19:45 project (video ≤5 min,
GitHub repo, description) + presentations · 20:30 reveal. FINAL = ONE scored submission (first valid file counts).
LIVE limits: 1 submission / 180 s, 20 / day. **Do not iterate on the leaderboard — iterate on `dev`.**

## Layout
- `data/` (gitignored): `masters/ sample/ starter/` (symlinks to the docs), `train/` (TRAIN lite), `live/`, later `final/`.
- `bankrec/data.py` — `load_phase(name)`: sample | dev | train | live | final. `dev` = TRAIN window 2025-01..06 with open
  items at the window start rebuilt from the golden — our LIVE simulator (golden available).
- `bankrec/pipeline.py` — `State` (claims + open-item ledger: `st.assign(txn, [(item, amount, exc)])`,
  `st.classify(txn, category, exc)`), cascade runner, validator, writer. First claim on a line wins.
- `bankrec/matchers/` — one file per family, registered in `matchers/__init__.py` in priority order:
  `batches.py` (DD batches/returns, cards, cheques, payment runs), `nonitem.py` (categories, learned lookup + bank
  reversals), `remittance.py` (advices), `receipts.py` (AR receipts), `payments.py` (AP leftovers).
- `bankrec/refs.py` — noisy document-number index (`RefIndex.find(text, entity)`), `bankrec/ctx.py` cached lookups.
- `scripts/run.py` → submission + parquet (with `matcher` column). `scripts/eval.py` → official score + per-segment and
  per-matcher accuracy + `out/errors/<segment>.csv`. `scripts/submit.py` → leaderboard (needs `HK_TOKEN` env var; never
  write the token in files or commands).

## Commands
```
.venv/bin/python scripts/run.py --phase sample --out out/sub_sample.csv.gz && .venv/bin/python scripts/eval.py out/sub_sample.csv.gz --phase sample
.venv/bin/python scripts/run.py --phase dev --out out/sub_dev.csv.gz && .venv/bin/python scripts/eval.py out/sub_dev.csv.gz --phase dev --seg ar_ref_noise
.venv/bin/python scripts/run.py --phase live --out out/sub_live.csv.gz     # must print VALID
```
Use `--only <matcher...>` to run a subset while developing. Sample runs in ~15 s; dev in a few minutes.

## Conventions
- A matcher must never crash the run (the runner catches exceptions, but keep it robust) and must prefer
  precision: a wrong claim costs a pair FP + the line; an unclaimed line still gets the fallback.
- Amounts: item rows positive, in item currency. `PARTIAL`/`SHORT_DISPUTE` → amount actually applied; other
  "fully cleared" exceptions → full open amount; `RETURNED` → re-opened amount.
- Golden segments (`golden_allocations.segment`) name the case type; use `eval.py --seg <segment>` to see failures.
- Python via `.venv/bin/python` (polars, pypdf, openpyxl, rapidfuzz). No network calls in the pipeline.

## Fast iteration loop (use this, not full dev)
March window = representative of full dev (70.37 vs 69.88) and runs in ~110 s:
```
.venv/bin/python scripts/run.py --phase dev --start 2025-03-01 --end 2025-03-31 --out out/<you>/m.csv.gz
.venv/bin/python scripts/eval.py out/<you>/m.csv.gz --phase dev --start 2025-03-01 --end 2025-03-31 --seg <segment>
```
Baseline (2025-03): score 70.37, F1 0.718 (precision 0.98, **recall 0.57**), line acc 0.888, exception F1 0.392.
Losses are recall (multi-item cases not allocated) and missing exceptions, not wrong claims. Target ≥ 90.
Full dev (~22 min, `receipts` 830 s) only before the 17:00 freeze.

## Case → rule → trap (from hackathon.usetalky.com/ejemplos; POLICIES.md is the authority)
- ar_ref_noise: no invoice number in text; payer name + exact amount identify the single open item. Trap: leaving it unclaimed.
- ar_same_amount_fifo: several equal open items → oldest first. Trap: random pick / split.
- ar_1toN_remittance: remittance advice lists the invoices, one row each. Trap: subset-sum guessing.
- ar_1toN_credit_note: payment = invoices minus pending credit note; the credit note is its own row. Trap: PARTIAL on the invoice.
- ar_1toN_text: several numbers in the text (no prefix, truncated, continued in next 38-char record) sum to the cash.
- ar_instalment (PDF: several due dates), ar_retention / ar_advance_deduction (PDF %, PARTIAL for amount collected),
  ar_replaced_invoice (PDF: pay goes to the NEW invoice though text cites the old one).
- ar_partial: amount = collected, PARTIAL. ar_short_discount: item fully cleared + SHORT_DISCOUNT. ar_short_bank_charges: fully cleared +
  SHORT_BANK_CHARGES. ar_short_withholding: exception ONLY on the affected item row. ar_short_dispute: that row = amount really paid + SHORT_DISPUTE.
- ar_overpayment: item at its amount, excess OVERPAYMENT. ar_duplicate_payment: DUPLICATE_PAYMENT against the same (already paid) item.
- ar_fx: amount in INVOICE currency, FX_DIFFERENCE. ar_third_party_{parent,agency,confirming}: payer is not the customer.
- ar_dd_returned / ar_cheque_returned: negative line vs the original invoice, RETURNED. unidentified*: UNIDENTIFIED, never force by amount alone.

## Ownership (parallel agents; each writes only its own files and out/<name>/)
- fixer-noise: receipts.py (ar_ref_noise, same_amount, third_party) · fixer-multi: ar_multi.py + remittance.py (1toN*, n_to_m)
- fixer-short: shorts.py (short_*, dispute_settlement, fx, overpayment, duplicate) · fixer-pdf: pdfs.py (instalment, retention, advance, replaced)
- fixer-zero: returns.py (customer_refund/advance, cheque_returned, ap_duplicate_*, card_chargeback, credit_note_paid_out)
