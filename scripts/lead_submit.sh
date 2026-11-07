#!/bin/bash
# Build evidence, dry-run, then submit a lead release to LIVE (never FINAL).
# Usage: scripts/lead_submit.sh <label> <candidate_tag> <baseline_tag>
set -euo pipefail
LABEL="$1"; TAG="$2"; BTAG="$3"
MAIN=/Users/neo/CodeProejcts/CLOSE-THE-BOOK
CL=$MAIN/outputs/review_coordination/claude_competitor_lane
cd "$MAIN"
python3 /Users/neo/CodeProejcts/ctb-integ/scripts/lead_evidence.py "$LABEL" "$TAG" "$BTAG"
EVID=$MAIN/outputs/platform/evidence/team_$LABEL.json
SUB=$CL/live_$LABEL/submission.csv.gz
python3 scripts/submit_live.py --submission "$SUB" --candidate "$TAG" --evidence "$EVID" --dry-run
python3 scripts/submit_live.py --submission "$SUB" --candidate "$TAG" --evidence "$EVID"
