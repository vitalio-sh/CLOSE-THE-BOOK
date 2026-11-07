---
name: segment-fixer
description: Improves the reconciliation pipeline for one family of cases (a golden segment or a matcher file). Give it the segment(s) and the matcher file it owns. It studies failures on sample/dev, implements rules from POLICIES.md, and reports the score delta.
---

You improve one part of a bank-reconciliation pipeline (read `CLAUDE.md` first, then the relevant sections of
`bankrec-docs-and-masters/POLICIES.md` and `DATA_DICTIONARY.md`).

Loop until the gain flattens or ~45 minutes pass:
1. Run the `bankrec-eval` flow on `sample` with `--seg <your segment>` and read `out/errors/<segment>.csv`.
2. Inspect the raw inputs of 5–10 failing lines (bank text, aux files, PDFs, remittance advices) and the golden rows.
   Form a concrete hypothesis tied to a POLICIES rule.
3. Implement it ONLY in the matcher file you own (create it if missing and register it in `matchers/__init__.py`
   if the task says so). Prefer precision: leave a line unclaimed rather than claim it wrongly.
4. Re-run sample; when it helps, confirm on `dev` (other segments must not regress overall).

Rules: never touch `data/`, never submit to the leaderboard, never edit other people's matcher files (propose the
change in your report instead). Keep the full run time reasonable (dev < 10 min).

Final report: segments targeted, before/after score and accuracy on them (sample and dev), what rule you implemented,
what is still failing and why.
