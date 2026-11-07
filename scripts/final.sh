#!/usr/bin/env bash
# FINAL runbook (18:00 Madrid). Downloads + extracts FINAL into data/final, runs the pipeline, validates.
# Does NOT submit. Submission is done by the user:
#   read -rs HK_TOKEN && export HK_TOKEN
#   .venv/bin/python scripts/submit.py FINAL out/sub_final.csv.gz --i-know-final-is-one-shot
set -euo pipefail
cd "$(dirname "$0")/.."
URL="https://hackathon.usetalky.com/data/v1/bankrec-final.tar.gz"
ARC="data/archives/bankrec-final.tar.gz"
TMP="data/_final_extract"

step() { echo; echo "=== $* ($(date +%H:%M:%S))"; }

if [ ! -d data/final ]; then
  step "download"
  mkdir -p data/archives
  curl -fL --retry 5 -sS "$URL" -o "$ARC.part" && mv "$ARC.part" "$ARC"
  shasum -a 256 "$ARC"

  step "check archive (no absolute paths, no ..)"
  if tar -tzf "$ARC" | grep -E '(^/|(^|/)\.\.(/|$))'; then echo "UNSAFE ARCHIVE"; exit 1; fi
  tar -tzf "$ARC" | head -20

  step "extract"
  rm -rf "$TMP" && mkdir -p "$TMP"
  tar -xzf "$ARC" -C "$TMP"
  SRC=$(dirname "$(find "$TMP" -name bank_transactions.csv.gz | head -1)")
  [ -n "$SRC" ] || { echo "bank_transactions.csv.gz not found in archive"; exit 1; }
  mv "$SRC" data/final
  rm -rf "$TMP"
fi
ls data/final
for f in bank_transactions.csv.gz open_items_at_start.csv.gz; do
  [ -f "data/final/$f" ] || { echo "MISSING data/final/$f"; exit 1; }
done

step "run pipeline (must print VALID)"
.venv/bin/python scripts/run.py --phase final --out out/sub_final.csv.gz 2>&1 | tee out/final_run.log

step "summary"
grep -E "VALID|INVALID|ERROR" out/final_run.log | tail -5
ls -la out/sub_final.csv.gz
echo "Not submitted. Review, then submit manually (see header)."
