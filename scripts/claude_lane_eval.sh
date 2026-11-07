#!/bin/bash
# Evaluate the current worktree source on the October and January windows (and optionally SAMPLE)
# with the same prepared inputs, PDF settings and cache as the evaluation lane.
# Usage: scripts/claude_lane_eval.sh <label> [sample]
set -euo pipefail
LABEL="$1"
WT="$(cd "$(dirname "$0")/.." && pwd)"
MAIN=/Users/neo/CodeProejcts/CLOSE-THE-BOOK
LANE=$MAIN/outputs/review_coordination/evaluation_lane
OUT=$MAIN/outputs/review_coordination/claude_competitor_lane/$LABEL
PY=/Applications/Xcode.app/Contents/Developer/usr/bin/python3
mkdir -p "$OUT"
SNAP="$OUT/source"
rm -rf "$SNAP" && mkdir -p "$SNAP"
cp -R "$WT/recon" "$WT/scripts" "$WT/vendor" "$SNAP/"
PDF=(--pdf-facts --require-pdf-complete --pdf-prewarm --pdf-max-documents 200000 --pdf-max-party-documents 200000
     --pdf-budget-seconds 1800 --pdf-cache-dir "$LANE/shared_pdf_cache")
run_window() {
  local name=$1 start=$2 end=$3
  "$PY" "$SNAP/scripts/evaluate_window.py" --data "$MAIN/data/workspace" --code-root "$SNAP" \
    --prepared-input "$LANE/t3_${name}_pdf_inputs/input" --start "$start" --end "$end" "${PDF[@]}" \
    --out "$OUT/$name" > "$OUT/$name.log" 2>&1
}
run_window october 2024-10-01 2024-10-31 &
P1=$!
run_window january 2025-01-01 2025-01-14 &
P2=$!
if [ "${2:-}" = "sample" ]; then
  "$PY" "$SNAP/scripts/run_experiment.py" --data "$MAIN/data/workspace" --code-root "$SNAP" --phase sample "${PDF[@]}" \
    --out-dir "$OUT/sample" > "$OUT/sample.log" 2>&1 &
  P3=$!
  wait $P3
fi
wait $P1
wait $P2
for w in october january; do
  "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); m=d['score']['metrics']; print(sys.argv[2], d['score']['score'], 'P', m['precision'], 'R', m['recall'], 'acc', m['txn_accuracy'], 'mF1', m['exception_macro_f1'])" "$OUT/$w/report.json" "$w"
done
