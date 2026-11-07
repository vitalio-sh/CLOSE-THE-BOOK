"""Score a submission with the official scorer against any phase folder that has golden_allocations.csv.gz.

    python solution/tools/score_phase.py outputs/sub_trainq2.csv.gz --data outputs/dataroot --phase trainq2
    python solution/tools/score_phase.py outputs/sub_sample_plus.csv.gz --data data/raw/docs --phase sample

Prints score, metrics and, per segment, lines and correct lines (sorted by wrong lines).
--segments FILE also writes "segment correct total" lines, to compare two runs with `join`.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor", "starter"))
import bankrec_score as S  # noqa: E402

EXC = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
       "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--data", required=True)
    ap.add_argument("--phase", required=True)
    ap.add_argument("--segments", help="write per-segment counts to this file")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    folder = os.path.join(a.data, a.phase)
    with open(os.path.join(a.data, "masters", "categories.csv"), encoding="utf-8") as fh:
        cats = [r["category"] for r in csv.DictReader(fh)]
    g = S.load_golden(os.path.join(folder, "golden_allocations.csv.gz"), os.path.join(folder, "bank_transactions.csv.gz"),
                      EXC, cats)
    with open(a.submission, "rb") as fh:
        res = S.score_submission(g, fh.read(), a.submission)
    print(json.dumps({k: res[k] for k in ("ok", "score", "metrics", "errors")}, indent=1))
    segs = sorted(res["segments"].items(), key=lambda x: -x[1]["n"] * (1 - x[1]["accuracy"]))
    for k, v in segs[:a.top]:
        ok = round(v["n"] * v["accuracy"])
        print(f"  {k:30s} n={v['n']:7d} ok={ok:7d} wrong={v['n'] - ok:7d}")
    if a.segments:
        with open(a.segments, "w", encoding="utf-8") as fh:
            for k, v in sorted(res["segments"].items()):
                fh.write(f"{k} {round(v['n'] * v['accuracy'])} {v['n']}\n")


if __name__ == "__main__":
    main()
