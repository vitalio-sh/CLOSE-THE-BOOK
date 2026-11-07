# Competitor-idea prototypes: handoff to the coordinator

Author: Claude Code (Opus 5.5), 7 November 2026, 13:00–13:40 Madrid. The ideas come from `docs/research/COMPETITOR_CUENTAMINDS_2026-11-07.md`. All code is our own.

**Isolation.** All work is on branch `claude/competitor-ideas` in a separate worktree, `/Users/neo/CodeProejcts/ctb-claude-competitor`.
- The base is the accepted cumulative candidate `candidate/2026-11-07-r1-v5-on-t3a-r2` (`b49a623`).
- Nothing in the shared working tree, the Codex worktree, tags or the LIVE queue was touched.
- Nothing was uploaded or pushed.

**Harness.** Every run uses the evaluation lane's harness unchanged:
- prepared inputs `t3_{october,january}_pdf_inputs`;
- strict PDF mode with `shared_pdf_cache`;
- `compare_task.py` with the category and exception lists.

The baseline rerun from the worktree is byte-identical to `evaluation_lane/r1v5_on_t3a_{october,january}`. Outputs are under `outputs/review_coordination/claude_competitor_lane/`.

## Result: final commit `db75b01` vs accepted R1v5

| window | score | precision | recall | txn accuracy | exception macro-F1 | fixes / regressions |
|---|---:|---:|---:|---:|---:|---:|
| October | 83.52 → **88.08** (+4.56) | 99.729 % → 99.808 % | 77.00 % → 79.85 % | 87.87 % → 90.30 % | 0.6855 → 0.8316 | **806 / 8** |
| January | 87.45 → **91.36** (+3.91) | 99.688 % → 99.784 % | 86.83 % → 88.51 % | 90.34 % → 92.33 % | 0.6972 → 0.8377 | **286 / 2** |
| SAMPLE | 82.80 → **85.99** (+3.19) | 99.858 % → 99.889 % | 74.75 % → 78.51 % | 85.48 % → 87.50 % | 0.7205 → 0.7889 | **176 / 4** |

Precision rises in every window. The remaining regressions are:
- October: `ar_1toN_text` 3, and 1 each in `ar_1toN_credit_note`, `ar_dd_batch`, `ar_dd_represented_transfer`, `ar_dd_returned` and `ar_ref_noise`;
- January: `ar_1toN_text` 2;
- SAMPLE: `ar_1toN_text` 2, and 1 each in `ar_1toN_credit_note` and `ar_dd_represented_transfer`.

"Changed but still wrong" rows (471 / 92 / 33) are mostly outgoing unmatched lines whose category moved from UNIDENTIFIED to BANK_FEES. That is never worse, because UNIDENTIFIED is never right for a debit.

The previous commit `3604a75`, which lacks only the last return-guard relaxation, scored:
- October **87.77** (+4.25): 771 fixes / 30 regressions, precision 99.763 %;
- January **91.22** (+3.77): 282 fixes / 12 regressions.

LIVE artifact (validated, **not uploaded**):
- **File:** `outputs/review_coordination/claude_competitor_lane/live_final/submission.csv.gz`
- **SHA-256:** `9658a299c3e590177e150832371b903f16c89c4662c89e741a14e8b399ab8e79`
- **Source:** `db75b01aa85057d90f9e53f3539fb396d80c68ba`. Exact frozen source is in `live_final/source`.
- **Run:** the coordinator's LIVE command, strict PDF, `publication: validated`, both structure checks pass, no source or input change during the run, 544 s, dashboard publishing disabled.

What changed on LIVE inputs compared with the R1v5 LIVE artifact (input-only diagnostics, no score claimed):

| | R1v5 | final |
|---|---:|---:|
| unidentified fallback lines | 23,053 | 19,926 |
| German `RUECKLASTSCHRIFT` returns RETURNED / UNIDENTIFIED | 1,013 / 1,245 | 2,252 / 0 |
| DUPLICATE_PAYMENT rows (many were un-returned re-presentations) | 1,546 | 790 |
| SHORT_BANK_CHARGES rows | 28 | 128 |
| OVERPAYMENT rows | 244 | 370 |
| DD batch lines left unallocated | 57 | 1 |
| debits with category UNIDENTIFIED | 1,987 | 0 |

## Commits, one idea each, with measured step deltas

The steps are cumulative. Δ is the official score on October / January for the step alone.

