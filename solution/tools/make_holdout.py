"""Build a fair holdout root that looks exactly like the real data, to compare any team solution as `--phase live`.

    python solution/tools/make_holdout.py --train outputs/dataroot/train --masters data/raw/docs/masters \
        --start 2025-04-01 --end 2025-06-30 --out outputs/q2root

<out>/masters -> junction/symlink to the masters
<out>/train   TRAIN strictly BEFORE start: bank lines, golden, documents, batch/payment files, PDFs, remittance advices
              (so solutions that learn from TRAIN history never see the answers of the window)
<out>/live    the window [start, end]: bank lines, documents issued in it, batch/payment files of its months, PDFs and
              remittance advices dated in it (like LIVE), open_items_at_start computed from the golden, and the window
              golden (only for scoring: score with solution/tools/score_phase.py --phase live)

Opening balances = total - golden allocations booked before start (RETURNED re-opens, DUPLICATE_PAYMENT does not
reduce); documents issued before the first TRAIN bank line and never allocated are left out (settled before TRAIN).
PDFs / remittance files are hard-linked (no extra disk). Every big file is streamed (~0.6 GB RAM).
This uses oracle opening balances: a temporal test, not an official LIVE score.
"""
import argparse
import csv
import gzip
import os
import shutil
import subprocess
import sys
from collections import defaultdict

BATCH_DIRS = ("direct_debit_batches", "card_settlements", "cheque_deposits", "payment_runs")
DOCS = (("ar_invoices", "customer_id", "document_number"),
        ("ap_invoices", "supplier_id", "supplier_invoice_number"),
        ("credit_notes", "party_id", "document_number"))
OPEN_COLS = ["item_id", "item_type", "entity", "party_id", "document_number", "issue_date", "due_date", "currency",
             "total_amount", "open_amount"]


def cents(s):
    return int(round(float(s) * 100)) if s else 0


