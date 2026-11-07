# Validation of review/team-integ-r6 (1535933) — Luke, 2026-11-07

Read-only checkout of r6; nothing on r6 was modified. Windows laptop (16 GB). The Unix-only `resource` module
imported by `recon/run.py` (peak-RSS report only) is replaced by a stand-in in `outputs/analysis/shims/` via
PYTHONPATH (not in the repo). PDF text via `pdftotext` = xpdf 4.06 (Git Bash), not poppler.

Strict PDF flags (from `scripts/lead_release.sh`): `--pdf-facts --require-pdf-complete --pdf-prewarm
--pdf-max-documents 200000 --pdf-max-party-documents 200000 --pdf-budget-seconds 1800 --pdf-cache-dir <dir>`.

## 1. Baseline scores (official scorer, `solution/tools/score_phase.py`)

| Run | Data root | Score | Runtime | Peak RAM |
|---|---|---|---|---|
| r6, sample, strict PDF | `data/raw/docs` (no train/) | 86.23 | 94 s | 0.37 GB |
| r6, sample, strict PDF | masters + train/ + sample/ (like data/workspace) | **87.52** | 96 s | 4.18 GB |
| r6, Q2-2025 holdout, **no PDF** | `make_holdout.py` root, `--phase live` | **89.14** | 9.3 min | 5.27 GB |
| r6, Q2-2025 holdout, strict PDF | same | stopped (superseded by r9) | | |

For reference on the same holdout: Vitalii 270d8cd 77.22; 270d8cd + Luke's ed79e69 graft 78.16.
Team-reported SAMPLE 89.59 is not reproduced here (87.52 at best); unexplained, candidates: PDF text extraction
(xpdf vs poppler), harness/config differences.

## 2. DD returns (segment `ar_dd_returned`, Q2 holdout, r6 no PDF) — `dd_returns_report.py`

| Reason code | Lines | Correct | Wrong | Abstained |
|---|---:|---:|---:|---:|
| AM04 | 765 | 762 | 2 | 1 |
| MS02 | 301 | 297 | 3 | 1 |
| none | 177 | 176 | 1 | 0 |
| **MD06** | 159 | 131 | **27** | 1 |
| AC04 / AC06 / MS03 / SL01 / AM05 | 292 | 292 | 0 | 0 |
| MD01 / AC01 / MD07 | 141 | 138 | 1 | 2 |
| **All** | **1,835** | **1,796 (97.9%)** | **34** | **5** |

- The 5 "abstained" lines are classified `BANK_FEES` (not left unidentified), e.g. `DEVOLUCION RECIBO AM04 REM
  05250404974 | MANDATO VTM-ES-0005189`.
- Downstream damage: **25 later collections submitted as DUPLICATE_PAYMENT** because the golden invoice was not
  re-opened (missed re-open). No measurable damage from inflating the wrongly returned invoice. All false
  PARTIAL/DUPLICATE on later DD collections: ar_dd_batch 19 DUP + 8 PARTIAL, ar_dd_represented_transfer 8 DUP + 1 PARTIAL.
- Segment accuracy: ar_dd_batch 755/780, ar_dd_represented_transfer 470/489, ar_dd_returned 1796/1835
  (270d8cd + ed79e69: 671 / 441 / 1588) — r6 is clearly better than ed79e69; do not extend ed79e69.

### Why MD06 fails (`md06_days.py`, `md06_window.py`)
- Wrong MD06 picks: the golden collection is **49–70 days** before the return, r6 picks the latest collection older
  than 21 days, i.e. the **next monthly collection at 21–42 days** (same customer, same recurring amount).
- Golden collection age over all 159 MD06 returns is spread uniformly over 21–70 days (p10 25, median 45, p90 66):
  no fixed window separates them.
- 94/159 MD06 returns have one same-mandate, same-amount collection in 21–75 days (unambiguous); 65 have 2–3.
  Where measurable, "latest" is right 23 times, "oldest" 9 times. Abstaining would not avoid the downstream damage
  (the golden invoice still stays closed). A non-date signal is needed (e.g. `re_presentation=Y` linkage).

## 3. Unit tests (`test_r6_dd_returns.py`, against r6 1535933): 8 pass, 1 fails

- PASS: AM04 reverses the latest collection; MD06 skips the last 3 weeks with multi-row batches; named batch wins;
  same-day two same-amount collections → abstain; repeated returns never push the balance above the total;
  second return hits the re-presented collection; transfer after a return is a normal payment; no original
  collection → abstain.
- **FAIL**: MD06 with **single-collection batches** reverses the collection of 3 days ago: the reverse batch-total
  match (3-day window, `ranked` path in `BatchMatcher.reconcile`) runs before the MD06 21-day rule. Not observed in
  the Q2 holdout (no wrong pick younger than 21 days), so low impact; a precedence bug nonetheless.
  Fixed in the team DD lane (dcb125e).
- Value of the fix on the Q2 holdout (`md06_single_row.py`, data only): **0 of 159 MD06 returns** hit a single-row
  batch; in fact **no direct-debit batch has a single row** (train 11,725 + window 780 batches, all multi-row), and no
  single-row same-total batch exists 0-3 days before any MD06 return. r6 (pre-dcb125e) wrong/abstained on MD06: 28,
  of which 0 due to this bug. Expected score impact on data shaped like TRAIN/LIVE: none (robustness only).

## 4. Replaced invoices (`ar_replaced_invoice`)

| Run | Correct |
|---|---|
| sample, r6 strict PDF | **12 / 12** (270d8cd without PDF: 10 / 12) |
| Q2 holdout, r6 no PDF | 12 / 130 |
| Q2 holdout, r6 strict PDF | not measured: run stopped at 16:20, superseded by review/team-integ-r9 (65d71aa) |

## Reproduce
```
python solution/validation/dd_returns_report.py --data outputs/q2root --phase live --sub outputs/runs/r6_q2.csv.gz
python solution/validation/md06_window.py outputs/q2root
python solution/validation/md06_single_row.py outputs/q2root outputs/runs/r6_q2.csv.gz
R6_ROOT=outputs/worktrees/r6 python -m unittest solution/validation/test_r6_dd_returns.py -v
```
