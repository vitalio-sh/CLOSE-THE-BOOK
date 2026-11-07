# Claude Code implementation review — 7 November 2026

Model: `claude-opus-5-5`, effort `xhigh`, read-only review. Original response archived from `outputs/reviews/code-review.json`; the complete review text below is unchanged. The reviewer did not execute tests or inspect compressed bank/label data, and several findings describe code before subsequent fixes. This is review input, not proof that each suggested fix is correct.

## Disposition after reproduction and tests

| Finding | Decision | Current behavior or remaining limitation |
|---|---|---|
| H1 — partial groups | Deferred; proposed shortcut rejected for release | Returning an arbitrary 80% of a group changes balances used by later matches and can hurt later rows. Complete evidence-backed groups remain the conservative default; a measured partial-group experiment is separate work. |
| H2 — confirming maturity | Proposed due-date regrouping disputed | Actual transfers include invoices with different invoice due dates. The bank's confirming maturity is not reliably the invoice due date. Multi-beneficiary maturity subgroup recovery remains incomplete. |
| H3 — closed historical items | Partially addressed | LIVE/FINAL restore explicitly referenced historical identities absent from the authoritative opening snapshot at zero balance. This supports returns and guarded duplicates without reopening old debts. Unreferenced historical re-presentations remain limited. |
| H4 — invoice PDF facts | Addressed with bounded opt-in mode | CLI `--pdf-facts` attaches the provider and reports budgets/statistics. PDF mode remains off by default: the measured sample run showed no score gain. A full unrestricted parallel PDF scan and additional service-month heuristics are deferred. |
| H5 — bank charge shortfall | Addressed conservatively | Requires an AR receipt from outside SEPA, transfer evidence, same currency, and a shortfall at most 2% and EUR 60 equivalent. Dated reference FX rates are used for the cap. |
| M1 — unidentified fallback | Category-guessing proposal rejected | An unexplained debit is not assigned a fabricated expense category. Unknown rows remain explicitly unidentified. |
| M2 — duplicate evidence | Hardened | Exact reference, prior payment amount and/or a restored closed identity with corroborating party and plausible total provide evidence. Batch duplicate status requires zero balance. This does not reconstruct every pre-phase payment history. |
| M3 — FINAL masters | Addressed | FINAL overrides apply per file; missing files fall back to base masters. Empty categories/payment terms fail before inference. Reports include file coverage and opening AR term enrichment coverage. |
| M4 — repeated batch rows | Addressed | Each batch uses a local reserved-balance map before central balance application. Repeated explicit documents can still aggregate valid partial rows. Regression tests cover both cases. |
| M5 — inferred FX | Broad conversion deferred | Original-currency evidence remains required for allocation. Dated reference rates are used for the bank-charge EUR cap. A reference-rate estimate alone does not establish the settlement amount. |
| M6 — category preemption | Addressed | Category expressions have token boundaries, use bank descriptions/references, and omit party names/accounts. Generic refund wording no longer preempts an APC allocation. |
| M7 — evaluation realism | Limitation accepted and documented | SAMPLE is a development set with no authoritative opening snapshot. October uses oracle prior bank-settlement balances and frozen older code, not a pure end-to-end holdout. It does not establish LIVE accuracy. A multi-month production-equivalent holdout is deferred. |
| L1 — output/parser robustness | Addressed | Stable gzip header, temporary candidate, validation/source checks before publish, report/audit hashes, report-last publication and exception rollback. Empty CSV advice is skipped and parser statistics are reported. |

Additional reproduced defects fixed independently: future collection/execution dates cannot support returns; a final one-cent balance is not a duplicate payment; FX credit-note handling preserves its explicit exception rules. Details are covered by the regression suite.

There is no claim that local review or tests establish hidden LIVE/FINAL accuracy. No generated release file has been uploaded by this workflow.

## Original reviewer response

Source JSON SHA-256: `856ba9c632a4aac88393a97c84da1f36203233d84e694dfa4af8312e0fcc14a7`.

The pipeline is valid and precise, but it misses too much. On the June-2025 sample, allocation precision is 0.997 and recall is 0.683. 1,295 of 8,499 lines (15.2%) fall back to `UNIDENTIFIED`, but only 218 really are unidentified. So about 1,077 lines, roughly 75% of the 1,430 wrong lines, are lines the pipeline gave up on, not wrong matches. LIVE shows the same pattern: 27,007 of 203,083 lines (13.3%) are `UNIDENTIFIED`.

