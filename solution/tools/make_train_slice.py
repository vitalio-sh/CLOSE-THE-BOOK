"""Build a LIVE-like test phase from a window of TRAIN (all banks), with the golden answers of that window.

    python solution/tools/make_train_slice.py --data outputs/dataroot --start 2025-04-01 --end 2025-06-30 --name trainq2

Writes <data>/<name>/ with:
  bank_transactions.csv.gz      TRAIN bank lines booked in [start, end]
  golden_allocations.csv.gz     their golden rows
  ar_invoices / ap_invoices / credit_notes.csv.gz
                                documents issued in [start, end] + documents issued before start still open at start
  open_items_at_start.csv.gz    documents open at start and their open amount, same columns as LIVE:
                                open = total - golden allocations booked before start
                                (RETURNED re-opens the amount, DUPLICATE_PAYMENT does not reduce it).
                                Documents issued before the first TRAIN bank line and never allocated in the golden
                                are left out: they were settled before TRAIN starts (LIVE lists ~3 k such items,
                                a naive computation would keep ~214 k phantom open items).
  direct_debit_batches/ card_settlements/ cheque_deposits/ payment_runs/
                                files of the months in [start, end] (copied)

Memory: every big file is streamed; the only large structure is one integer per settled document.
Note: opening balances come from the golden ("oracle"), so this is a temporal test, not an official LIVE score.
"""
import argparse
import csv
import gzip
import os
import shutil
from collections import defaultdict

DIRS = ("direct_debit_batches", "card_settlements", "cheque_deposits", "payment_runs")
DOCS = (("ar_invoices", "customer_id", "document_number"),
        ("ap_invoices", "supplier_id", "supplier_invoice_number"),
        ("credit_notes", "party_id", "document_number"))
OPEN_COLS = ["item_id", "item_type", "entity", "party_id", "document_number", "issue_date", "due_date", "currency",
             "total_amount", "open_amount"]


def cents(s):
    return int(round(float(s) * 100)) if s else 0


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        yield rd.fieldnames
        yield from rd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="folder with train/ and masters/")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--name", required=True)
    a = ap.parse_args()
    src, dst = os.path.join(a.data, "train"), os.path.join(a.data, a.name)
    os.makedirs(dst, exist_ok=True)

    def writer(name, fields):
        fh = gzip.open(os.path.join(dst, name), "wt", encoding="utf-8", newline="")
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        return fh, w

    # 1) bank lines of the window; lines after the window are remembered so they do not count as "paid before"
    it = rows(os.path.join(src, "bank_transactions.csv.gz"))
    fh, w = writer("bank_transactions.csv.gz", next(it))
    inside, after = set(), set()
    first_day = "9999"
    for r in it:
        d = r["booking_date"]
        first_day = min(first_day, d)
        if a.start <= d <= a.end:
            inside.add(r["bank_txn_id"])
            w.writerow(r)
        elif d > a.end:
            after.add(r["bank_txn_id"])
    fh.close()
    print(f"bank lines in window: {len(inside):,}")

    # 2) golden of the window; amounts settled before the window, per document
    it = rows(os.path.join(src, "golden_allocations.csv.gz"))
    fh, w = writer("golden_allocations.csv.gz", next(it))
    settled = defaultdict(int)
    for r in it:
        t = r["bank_txn_id"]
        if t in inside:
            w.writerow(r)
        elif t not in after and r["item_id"]:
            if r["exception"] == "RETURNED":
                settled[r["item_id"]] -= cents(r["amount"])
            elif r["exception"] != "DUPLICATE_PAYMENT":
                settled[r["item_id"]] += cents(r["amount"])
    fh.close()
    del inside, after

    # 3) documents: issued in the window, or issued before and still open at start
    ofh, ow = writer("open_items_at_start.csv.gz", OPEN_COLS)
    n_open = 0
    for name, party_col, num_col in DOCS:
        it = rows(os.path.join(src, f"{name}.csv.gz"))
        fh, w = writer(f"{name}.csv.gz", next(it))
        n_in = 0
        for r in it:
            d = r["issue_date"]
            if a.start <= d <= a.end:
                w.writerow(r)
                n_in += 1
            elif d < a.start:
                if d < first_day and r["item_id"] not in settled:
                    continue                          # settled before TRAIN starts: not really open
                open_amt = cents(r["total_amount"]) - settled.get(r["item_id"], 0)
                if open_amt > 0:
                    w.writerow(r)
                    n_open += 1
                    ow.writerow({"item_id": r["item_id"], "item_type": r["item_id"][:3], "entity": r["entity"],
                                 "party_id": r[party_col], "document_number": r[num_col], "issue_date": d,
                                 "due_date": r.get("due_date", ""), "currency": r["currency"],
                                 "total_amount": r["total_amount"], "open_amount": f"{open_amt / 100:.2f}"})
        fh.close()
        print(f"{name}: {n_in:,} issued in window")
    ofh.close()
    print(f"open items at start: {n_open:,}")

    # 4) batch / payment files of the months of the window
    months = {f"{y}{m:02d}" for y in range(int(a.start[:4]), int(a.end[:4]) + 1) for m in range(1, 13)
              if a.start[:7] <= f"{y}-{m:02d}" <= a.end[:7]}
    for sub in DIRS:
        os.makedirs(os.path.join(dst, sub), exist_ok=True)
        n = 0
        for f in os.listdir(os.path.join(src, sub)):
            if f.endswith(".csv") and not f.startswith(".") and any(f"_{mo}" in f for mo in months):
                shutil.copyfile(os.path.join(src, sub, f), os.path.join(dst, sub, f))
                n += 1
        print(f"{sub}: {n} files")


if __name__ == "__main__":
    main()
