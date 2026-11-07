# Team update for the 15:00 meeting

For Vitalii, Luke, Ivan, Marina.

## 1. A fair holdout to compare solutions

Luke built a holdout from TRAIN, using `solution/tools/make_holdout.py` (branch `solution/lukemininno`, commit 2a754cc).

- Window: 2025-04-01..2025-06-30, all 13 banks. 107,352 bank lines, 323,161 open items at start (real LIVE: 325,419).
- Layout is identical to the real data. `train/` is TRAIN strictly before 2025-04-01. `live/` is the window, with `open_items_at_start` computed from the golden.
- Every solution runs unchanged as `--phase live`. No solution sees the window's golden. Scoring uses the official scorer through `solution/tools/score_phase.py`.
- Caveat: opening balances are "oracle" (from the golden). This is a temporal test, not an official LIVE score.

| Solution | Sample | Holdout Q2-2025 | Runtime | Peak RAM |
|---|---|---|---|---|
| Vitalii (`python -m recon.run --phase live`) | 75.70 | 77.22 | 11 min | 5.98 GB |
| Ivan (eltriki-x, `scripts/run.py --phase live`) | 65.58 | 54.31 | 5 min | 5.30 GB |
| Luke (`solution/starter_plus.py`) | 53.51 | 49.32 | 3 min | 1.75 GB |
| Marina | no code on branch yet | | | |

Runtime and RAM were measured on Luke's 16 GB Windows laptop. Vitalii's latest official LIVE score on his branch is 84.06.

The sample is misleading. Ivan looked strongest on AR receipts on the sample (94%) but drops to 70% on the all-bank holdout.

## 2. Where others beat Vitalii (share of bank lines fully correct)

- Luke: direct-debit batches 85% vs 65%, returned direct debits 89% vs 62%, receipts after a return 85% vs 63%, replaced invoices 54% vs 10%, card chargebacks 40% vs 10%.
- Ivan: non-item categories learned from TRAIN history (cash pool 100% vs 50%, bank reversal 99% vs 50%, FX deal 100% vs 62%, taxes/social security 100% vs 80-92%), "month named in the text" 71% vs 58%, new IBAN 86% vs 83%.

Upper bound of grafting these into Vitalii (rows swapped segment by segment, official scorer): 77.22 -> 78.74 (Luke's direct-debit/returns rules) -> 78.90 (Ivan's categories) -> 79.02 (month rule, new IBAN) -> 79.14 (replaced invoices). That is about +1.9 at most.

The bigger gaps are where nobody is good yet:
- noisy-reference receipts: ~4,600 lines wrong even for Vitalii (80% correct)
- receipts net of a credit note: 29%
- many-to-many: 16%
- guarantee retentions: 46%
- short-paid disputes: 51%
- remittance advices: 71%

## 3. Proposed split

- Vitalii: his solution is the base. Work on the large margins above. Run and submit FINAL from his Mac, the only machine with enough memory headroom.
- Luke: grafts onto Vitalii in `solution/luke-on-vitalii`. **First graft done (commit ed79e69): mandate-based direct-debit returns** (Vitalii only found returns whose original batch was in the phase files; unreturned invoices were later flagged DUPLICATE_PAYMENT). Holdout **77.22 -> 78.16**, sample 75.70 -> 76.10, no segment worse, same unit tests passing. Next: replaced invoices (needs Vitalii's `pdf_facts`, currently off in the LIVE run). Document reader: the explorer found the hard segments fail on *linking*, not reading (advices are CSV/XLSX/text/PDF with text; an advice can arrive days after the cash), so a parser + linking fix beats an LLM.
- Ivan: graft his learned non-item categories and the month rule into Vitalii's base. Clean the data files committed to Git.
- Marina: owns the 19:45 project delivery: video (max 5 min, 720p), GitHub link with jury access (the repo is private), and a written description (key ideas, stack/models, next steps, team members).

Nothing is merged into main until the team agrees.

## 4. Three warnings

1. **FINAL memory.** Vitalii's solution peaked at ~6 GB on 3 months of data. FINAL has ~220k bank lines (about twice the holdout), so a 16 GB laptop is at risk. Run FINAL on Vitalii's Mac (LIVE took ~3 min there) and do a dry run on LIVE first. FINAL data lands at 18:00, submissions close 19:30, and there is only one scored FINAL upload.
2. **Committed data in Ivan's branch.** `solution/eltriki-x` has ~2,700 committed files under `bankrec-docs-and-masters/` (official docs/masters data). CONTRIBUTING says to keep bulk datasets out of Git. Remove them from the branch (`git rm --cached` plus `.gitignore`) before any merge.
3. **Use the holdout for every change.** Anyone can build it (~12 min, 0.7 GB RAM):
   ```
   python solution/tools/make_holdout.py --train <TRAIN dir> --masters <masters dir> --start 2025-04-01 --end 2025-06-30 --out outputs/q2root
   python solution/tools/score_phase.py <submission> --data outputs/q2root --phase live
   ```
   Compare every change on the same holdout, not on the leaderboard or the sample alone.