The largest gains are recall and exception-class fixes, plus an evaluation set that actually behaves like LIVE/FINAL. I found no label leakage in `recon/`: nothing in it reads `golden`, `segment` or `difficulty`.

## Summary

| # | Severity | Finding |
|---|---|---|
| H1 | High | Batches and remittance advices are all-or-nothing: one unresolved row loses every pair in the group |
| H2 | High | Confirming debits are grouped per beneficiary, but the bank debits once per maturity date |
| H3 | High | Items already closed at phase start are never loaded, so returns and re-presentations across the phase boundary fail |
| H4 | High | `PdfFacts` is never attached in production; the replacement and service-month logic never runs |
| H5 | High* | `SHORT_BANK_CHARGES` is never emitted |
| M1 | Medium | `UNIDENTIFIED` is the fallback for every unmatched line, debits included |
| M2 | Medium | `DUPLICATE_PAYMENT` is emitted without evidence, with no amount cap |
| M3 | Medium | `masters_final` is read all-or-nothing (FINAL-day risk) |
| M4 | Medium | Rows in one batch don't use up balance, so two rows can resolve to the same invoice |
| M5 | Medium | `fx_rates` is loaded but never used |
| M6 | Medium | The category classifier runs before matching and uses unanchored substring patterns |
| M7 | Med-High | Neither the sample nor the Oct-2024 window evaluation reproduces LIVE/FINAL conditions |
| L1 | Low | Output provenance and robustness gaps |

\*If the class exists in the golden labels.

## Findings

**H1. All-or-nothing group allocation is the main recall loss**
- **Where:** `recon/batches.py:168-170` (`if item is None: return None`); `recon/documents.py:208-209, 212-213, 216-217`.
- **Evidence:**
  - `verified_sample_score.txt`: precision 0.997, recall 0.683.
  - Amount-weighted accuracy is 0.630 against 0.832 transaction accuracy, so large multi-item lines fail more often.
  - `ar_dd_batch` 0.792 (n=72) and `ar_n_to_m` 0.098 (n=102).
  - A failed group falls straight through to `UNIDENTIFIED`.
- **Fix:**
  - Once a group is identified by exact total plus account and date/reference, emit the rows that did resolve, for example when ≥80% of the group amount is resolved.
  - F1 scores each pair independently and the line is wrong either way, so this can only help.
  - Write a decline reason to `audit.jsonl` (e.g. `batch:unresolved 3/212`) so the losses can be measured.

**H2. Confirming runs are grouped by `end_to_end_id`**
- **Where:** `recon/batches.py:61-64`. Line 78 (`key[1] if isinstance(key, tuple)`) is dead code; it looks like a planned (run, maturity) key that was never written.
- **Evidence:**
  - `DATA_DICTIONARY.md:85-86` says confirming means one debit per maturity date.
  - `data/raw/live/live/payment_runs/ES02_202510_0.csv:945-969`: run `CF020025101023` has 17 or more beneficiaries. A debit covering several of them can't equal any single per-beneficiary total, which `batches.py:221` requires.
  - The only test uses a one-row run (`tests/test_batches.py:57-60`).
  - Sample `ap_confirming` accuracy is 0.373 (n=67).
