#!/bin/bash
# Generate a LIVE artifact from a frozen git ref with the coordinator's exact LIVE command.
# Never uploads. Usage: scripts/claude_lane_live.sh <label> <git-ref>
set -euo pipefail
LABEL="$1"; REF="$2"
WT="$(cd "$(dirname "$0")/.." && pwd)"
MAIN=/Users/neo/CodeProejcts/CLOSE-THE-BOOK
LANE=$MAIN/outputs/review_coordination/evaluation_lane
OUT=$MAIN/outputs/review_coordination/claude_competitor_lane/live_$LABEL
PY=/Applications/Xcode.app/Contents/Developer/usr/bin/python3
rm -rf "$OUT" && mkdir -p "$OUT/source"
git -C "$WT" archive "$REF" recon scripts vendor | tar -x -C "$OUT/source"
git -C "$WT" rev-parse "$REF" > "$OUT/source_ref.txt"
cd "$OUT/source"
export DASHBOARD_PUBLISH_ENABLED=0   # prototype artifacts never reach the team dashboard
"$PY" -m recon.run --data "$MAIN/data/workspace" --phase live --out "$OUT/submission.csv.gz" \
  --modules batches,documents,matching --pdf-facts --pdf-max-documents 200000 --pdf-budget-seconds 1800.0 \
  --pdf-cache-dir "$LANE/shared_pdf_cache" --pdf-max-party-documents 200000 --pdf-prewarm --require-pdf-complete \
  > "$OUT/run.log" 2>&1
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print('publication', d.get('publication'), 'modules', d.get('modules'), 'validation_ok', d['validation'].get('ok'), 'official_ok', d['official_structure'].get('ok'))" "$OUT/submission.csv.gz.report.json"
