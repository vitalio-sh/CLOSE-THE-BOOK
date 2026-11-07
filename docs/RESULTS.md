# Measured results — 7 November 2026

These are local measurements, not submitted LIVE or FINAL scores.

| Check | Result | Meaning |
|---|---:|---|
| Organizer starter on sample | 11.28 / 100 | Reference baseline |
| Combined solver on sample | 77.71 / 100 | Development-set result, 8,499 transactions |
| October 2024 chronological window | 79.23 / 100 | 32,859 transactions; prior-state caveats below |
| First full LIVE generation | 203,083 / 203,083 covered | Structure only; no accuracy labels available |
| First LIVE runtime | 193.22 seconds | Local machine, before later historical-catalog enhancement |
| First LIVE uncompressed size | 22,619,597 bytes | Below official file limit |

Sample allocation precision 99.73%, recall 68.28%, transaction accuracy 83.17%. This sample was used during development; it is not an unbiased estimate of generalization. Optional bounded PDF facts produced the same sample score in the measured run, so PDF mode remains opt-in.

## October window assumptions

`scripts/evaluate_window.py` freezes the source used for the experiment. Invoice issue dates are limited to the end of the window. Golden rows strictly before October 1 reconstruct opening bank-settlement state. Inside October, balances follow predictions, not target labels. October labels are loaded for scoring only after the output file is closed.

This is not a pure end-to-end holdout: prior settlements are oracle state, non-bank cancellations are not reconstructed, and organizer masters are later snapshots. The test used 4.65 million invoice/credit records and about 15.76 GB peak memory; it is heavier than LIVE inference. Total runtime 112.93 seconds, inference 50.69 seconds.

Allocation precision 98.96%, recall 73.13%, transaction accuracy 87.03%. Weak areas include multi-party confirming, N:M payments, credit-note netting and retention. No score guarantees for LIVE/FINAL follow from these measurements.

## Review and verification

Claude Code was invoked explicitly as `claude-opus-5-5`, effort `xhigh`, read-only tools. The first architecture review is archived in `docs/reviews/CLAUDE_ARCHITECTURE.md`; it began before the implementation existed. Review findings are checked before adoption.

Central safeguards reject future-dated invoice proposals, duplicate rows and over-allocation beyond a cent (except returned/duplicate payments). The runner applies balances centrally and uses both independent checks and the organizer parser. The organizer parser runs with empty labels for LIVE format validation; its fabricated score is discarded. The report labels this as format-only.

## Remaining work

- Get an official LIVE score using the team's token; generated files have not been submitted.
- Improve weakest segments using measured feedback.
- FINAL data is not yet published; execute and validate on release.
- Create the final project video using real results, give the jury repository access, and submit the project materials.
