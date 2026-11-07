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