- **Fix:**
  - Group confirming rows by (`run_id`, the item's `due_date`) and date the group at `due_date`.
  - As a fallback, search for a subset of the run's beneficiary groups that adds up to the debit.
  - Add a test with several beneficiaries.

**H3. Items closed at phase start are never loaded (time-boundary error)**
- **Where:** `recon/context.py:46-54` (only opening IDs are enriched); `recon/validate.py:35` rejects unknown items; `re_presentation` is never read anywhere in `recon/`.
- **Evidence:**
  - A late-June (or late-December) collection that is returned in July (or January) refers to an item that isn't in `ctx.items`. `_resolve` returns `None`.
  - A re-presented receipt for that item then sinks the whole later DD batch, because of the all-or-nothing rule in H1.
  - Returns are 2.4% of sample lines (`ar_dd_returned`, n=202). FINAL's first weeks are exposed to this.
- **Fix:**
  - Load items from the history phases that were issued in the last ~180 days and are missing from `open_items_at_start`, with `open_amount=0` and a `closed_at_start` flag.
  - Only allow those items on the RETURNED, re-presentation and DUPLICATE paths.
  - In `_allocate`, a row with `re_presentation=='Y'` gets an empty exception and must never become `DUPLICATE_PAYMENT`.

**H4. The PDF-based rules never run in production**
- **Where:** `recon/run.py:13-25` never sets `ctx.pdf_facts`, so `recon/matching.py:185-187` and `253-261` never execute. Tests inject a fake (`tests/test_matching.py:62-69`) and pass anyway. The instalment, retention and advance facts (`recon/pdf_facts.py:54-66`) have no consumer.
- **Evidence:**
  - POLICIES §5.6 and §5.7 depend on the PDFs.
  - `ar_same_amount_month` 0.832 (n=185); "old cancelled invoice number" falls under `ar_ref_noise` 0.836 (n=2,155).
  - Wired in as it stands, the defaults (`max_seconds=30`, `max_documents=4000`, `pdf_facts.py:74-75`) can't cover the 11,340 invoice and credit-note PDFs in `live/pdfs/index.csv`. `_scan_party` reads up to 64 PDFs per party, so the budget runs out early, and `stats` are never reported.
- **Fix:**
  - Fill the cache in one parallel `pdftotext` pass, then attach `PdfFacts` with no time budget and put `stats` in the report.
  - A rule that needs no PDFs: §5.6 says monthly fees are invoiced at the start of the following month, so for service month M pick the exact-amount candidate issued in M+1.
  - Also accept a month with no year ("SERVICIOS MAYO"); the regex at `matching.py:256` requires a year.

**H5. `SHORT_BANK_CHARGES` is never emitted**
- **Where:** it appears only in the allowed-values list at `validate.py:7`. In `matching.py:108-134` a bank-charge shortfall falls through to `PARTIAL` with amount = cash.
- **Impact:**
  - The pair amount is wrong whenever the shortfall is more than 0.5%, so it counts as both a false positive and a missed pair.
  - It adds a false positive to `PARTIAL`.
  - The class gets F1 = 0, worth up to about 2 score points (one of about 10 classes averaged into the 20% macro-F1 weight).
- **Fix:**
  - First count the class in the TRAIN golden.
  - Then, before the `PARTIAL` fallback: same currency, payer outside SEPA, shortfall ≤ min(60 EUR equivalent, 2% of the open amount) → (`SHORT_BANK_CHARGES`, open amount).

**M1. `UNIDENTIFIED` is the fallback for everything** (`run.py:39-40`)
- **Evidence:** 1,295 predicted against 218 in the golden. All 218 were hit, so class precision is about 0.17 and F1 about 0.29, inside a macro-F1 of 0.612.
- **Fix:**
  - Give the `UNIDENTIFIED` exception only to credits with no party candidate. POLICIES §3 defines it as a receipt.
  - For debits, emit the most likely category with an empty exception.
  - Measure the change on the sample first.

**M2. `DUPLICATE_PAYMENT` without evidence**
- **Where:** `matching.py:110` flags any referenced item with open ≤ 0, using amount = cash. `context.py:88` exempts DUPLICATE from the amount cap. The overpayment guard at `matching.py:241` only applies when open > 0. `batches.py:178` does the same.
- **Effect:** any earlier mistake, including a missed return, turns the next real payment into a false positive in a rare class (n=32 in the sample).
- **Fix:**
  - Require prior payment history and cash ≈ total, as `matching.py:304` already does.
  - Cap the amount at the total.
  - Skip when the item's last history entry is RETURNED.

**M3. `masters_final` is read all-or-nothing** (`context.py:26-28`; `read_csv` silently yields nothing for a missing file, `context.py:16-17`)
- **Risk:** if FINAL ships only the changed master files:
  - categories come back empty, every category row fails `validate.py:43`, and the run exits only after the full pass;
  - payment terms come back empty and `SHORT_DISCOUNT` disappears silently.
- **Fix:**
  - Fall back per file: `masters_final/<f>` if it exists, otherwise `masters/<f>`.
  - Assert that categories and payment terms are non-empty.
  - Report the share of opening AR invoices that got `payment_terms`. Enrichment silently depends on `data/workspace/train` being present (`context.py:29, 49-54`).

**M4. Rows in one batch don't use up balance** (`batches.py:153-162, 184-185`)
- Two same-amount rows for one debtor with no usable number both pick the oldest invoice (FIFO) and get summed.
- The doubled amount is then rejected (`context.py:88`), and the whole batch is lost.
- **Fix:** pass a per-batch `consumed` map into `_resolve`.

**M5. FX conversion is missing** (`context.py:38-40` loads the rates; nothing reads them)
- `_cash` (`matching.py:101-106`) and `documents.py:190-194` need `orig_amount` in the item currency. POLICIES §3 says the bank shows the original amount "and/or rate".
- **Fix:**
  - Convert at the booking-date reference rate, using the last available rate on weekends and holidays.
  - Accept a result 0–3% worse than reference, and require party evidence.
  - The same conversion is needed for the 60 EUR limit in H5.

**M6. The classifier preempts matching** (`run.py:24-25`, `matching.py:164`)
- Patterns run without word boundaries over the counterparty name and IBAN (`matching.py:28, 138-160`):
  - `FRAIS` matches a payer named "Fraisse" and labels it `BANK_FEES`.
  - `PRES [A-Z]{2}[0-9]` matches "…EXPRES ES91…" and labels it `LOAN_REPAYMENT`.
  - `REEMBOLSO` turns a supplier's refund of a credit note into `SUPPLIER_REFUND`, but §4 says it settles the APC item.
- **Fix:**
  - Add `\b` word boundaries.
  - Exclude the counterparty name and account from the category patterns.
  - Try reference matching first for refunds.
- **Verify:** count category-module lines on the sample whose golden has items.

**M7. The evaluations are misleading**
- **Sample:**
  - There is no `open_items_at_start` in `data/raw/docs/sample/`, so `context.py:70` sets open = total.
  - The "remainder" segments therefore look broken when they aren't: `ar_retention_release` 0.000 (n=44) and `ar_dispute_settlement` 0.038 (n=53). The code emits `PARTIAL` because it thinks the whole invoice is still open.
  - Tuning against those segments would hurt LIVE.
  - The sample is also the tuning set.
- **Window eval** (`scripts/evaluate_window.py`, the 79.23 result):
  - It loads all 4.6M items, closed ones included (lines 98-105), and replays the golden history into `ctx.history` (119-120). Returns and duplicates that are impossible in LIVE (H3) become possible.
  - It skips `proposal_valid` (132-134), so it tests different control flow from `run.py:36-37`.
  - It covers one month, not six.
  - Peak memory was 15.7 GB (`window_oct2024.log:30`).
- **Fix:**
  - Write a synthetic `open_items_at_start.csv.gz` for 2025-01-01 from the TRAIN golden (positive balances only, official columns).
  - Put the Jan–Jun 2025 inputs in a phase directory and run `recon.run` unchanged.
  - Score each month, and keep one month you never look at.

**L1. Provenance and robustness**
- `submission_live.csv.gz.report.json` has no `source_hashes`, so it was built by older code than the 77.71 run. Regenerate it before reading anything into the leaderboard.
- The report's `sha256` covers the gzip bytes, which include a timestamp. That's why `verified_sample` and `combined_sample_v2` have identical counts and scores but different hashes. Use `mtime=0` or hash the decompressed content.
- The `except` at `documents.py:156` doesn't catch `IndexError`. An empty CSV (`documents.py:84`) would crash the whole run.
- Parse errors are collected (`documents.py:142`) but never reported.
- Output is not written atomically (`run.py:30`).

## Questions to settle from TRAIN golden (5-minute checks)
- **FX plus overpayment:** `matching.py:113` emits `FX_DIFFERENCE`, but §3's list of exceptions FX replaces doesn't include `OVERPAYMENT`.
- **Cancelled invoices within a phase:** the cancelled invoice X and its credit note both stay open, so a payment citing X goes to X. `related_document` is unused anywhere in `recon/`. It could close X and its credit note at load time, and help with credit-note netting (`ar_1toN_credit_note` 0.296, n=199).
- **Multi-invoice subset search:** it skips parties with more than 24 open items (`matching.py:275`) and only tries subsets of 2–3 items. This hurts `ar_1toN_text` (0.596) and `ar_n_to_m`.

## Suggested order for today
1. Low-risk recall fixes: H1, M4, H2, and the `re_presentation` part of H3.
2. FINAL safety: M3 and the L1 robustness items.
3. Exception classes: H5, M1, M2.
4. Service month (H4, with or without PDFs).
5. If time allows: the rest of H3, M5, and the M7 holdout.

Gate each step on the sample, and ignore the remainder segments there.

I didn't run any code or tests. I couldn't read the `.gz` contents (bank text, golden labels, sample invoices) with read-only tools, so the impact estimates come from the existing score and report files, not fresh runs.