| commit | idea | Oct Δ | Jan Δ | step fixes / regressions (Oct; Jan) |
|---|---|---:|---:|---|
| `0b4b02f` | German DD returns (`RUECKLASTSCHRIFT` was missing from the batch return gate); a return must be strictly after its collection and re-opens only the printed invoice; invoices printed in DD files of the last 6 months are restored at zero balance | +1.17 | +0.97 | 277/25; 134/10 |
| `a13bd5c` | SHORT_BANK_CHARGES also from bank_code `PMNT/RCDT/XBCT` | ┐ | ┐ | |
| `0e46e0a` | service-month parser: every keyword occurrence, abbreviations (`MANT. DIC 24`), IT/PT names, truncated months (`DICIEMB`) | │ +1.50 | │ +1.59 | 92/**0**; 35/**0** |
| `08b662b` | multi-invoice FX subsets on `orig_amount`; singular `COMPRA/VENTA DIVISA` → FX_DEAL | │ | │ | |
| `f0687fb` | unmatched outgoing lines → BANK_FEES (UNIDENTIFIED is receipt-only; TRAIN has none) | ┘ | ┘ | |
| `a7bbb93` | known-payer ladder: best-ranked payer for exact singles; FIFO-shaped exact sets (oldest-first prefix, due-date prefix, contiguous run nearest the due dates); signature overpayment (round amount, or exactly 1 % / 10 %) | +0.85 | +0.76 | 132/**0**; 44/**0** |
| `a0ca4f7` + `09a12fa` | exact-total batches keep resolvable rows (≤ half unknown); truncated-reference prefixes; gap fill after quoted invoices | +0.59 | +0.36 | 287/64; 74/26 |
| `427d8f4`, `1c487a2`, `3604a75` | uniform 6-month floor (windows are LIVE-faithful); a return quoting its batch id reverses that batch; prefixes only seed gap fills; credits only with their own larger invoice | +0.14 | +0.09 | removed most of the 64/26 |
| `db75b01` | the return guard accepts the explicit printed suffix (`RECIBO 0217685`) | +0.31 | +0.14 | removed 22 of the remaining 30 / 10 of 12 regressions |

Segment highlights, October baseline → `3604a75`:
- `ar_dd_returned` 332 → 518 of 584
- `ar_short_bank_charges` 2 → 20 of 27
- `ar_overpayment` 28 → 54 of 55
- `ar_ref_noise` 5,572 → 5,771
- `ap_payment_multi` 3,366 → 3,455
- `ar_same_amount_month` 699 → 741
- `nonitem_fx_deal` 22 → 35
- `ar_fx` 54 → 73

## Policy change the team must accept explicitly

Two existing unit tests encoded "abstain on any ambiguity". They were changed **on purpose** and must not be silently reverted:

- `test_ambiguous_subset_rejected` → `test_ambiguous_subset_takes_fifo_prefix`. Two exact sets are possible, and POLICIES §5.6 FIFO pays the oldest-first prefix. A non-FIFO free subset still abstains (`test_free_subset_without_fifo_shape_rejected`).
- `test_incomplete_group_rejected`. A batch with most rows unknown is still rejected; with at most half unknown, the resolvable rows are kept (`test_batch_keeps_resolvable_rows`).

There is no free subset-sum anywhere. CuentaMinds measured −0.5 for that.

## Known residual regressions (final commit)

There are 8 in October, 2 in January and 4 on SAMPLE (listed above). Most are `ar_1toN_text` lines where the gap fill or a FIFO-shaped set picked a different, equally exact set. The rest are DD cascades.

## Integration notes

- Files touched: `recon/batches.py`, `recon/history.py`, `recon/matching.py`, `recon/engine.py`, tests. Lane scripts are `scripts/claude_lane_{eval,compare,live}.sh`.
- `documents.py` is untouched. Advice rows already agree with golden (22,018 of 22,036 in October), so amount-based deduction classification was not worth a conflict with R8.
- Confirming (R7) was not touched. CuentaMinds' evidence suggests dropping R7's uniqueness proof (see the research doc §3, item 7).
- To integrate, cherry-pick the commits in order onto the current accepted candidate. Each commit is one idea with its own tests.

## Handoff to the main chat (sole LIVE uploader), prepared 13:30 Madrid

Everything `scripts/submit_live.py` needs is ready, and its own `--dry-run` returns `"status": "verified"`.

- **Candidate tag:** `experiment/2026-11-07-claude-competitor-ideas` → `db75b01aa85057d90f9e53f3539fb396d80c68ba`.
- **Baseline:** `candidate/2026-11-07-r1-v5-on-t3a-r2` (`b49a623`). This is the latest scored LIVE artifact (official 84.06), and its source hashes match the commit.
- **LIVE artifact:** `outputs/review_coordination/live_claude_competitor_ideas/submission.csv.gz`, SHA-256 `9658a299c3e590177e150832371b903f16c89c4662c89e741a14e8b399ab8e79`.
  - Generated with `scripts/run_experiment.py --phase live`, strict PDF; `experiment.json` is `verified` with `inputs_changed: false`.
  - The bytes are identical to an independent earlier run, so the output is deterministic.
  - The link points to `claude_competitor_lane/live_db75b01`.
- **Handoff file:** `outputs/review_coordination/live_claude_competitor_ideas/handoff.json`.
- **Comparison report:** `docs/reviews/CLAUDE_COMPETITOR_IDEAS_VALIDATION_2026-11-07.json`.
  - It was produced by `compare_three_superset.py`, a verbatim copy of `compare_three.py` with one documented deviation.
  - The identical-PDF assertion is replaced by a strict-superset check: every baseline PDF input and fact is present and byte-identical. The candidate restores batch-referenced closed invoices, which adds about 1,000 required PDFs per window. SAMPLE PDF evidence is identical.
  - Tagged source hashes are verified in all three windows.
- **Evidence:** `outputs/platform/evidence/claude_competitor_ideas.json`.

Upload command (main chat only, without `--dry-run`; FINAL is not authorized):

```sh
python3 scripts/submit_live.py --submission outputs/review_coordination/live_claude_competitor_ideas/submission.csv.gz --candidate experiment/2026-11-07-claude-competitor-ideas --evidence outputs/platform/evidence/claude_competitor_ideas.json
```