def read(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        yield rd.fieldnames
        yield from rd


class Pair:
    """Two gzip CSV writers with the same header: before the window (train) and inside it (live)."""

    def __init__(self, out, name, fields):
        self.files = [gzip.open(os.path.join(out, d, name), "wt", encoding="utf-8", newline="") for d in ("train", "live")]
        self.w = [csv.DictWriter(f, fieldnames=fields, lineterminator="\n") for f in self.files]
        for w in self.w:
            w.writeheader()

    def close(self):
        for f in self.files:
            f.close()


def link_dir_by_index(src, out, sub, date_col, start, end):
    """PDFs / remittance advices: hard-link files dated before start to train/, inside the window to live/."""
    with open(os.path.join(src, sub, "index.csv"), encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        rows, fields = list(rd), rd.fieldnames
    counts = {}
    for phase, keep in (("train", lambda d: d < start), ("live", lambda d: start <= d <= end)):
        sel = [r for r in rows if keep(r[date_col])]
        base = os.path.join(out, phase, sub)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "index.csv"), "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(sel)
        for r in sel:
            dst = os.path.join(base, r["file"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                try:
                    os.link(os.path.join(src, sub, r["file"]), dst)
                except OSError:
                    shutil.copyfile(os.path.join(src, sub, r["file"]), dst)
        counts[phase] = len(sel)
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--masters", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if os.path.exists(a.out):
        sys.exit(f"{a.out} exists: remove it first")
    for d in ("train", "live"):
        os.makedirs(os.path.join(a.out, d))
    masters = os.path.abspath(os.path.join(a.out, "masters"))
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", masters, os.path.abspath(a.masters)], check=True,
                       stdout=subprocess.DEVNULL)
    else:
        os.symlink(os.path.abspath(a.masters), masters, target_is_directory=True)

    # 1) bank lines
    it = read(os.path.join(a.train, "bank_transactions.csv.gz"))
    out = Pair(a.out, "bank_transactions.csv.gz", next(it))
    before, inside = set(), set()
    first_day = "9999"
    for r in it:
        d = r["booking_date"]
        first_day = min(first_day, d)
        if d < a.start:
            before.add(r["bank_txn_id"])
            out.w[0].writerow(r)
        elif d <= a.end:
            inside.add(r["bank_txn_id"])
            out.w[1].writerow(r)
    out.close()
    print(f"bank lines: train {len(before):,} | window {len(inside):,}")

    # 2) golden split + amounts settled before the window
    it = read(os.path.join(a.train, "golden_allocations.csv.gz"))
    out = Pair(a.out, "golden_allocations.csv.gz", next(it))
    settled = defaultdict(int)
    for r in it:
        t = r["bank_txn_id"]
        if t in before:
            out.w[0].writerow(r)
            if r["item_id"]:
                if r["exception"] == "RETURNED":
                    settled[r["item_id"]] -= cents(r["amount"])
                elif r["exception"] != "DUPLICATE_PAYMENT":
                    settled[r["item_id"]] += cents(r["amount"])
        elif t in inside:
            out.w[1].writerow(r)
    out.close()
    del before, inside

    # 3) documents: issued before -> train; issued in window -> live; open at start -> open_items_at_start
    of = gzip.open(os.path.join(a.out, "live", "open_items_at_start.csv.gz"), "wt", encoding="utf-8", newline="")
    ow = csv.DictWriter(of, fieldnames=OPEN_COLS, lineterminator="\n")
    ow.writeheader()
    n_open = 0
    for name, party_col, num_col in DOCS:
        it = read(os.path.join(a.train, f"{name}.csv.gz"))
        out = Pair(a.out, f"{name}.csv.gz", next(it))
        n = [0, 0]
        for r in it:
            d = r["issue_date"]
            if d < a.start:
                out.w[0].writerow(r)
                n[0] += 1
                if d < first_day and r["item_id"] not in settled:
                    continue                          # settled before TRAIN starts: not really open
                open_amt = cents(r["total_amount"]) - settled.get(r["item_id"], 0)
                if open_amt > 0:
                    n_open += 1
                    ow.writerow({"item_id": r["item_id"], "item_type": r["item_id"][:3], "entity": r["entity"],
                                 "party_id": r[party_col], "document_number": r[num_col], "issue_date": d,
                                 "due_date": r.get("due_date", ""), "currency": r["currency"],
                                 "total_amount": r["total_amount"], "open_amount": f"{open_amt / 100:.2f}"})
            elif d <= a.end:
                out.w[1].writerow(r)
                n[1] += 1
        out.close()
        print(f"{name}: train {n[0]:,} | window {n[1]:,}")
    of.close()
    print(f"open items at start: {n_open:,}")

    # 4) batch / payment files by month in the file name
    m_start, m_end = a.start[:7].replace("-", ""), a.end[:7].replace("-", "")
    for sub in BATCH_DIRS:
        n = [0, 0]
        for d in ("train", "live"):
            os.makedirs(os.path.join(a.out, d, sub), exist_ok=True)
        for f in os.listdir(os.path.join(a.train, sub)):
            if not f.endswith(".csv") or f.startswith("."):
                continue
            month = next((p[:6] for p in f[:-4].split("_") if len(p) >= 6 and p[:6].isdigit()), None)
            if month is None:
                continue
            phase = "train" if month < m_start else "live" if month <= m_end else None
            if phase:
                shutil.copyfile(os.path.join(a.train, sub, f), os.path.join(a.out, phase, sub, f))
                n[phase == "live"] += 1
        print(f"{sub}: train {n[0]} files | window {n[1]} files")

    # 5) PDFs and remittance advices by their index date (hard links)
    for sub, col in (("pdfs", "document_date"), ("remittance_advices", "received_date")):
        print(f"{sub}: {link_dir_by_index(a.train, a.out, sub, col, a.start, a.end)}")


if __name__ == "__main__":
    main()
