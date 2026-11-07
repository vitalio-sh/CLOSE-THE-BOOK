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
