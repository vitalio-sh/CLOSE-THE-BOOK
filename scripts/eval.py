"""Score a sample/dev submission with the official scorer and show where the points are lost.

    .venv/bin/python scripts/eval.py out/sub_dev.csv.gz --phase dev [--seg ar_ref_noise] [--n 15]

Prints metrics, per-segment accuracy sorted by lost lines, per-matcher accuracy, and writes
out/errors/<segment>.csv with our rows vs golden rows for wrong lines.
"""
import argparse
import csv
import gzip
import json
import os
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "data", "starter"))

import polars as pl  # noqa: E402

import bankrec_score as S  # noqa: E402
from bankrec.data import DATA, load_phase  # noqa: E402

EXC = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
       "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]


def line_ok(ours: list, gold: list) -> bool:
    def norm(rows):
        return sorted((r[0] or "", r[2] or "", r[3] or "") for r in rows)
    a, b = norm(ours), norm(gold)
    if [(x[0], x[1] if not x[0] else "", x[2]) for x in a] != [(x[0], x[1] if not x[0] else "", x[2]) for x in b]:
        return False
    ga = {r[0]: float(r[1]) for r in gold if r[0]}
    for r in ours:
        if r[0]:
            g = ga[r[0]]
            if abs(float(r[1] or 0) - g) > max(0.01, 0.005 * abs(g)):
                return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--phase", default="dev", choices=["sample", "dev"])
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--seg", help="print wrong examples of this segment")
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args()
    ph = load_phase(a.phase, a.start, a.end)
    cats = ph.masters["categories"]["category"].to_list()

    # official score restricted to the phase lines
    with tempfile.NamedTemporaryFile("wb", suffix=".csv.gz", delete=False) as tmp:
        pass
    with gzip.open(tmp.name, "wt") as out:
        out.write("bank_txn_id\n")
        for t in ph.bank["bank_txn_id"].to_list():
            out.write(t + "\n")
    gold_path = os.path.join(DATA, "sample" if a.phase == "sample" else "train", "golden_allocations.csv.gz")
    if a.phase == "dev":   # restrict golden to the window
        gtmp = tmp.name + ".golden.csv.gz"
        ph.golden.write_csv(gtmp.replace(".gz", ""))
        with open(gtmp.replace(".gz", ""), "rb") as fi, gzip.open(gtmp, "wb") as fo:
            fo.write(fi.read())
        gold_path = gtmp
    g = S.load_golden(gold_path, tmp.name, EXC, cats)
    res = S.score_submission(g, open(a.submission, "rb").read(), a.submission)
    print(json.dumps({"score": res["score"], **{k: round(v, 4) if isinstance(v, float) else v
                                                  for k, v in res["metrics"].items()}}, indent=1))
    if res["errors"]:
        print("ERRORS", res["errors"][:10])

    # our vs golden per line
    sub = pl.read_parquet(a.submission.replace(".csv.gz", ".parquet"))
    ours = {}
    for t, i, am, c, e, m in sub.iter_rows():
        ours.setdefault(t, []).append((i, am, c, e, m))
    gold, seg = {}, {}
    for r in ph.golden.iter_rows(named=True):
        gold.setdefault(r["bank_txn_id"], []).append((r["item_id"] or "", r["amount"] or "", r["category"] or "",
                                                      r["exception"] or ""))
        seg[r["bank_txn_id"]] = r["segment"]
    stats, mstats, wrong = {}, {}, {}
    for t, gr in gold.items():
        o = ours.get(t, [])
        ok = line_ok([x[:4] for x in o], gr)
        s = seg[t]
        n, k = stats.get(s, (0, 0))
        stats[s] = (n + 1, k + ok)
        m = o[0][4] if o else "missing"
        n, k = mstats.get(m, (0, 0))
        mstats[m] = (n + 1, k + ok)
        if not ok:
            wrong.setdefault(s, []).append(t)
    print("\nby matcher (lines claimed, accuracy):")
    for m, (n, k) in sorted(mstats.items(), key=lambda x: -x[1][0]):
        print(f"  {m:22s} n={n:7d} acc={k / n:.3f}")
    print("\nby segment, sorted by wrong lines:")
    for s, (n, k) in sorted(stats.items(), key=lambda x: -(x[1][0] - x[1][1])):
        print(f"  {s:32s} n={n:6d} acc={k / n:.3f} wrong={n - k}")
    os.makedirs(os.path.join(ROOT, "out", "errors"), exist_ok=True)
    for s, ts in wrong.items():
        with open(os.path.join(ROOT, "out", "errors", f"{s}.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["bank_txn_id", "who", "item_id", "amount", "category", "exception", "matcher"])
            for t in ts[:200]:
                for r in gold[t]:
                    w.writerow([t, "GOLD", *r, ""])
                for r in ours.get(t, []):
                    w.writerow([t, "OURS", *r])
    if a.seg:
        bank = {r["bank_txn_id"]: r for r in ph.bank.filter(pl.col("bank_txn_id").is_in(wrong.get(a.seg, [])[:a.n])).to_dicts()}
        for t in wrong.get(a.seg, [])[:a.n]:
            b = bank[t]
            print(f"\n### {t} {b['account_id']} {b['booking_date']} {b['amount']} {b['currency']} code={b['bank_code']}")
            print("   text:", b["text"], "| ref1:", b["ref1"], "| ref2:", b["ref2"], "| cp:", b["counterparty_name"],
                  "| rmt:", b["remittance_info"], "| orig:", b["orig_amount"], b["orig_currency"])
            for r in gold[t][:12]:
                print("   GOLD", r)
            for r in ours.get(t, [])[:12]:
                print("   OURS", r)


if __name__ == "__main__":
    main()
