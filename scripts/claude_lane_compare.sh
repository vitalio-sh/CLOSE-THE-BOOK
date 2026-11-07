#!/bin/bash
# Per-segment fixes/regressions of <after> vs <before> lane runs on October and January.
# Usage: scripts/claude_lane_compare.sh <before_label> <after_label>
set -euo pipefail
B="$1"; A="$2"
MAIN=/Users/neo/CodeProejcts/CLOSE-THE-BOOK
LANE=$MAIN/outputs/review_coordination/evaluation_lane
CL=$MAIN/outputs/review_coordination/claude_competitor_lane
PY=/Applications/Xcode.app/Contents/Developer/usr/bin/python3
for w in october january; do
  INPUT=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['input_root'])" "$CL/$B/$w/report.json")
  "$PY" "$LANE/compare_task.py" --before "$CL/$B/$w/submission.csv.gz" --after "$CL/$A/$w/submission.csv.gz" \
    --golden "$CL/$B/$w/target_golden.csv.gz" --bank "$INPUT/live/bank_transactions.csv.gz" \
    --categories "$LANE/categories.json" --exceptions "$LANE/exceptions.json" \
    --task "${A}_vs_${B}_$w" --before-tag "$B" --after-tag "$A" --out "$CL/compare_${A}_vs_${B}_$w.json" \
    | sed -n '/^Task:/,$p'
done
