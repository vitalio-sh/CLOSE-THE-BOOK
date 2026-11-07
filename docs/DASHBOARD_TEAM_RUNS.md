# Share a run with Team 10

The dashboard has two different records: **official LIVE submissions**, scored by the organisers, and **team runs**, which contain local experiment summaries. Sharing a team run does not submit it to the hackathon and does not replace the official best score.

Dashboard: https://valtermia-team10-reconciliation.peakedalbatross1.chatgpt.site

Keep your solver on your own `solution/<username>` branch. The separate `dashboard/runs` branch contains small JSON summaries and the publishing workflow. The helper uses a temporary clone; it never switches, resets or commits your current working branch.

## Share a validated run

Use Python 3.9 or newer, Git and your existing GitHub write access to the private team repository. From your repository checkout, get the standalone helper without touching your solver files:

```sh
git fetch origin dashboard/runs
git show origin/dashboard/runs:scripts/share_dashboard_run.py > /tmp/share_dashboard_run.py
```

Use `python3 /tmp/share_dashboard_run.py --repo "$PWD"` in place of `python3 scripts/share_dashboard_run.py` below if your branch does not already include the helper. Relative report/evaluation paths are resolved against the repository root. You do not need to merge another solver branch.

Preview exactly what will become public:

```sh
python3 scripts/share_dashboard_run.py \
  --report outputs/my_sample/submission.csv.gz.report.json \
  --evaluation outputs/my_sample/score.txt \
  --author YOUR_GITHUB_USERNAME \
  --label "Reference recovery experiment" \
  --dry-run
```

Remove `--dry-run` to push the summary to `dashboard/runs`. GitHub Actions then publishes it to the dashboard. The action may take a short time to start; the page refreshes its progress data automatically. Check [workflow runs](https://github.com/vitalio-sh/CLOSE-THE-BOOK/actions/workflows/dashboard-runs.yml) if a result has not appeared.

`--evaluation` is optional. Without it, a run is shared with no local score unless its report already includes a successful evaluation. A LIVE run has no local score: only the organiser's response can give it an official score.

Solver reports must pass both validation and the official format check, and the CSV must match the report's SHA-256. Temporal TRAIN reports from `scripts/evaluate_window.py` are also supported: they must contain successful validation and scoring plus a matching `output_sha256`. Their format-check flag remains false if no separate official format check was recorded. Format validation alone does not establish accuracy.

A companion `score.txt` may include the scorer's JSON followed by its readable segment table. It must be in the report's directory and have matching row and transaction counts. An evaluation from a different directory must explicitly contain a matching `submission_sha256`, `output_sha256` or `sha256`. Use the scorer output from the exact submitted artifact; matching counts alone do not prove that a manually supplied score file belongs to the same run. Different phases or TRAIN date windows are not interchangeable benchmarks.

The public summary includes author, current working branch, artifact hash, time, phase, validation, counts, runtime and optional local metrics. A source commit is included only when provenance is available; a dirty working tree is not silently represented as a clean commit. Run identity includes the author and evaluation/label fingerprint, so repeated shares are idempotent while a later evaluated view can be added separately. Existing records are not overwritten.

**Do not put secrets or personal data in `--label`.** CSVs, invoices, audit files, full reports, `.env`, local paths and the organiser's `PUBLISHTOKEN` are not copied into this branch. Teammates do not need a dashboard token: the repository workflow holds its own `DASHBOARD_PUBLISH_TOKEN` secret. Publishing this way makes only the selected summary visible on the public dashboard.

## Copy this prompt to your coding agent

> Work on my own `solution/<username>` branch in the Team 10 CLOSE-THE-BOOK repository. Run and validate my reconciliation solution. Fetch `origin dashboard/runs` and copy its `scripts/share_dashboard_run.py` to `/tmp/share_dashboard_run.py` using `git show`, without merging solver code or changing my working branch. Share the actual result using `python3 /tmp/share_dashboard_run.py --repo "$PWD" --report <report.json> --author <my GitHub username> --label <short description>` and optionally `--evaluation <score.txt or evaluation.json>` for the same exact artifact. First use `--dry-run` and inspect the public summary. Publish only validated metrics and provenance; never invent a score or treat local SAMPLE/TRAIN scores as official LIVE scores. Keep datasets, invoices, audit logs, local paths and secrets out of the published summary. The helper pushes metadata to `dashboard/runs` without changing my solver branch. This is dashboard-only publication: do not submit anything to the organisers and do not use FINAL. Check the GitHub Actions run and confirm that my summary appears on the dashboard.

## Maintainer setup

Create `dashboard/runs` with `.github/workflows/dashboard-runs.yml` and an empty `runs/` directory. Add the dashboard write token as the private repository Actions secret `DASHBOARD_PUBLISH_TOKEN`. Keep the organiser token separate. The workflow runs only for this branch, has repository read permission, and posts summaries to the fixed dashboard `/api/runs` endpoint. It replays records idempotently so a later successful run repairs a previously failed publication. A manual dispatch may require the workflow to exist on GitHub's default branch; an ordinary summary push does not.
