#!/bin/bash
# Lead release pipeline: evaluate a frozen tag on SAMPLE/October/January and generate its LIVE artifact
# (parallel), then build the three-window comparison vs the latest scored candidate.
# Never uploads. Usage: scripts/lead_release.sh <label> <tag> <baseline_label> <baseline_tag> <official_record_json>
set -euo pipefail
LABEL="$1"; TAG="$2"; BLABEL="$3"; BTAG="$4"; OFFICIAL="$5"
WT="$(cd "$(dirname "$0")/.." && pwd)"
MAIN=/Users/neo/CodeProejcts/CLOSE-THE-BOOK
LANE=$MAIN/outputs/review_coordination/evaluation_lane
CL=$MAIN/outputs/review_coordination/claude_competitor_lane
PY=/Applications/Xcode.app/Contents/Developer/usr/bin/python3
OUT=$CL/$LABEL
LIVE=$CL/live_$LABEL
SRC=$CL/live_${LABEL}_src
[ -e "$LIVE" ] && { echo "LIVE out dir exists: $LIVE"; exit 1; }
mkdir -p "$OUT"
rm -rf "$SRC" && mkdir -p "$SRC"
git -C "$WT" archive "$TAG" recon scripts vendor | tar -x -C "$SRC"
git -C "$WT" rev-parse "$TAG^{commit}" > "$OUT/source_ref.txt"
PDF=(--pdf-facts --require-pdf-complete --pdf-prewarm --pdf-max-documents 200000 --pdf-max-party-documents 200000
     --pdf-budget-seconds 1800 --pdf-cache-dir "$LANE/shared_pdf_cache")
( cd "$SRC" && DASHBOARD_PUBLISH_ENABLED=0 "$PY" scripts/run_experiment.py --data "$MAIN/data/workspace" \
    --code-root "$SRC" --phase live "${PDF[@]}" --out-dir "$LIVE" > "$OUT/live_run.log" 2>&1 ) &
PL=$!
"$WT/scripts/claude_lane_eval.sh" "$LABEL" sample "$TAG"
for w in sample october january; do ln -sfn "$LABEL/$w" "$CL/${LABEL}_$w"; done
"$PY" "$CL/compare_three_superset.py" --before "$BLABEL" --after "$LABEL" --task "$LABEL" \
  --before-tag "$BTAG" --after-tag "$TAG" --official-live-record "$OFFICIAL" > "$OUT/compare_three.log" 2>&1 \
  || { echo "compare_three failed"; tail -20 "$OUT/compare_three.log"; }
grep -E "^Score|^Fixes" "$OUT/compare_three.log" || true
wait $PL || { echo "LIVE run failed"; tail -20 "$OUT/live_run.log"; exit 1; }
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print('LIVE', d['status'], 'inputs_changed', d['inputs_changed'], 'sha', d.get('submission_sha256'), 's', d.get('seconds_end_to_end'))" "$LIVE/experiment.json"
