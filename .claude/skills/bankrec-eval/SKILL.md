---
name: bankrec-eval
description: Run the bank-reconciliation pipeline on sample or dev, score it with the official scorer, and show the worst segments with wrong examples (ours vs golden). Use after every matcher change, or to decide what to work on next.
---

# bankrec-eval

1. Pick the phase: `sample` (1 month, ~15 s) for quick checks, `dev` (2025-01..06, minutes) before merging or
   submitting. Optional `--only m1 m2` to run a subset of matchers.
2. Run:
   ```
   .venv/bin/python scripts/run.py --phase <phase> --out out/sub_<phase>.csv.gz [--only ...]
   .venv/bin/python scripts/eval.py out/sub_<phase>.csv.gz --phase <phase> [--seg <segment> --n 10]
   ```
   `run.py` must print `VALID`. If it prints `!! matcher X failed`, fix that first.
3. Report back, compactly:
   - score and the three components (allocation F1 with precision/recall, txn accuracy, exception macro-F1);
   - the delta vs the previous run if one is known (keep a line in `out/score_log.txt`: date, phase, score, note);
   - top 5 segments by wrong lines and the per-matcher accuracy table (a matcher with acc < 0.9 is hurting precision);
   - for the segment in focus, 3–5 wrong examples and the hypothesis of what rule is missing (cite POLICIES.md §).
4. Append `<time> <phase> <score> <note>` to `out/score_log.txt`.

Do not submit to the leaderboard from this skill; submissions are a human decision (`scripts/submit.py`).
