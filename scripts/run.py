"""Run the pipeline on a phase and write a validated submission.

    .venv/bin/python scripts/run.py --phase live  --out out/sub_live.csv.gz
    .venv/bin/python scripts/run.py --phase final --out out/sub_final.csv.gz
    .venv/bin/python scripts/run.py --phase dev   --out out/sub_dev.csv.gz   (then scripts/eval.py)
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bankrec.data import load_phase  # noqa: E402
from bankrec.pipeline import run, to_frame, validate, write  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="dev", choices=["sample", "dev", "train", "live", "final"])
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", nargs="*", help="run only these matchers")
    a = ap.parse_args()
    t0 = time.time()
    ph = load_phase(a.phase, a.start, a.end)
    print(f"phase {ph.name}: {ph.bank.height} bank lines, {ph.items.height} items ({time.time() - t0:.0f}s)")
    st = run(ph, a.only)
    df = to_frame(st)
    cats = set(ph.masters["categories"]["category"].to_list())
    errs = validate(df, ph, cats)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    write(df, a.out)
    df.write_parquet(a.out.replace(".csv.gz", ".parquet").replace(".csv", ".parquet"))
    print(f"{df.height} rows -> {a.out}  ({time.time() - t0:.0f}s)")
    print("VALID" if not errs else "INVALID: " + "; ".join(errs))
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
