"""Cascade of matchers. Each matcher claims bank lines; the first claim wins. Unclaimed lines fall to the fallback.

A matcher is a function `match(ph: Phase, st: State) -> None` that calls `st.assign(...)` / `st.classify(...)`.
Add it to MATCHERS in priority order (most reliable first).
"""
from __future__ import annotations

import csv
import gzip
import time
from collections import defaultdict

import polars as pl

from .data import Phase

EXCEPTIONS = {"PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE",
              "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"}


class State:
    def __init__(self, ph: Phase):
        self.ph = ph
        self.rows: dict[str, list] = {}          # bank_txn_id -> list of (item_id, amount, category, exception)
        self.by: dict[str, str] = {}             # bank_txn_id -> matcher name that claimed it
        self.open = dict(zip(ph.items["item_id"].to_list(), ph.items["open_amount"].to_list()))
        self.settled_by: dict[str, list] = defaultdict(list)  # item_id -> bank_txn_ids that touched it
        self.current = ""

    def claimed(self, txn: str) -> bool:
        return txn in self.rows

    def assign(self, txn: str, allocs: list) -> bool:
        """allocs: list of (item_id, amount, exception or ''). Updates the open-item ledger."""
        if txn in self.rows or not allocs:
            return False
        seen, rows = set(), []
        for item, amt, exc in allocs:
            if item in seen:
                continue
            seen.add(item)
            exc = exc or ""
            rows.append((item, round(abs(float(amt)), 2), "", exc))
            if item in self.open:
                if exc == "RETURNED":
                    self.open[item] = self.open[item] + abs(amt)
                elif exc in ("PARTIAL", "SHORT_DISPUTE", "DUPLICATE_PAYMENT"):
                    self.open[item] = self.open[item] - (abs(amt) if exc != "DUPLICATE_PAYMENT" else 0)
                else:
                    self.open[item] = 0.0
            self.settled_by[item].append(txn)
        self.rows[txn] = rows
        self.by[txn] = self.current
        return True

    def classify(self, txn: str, category: str, exception: str = "") -> bool:
        if txn in self.rows:
            return False
        self.rows[txn] = [("", None, category, exception or "")]
        self.by[txn] = self.current
        return True

    def unclaimed(self) -> pl.DataFrame:
        return self.ph.bank.filter(~pl.col("bank_txn_id").is_in(list(self.rows.keys())))


def fallback(ph: Phase, st: State) -> None:
    for t, a in zip(ph.bank["bank_txn_id"].to_list(), ph.bank["amount"].to_list()):
        if t not in st.rows:
            if a is not None and a > 0:
                st.classify(t, "UNIDENTIFIED", "UNIDENTIFIED")
            else:
                st.classify(t, "BANK_FEES", "")


def get_matchers():
    from .matchers import MATCHERS
    return MATCHERS


def run(ph: Phase, only: list | None = None, verbose: bool = True) -> State:
    st = State(ph)
    for name, fn in get_matchers():
        if only and name not in only:
            continue
        st.current = name
        t0, n0 = time.time(), len(st.rows)
        try:
            fn(ph, st)
        except Exception as e:  # a broken matcher must never kill the submission
            import traceback
            traceback.print_exc()
            print(f"!! matcher {name} failed: {e}")
        if verbose:
            print(f"  {name:28s} +{len(st.rows) - n0:7d} lines  ({time.time() - t0:5.1f}s)")
    st.current = "fallback"
    fallback(ph, st)
    return st


def to_frame(st: State) -> pl.DataFrame:
    out = []
    for t, rows in st.rows.items():
        for item, amt, cat, exc in rows:
            out.append((t, item, "" if amt is None else f"{amt:.2f}", cat, exc, st.by.get(t, "")))
    return pl.DataFrame(out, schema=["bank_txn_id", "item_id", "amount", "category", "exception", "matcher"], orient="row")


def validate(df: pl.DataFrame, ph: Phase, categories: set) -> list:
    """Replicates the FORMAT.md checks. Returns a list of error strings (empty = valid)."""
    errs = []
    txns = set(ph.bank["bank_txn_id"].to_list())
    sub = set(df["bank_txn_id"].to_list())
    if sub - txns:
        errs.append(f"{len(sub - txns)} bank_txn_id not in phase")
    if txns - sub:
        errs.append(f"{len(txns - sub)} bank lines missing (count as wrong)")
    item_rows = df.filter(pl.col("item_id") != "")
    if item_rows.select(["bank_txn_id", "item_id"]).is_duplicated().any():
        errs.append("duplicated (bank_txn_id,item_id)")
    noitem = df.filter(pl.col("item_id") == "")
    if noitem["bank_txn_id"].is_duplicated().any():
        errs.append("more than one no-item row per bank line")
    if set(noitem["bank_txn_id"].to_list()) & set(item_rows["bank_txn_id"].to_list()):
        errs.append("bank line with both item rows and category row")
    bad_cat = set(noitem["category"].to_list()) - categories
    if bad_cat:
        errs.append(f"bad categories {bad_cat}")
    if (item_rows["category"] != "").any():
        errs.append("category on item rows")
    bad_exc = set(df["exception"].to_list()) - EXCEPTIONS - {""}
    if bad_exc:
        errs.append(f"bad exceptions {bad_exc}")
    if (item_rows["amount"] == "").any():
        errs.append("missing amount on item rows")
    if df.height > 3_000_000:
        errs.append("more than 3M rows")
    return errs


def write(df: pl.DataFrame, path: str) -> None:
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
        w.writerows(df.select(["bank_txn_id", "item_id", "amount", "category", "exception"]).iter_rows())
