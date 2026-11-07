# Local dashboard quickstart

Run these commands from the repository root with Python 3.12. No packages need installing.

First export the dashboard snapshot from an existing reconciliation candidate. Supply the exact candidate you want to inspect, together with its adjacent `.report.json` and `.audit.jsonl` files:

```sh
python3.12 scripts/build_dashboard_data.py --submission outputs/submission_live_release.csv.gz --data data/workspace --out dashboard/data.json
python3.12 scripts/serve_dashboard.py
```

Open <http://127.0.0.1:8765>. Stop the server with **Ctrl+C**. If the port is occupied, run `python3.12 scripts/serve_dashboard.py --port 8766` and open <http://127.0.0.1:8766>.

The exporter reads an existing run; it does not run reconciliation. Use `--report` and `--audit` if those artifacts are stored elsewhere. `--sample-size 200` controls the bounded transaction explorer. Whole-run counts and currency-separated totals cover the complete bank dataset; search and drilldown cover only the clearly labelled representative sample. Coverage and format validation are not matching accuracy. LIVE accuracy is unavailable without external evaluation.

The dashboard is read-only. Its download endpoint, `/submission`, serves only the candidate recorded in `dashboard/data.json` under `sources.submission.path`. That file must resolve inside this repository's `outputs` directory. Every download verifies its bytes against `pipeline.sha256`; a changed file returns HTTP 409 instead of downloading stale or mismatched results. Regenerate the export after replacing the candidate. Downloading a file does not upload or submit it to the hackathon.

The server binds only to `127.0.0.1`. It serves the four dashboard assets and the verified candidate; repository browsing, directory listings and arbitrary data-file URLs are unavailable. Keep it local: no reverse proxy or public sharing is needed for a team demo. The export contains real challenge examples, so review it before publishing any dashboard files.

Design sources and decisions are documented in [DASHBOARD_DESIGN.md](DASHBOARD_DESIGN.md). The interface is in English for the international team.

Verified in-browser: desktop and 390px mobile layout, search with zero results, outcome filtering, transaction detail, guided examples, process and export screens. The mobile document has no horizontal overflow; wider tables scroll within their container. Exporter and server regression tests cover stratification, currency separation, artifact hash mismatch and file access boundaries.

## Published team view and automatic updates

[Open the dashboard](https://valtermia-team10-reconciliation.peakedalbatross1.chatgpt.site). The owner approved public access without sign-in. The same site URL shows the latest accepted LIVE or FINAL run, containing whole-run summaries and 200 representative examples. The complete candidate CSV remains downloadable through the local server; public publication is not a competition submission.

The update happens after a successful reconciliation run. With publication configured, the normal `recon.run` command publishes its verified LIVE/FINAL snapshot after the candidate and its audit/report have passed checks. SAMPLE, TRAIN, and runs with a nonstandard matching-module selection are excluded. This hook exports an existing result: it does **not** launch another reconciliation, run on a timer, or infer new results when someone opens the site.

A visible browser tab checks `/api/version` every 15 seconds and immediately when the tab becomes visible again. When the run ID or output hash changes, it fetches the new snapshot without using the browser cache. The page updates without reloading, preserving the active screen, search, outcome filter, selected transaction when still in the filtered examples, and page number where possible. Pagination is clamped if the example list becomes shorter. The header shows the publication time and update status. A network error leaves the previous run visible with a stale-status message; it never turns an unsuccessful refresh into a claim of new results.

Only snapshots with `hosting.auto_refresh: true` enable these requests. Ordinary local exports have no polling requirement or `/api/version` dependency. Hidden tabs pause polling. Updates do not request a competition score or submit a candidate to the hackathon.

## Publication setup and retry

Publication is configured in the local, Git-ignored `.dashboard-publish.json` file. It has `enabled: true`, the full HTTPS endpoint URL ending in `/api/snapshot`, and the authorized publication token. Environment overrides are `DASHBOARD_PUBLISH_ENABLED`, `DASHBOARD_PUBLISH_URL`, and `DASHBOARD_PUBLISH_TOKEN`. This workspace is configured by the owner; another team checkout needs its own authorized setup. Never commit the token, embed it in browser assets, or put it in a public report. The browser reads snapshots without this write credential.

A network publication failure does not invalidate or remove the locally verified candidate. Its `.dashboard-status.json` sidecar records the result, and `outputs/dashboard-outbox/` retains pending publication information. Retry publication of an existing verified candidate with:

```sh
python3.12 scripts/publish_dashboard.py --submission outputs/submission_live_release.csv.gz --data data/workspace
```

The retry validates the output and audit identities, coverage and recorded source stability, rebuilds the snapshot, and preserves the original run identity and timestamps. It does not run inference. The command prints a sanitized status: `pending` exits with status 1; `published`, `superseded`, or `skipped` exit with status 0. A skipped result, for example when the token is missing or the phase is SAMPLE, is not a successful upload. An older run is marked superseded rather than replacing the newer public result. Legacy candidates receive a clearly identified fallback run identity and timestamp provenance.

The registered Site identity is in `dashboard/site.json`. Its separate checkout is `outputs/hosted-dashboard`, with `.openai/hosting.json` retained there. The initial service deployment and later interface changes use the existing Sites source workflow and publish a new version of the **same** project. Successful inference runs update the stored snapshot through the publication hook; they do not create a replacement site or require the reader to refresh the page manually.
