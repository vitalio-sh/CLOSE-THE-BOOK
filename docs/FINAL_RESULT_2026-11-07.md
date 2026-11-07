# FINAL result (Team 10, 7 Nov 2026)

Official FINAL scores (organizer API, the best attempt counts):

| Attempt | Time (Madrid) | Artifact | FINAL score | Exception macro-F1 |
|---|---|---|---|---|
| 1 | 18:41 | tag candidate/2026-11-07-team-r21 (90a6560) | 95.13 | 0.9239 |
| 2 | 19:03 | tag candidate/2026-11-07-team-r22 (41e8c2c) | 95.66 | 0.9458 |
| 3 | 19:21 | **r25** = tag candidate/2026-11-07-team-r23 (d6945bb) + `scripts/final_postprocess/` | **95.94** | **0.9586** |

r25: allocation F1 0.952 (precision 0.9983, recall 0.9099), transaction accuracy 0.9722, 764,552 rows covering all 220,256 FINAL bank lines.
Final LIVE result: r19 (c480497) = 95.27.

## Reproduce r25
```sh
# 1) solver output for FINAL (strict PDF facts; same flags as the LIVE releases)
python3 scripts/run_experiment.py --data <workspace with final/> --phase final --pdf-facts --require-pdf-complete --pdf-prewarm \
  --pdf-max-documents 200000 --pdf-max-party-documents 200000 --pdf-budget-seconds 1800 --pdf-cache-dir <cache> --out-dir out/final_r23
# 2) deterministic post-processing (no labels used)
RATIOS=0.95,0.80,0.97,0.90,0.70 python3 -I scripts/final_postprocess/relabel_disputes.py out/final_r23/submission.csv.gz \
  out/final_r23/submission.csv.gz.audit.jsonl <workspace>/final r24.csv.gz
python3 -I scripts/final_postprocess/rule_c_retention.py r24.csv.gz <workspace>/final r25.csv.gz
python3 -I scripts/final_postprocess/validate_final_csv.py r25.csv.gz <workspace>/final <workspace>/final/masters_final
```
Expected sha256 of r25: c90fa67d6611f841ede63d12e758ca261fb7b64f73a82ec3ca4c3a20112fb2f3 (input archive sha256 04083d6a…).

## What the post-processing does (POLICIES §3; TRAIN evidence)
- Rule A: remittance-advice (documents) rows labelled PARTIAL at exactly 95/80/97/90/70 % of the invoice become SHORT_DISPUTE. The changed
  advice layout drops the reason column; in TRAIN these ratios are disputes, and documents never emits PARTIAL there.
- Rule B: a later "SETTLEMENT <invoice>" line paying exactly the deducted share means the earlier deduction was a dispute (3,707/3,707 in TRAIN).
- Rule C: retention releases carry no exception.
These rules are not yet in `recon/`; porting them into the solver is the next step.
