# bankrec (Close the Books hackathon)

Rule-based bank reconciliation engine. Entry point `run.py`; engine in `recon/`; official scorer
`recon/bankrec_score.py` (same code as the server). Data in `data/{masters,sample,train,live,final}`.

Non-negotiable:
- Decide on improvements with the **TRAIN gate**, not the sample: skill `bankrec-eval`
  (`python tools/eval.py --tag X && python tools/gate.py out/eval/X`).
- Improvements follow skill `bankrec-improve`; data facts and failed experiments are in `bankrec-knowledge`.
- Submissions follow skill `bankrec-submit`. FINAL is a single shot.
- Agents: `bankrec-evaluator` (measure), `bankrec-error-analyst` (what next), `bankrec-rule-improver`
  (one change, gated), `bankrec-submission-guard` (LIVE/FINAL go/no-go).
- Never `git add -A` (data caches live in `models/` and `out/`). Never run two TRAIN evaluations at once.
