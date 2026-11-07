"""Lines that settle no item -> category (POLICIES §4).

1. BANK_REVERSAL pairs: "ANULACION OPERACION INDEBIDA" + the original wrong line (same account, opposite amount).
2. A lookup table learned from TRAIN golden: key (sign, bank_code, text pattern) -> category, when pure enough.
   UNIDENTIFIED / CUSTOMER_* are not claimed here (they need the item matchers to fail first).
"""
from __future__ import annotations

import hashlib
import os
import re

import polars as pl

from ..data import CACHE, DATA, _bank, _read

NEVER = {"UNIDENTIFIED", "CUSTOMER_ADVANCE", "CUSTOMER_REFUND", "SUPPLIER_REFUND", "ITEM"}
MIN_N, MIN_PURITY = 3, 0.97


def pat(s) -> str:
    s = (s or "").upper()
    s = re.sub(r"\d", "9", s)
    s = re.sub(r"\s+", " ", s)
    return s[:22]


def _model(cutoff: str | None) -> pl.DataFrame:
    # cache keyed on the TRAIN golden + bank files actually used (data root, size, mtime) and the cutoff,
    # so a different / refreshed / truncated TRAIN never reuses a stale model
    gpath = os.path.join(DATA, "train", "golden_allocations.csv.gz")
    bpath = os.path.join(DATA, "train", "bank_transactions.csv.gz")
    sig = "|".join(f"{os.path.realpath(x)}:{os.path.getsize(x)}:{int(os.path.getmtime(x))}" for x in (gpath, bpath))
    key = hashlib.sha256(sig.encode()).hexdigest()[:16]
    path = os.path.join(CACHE, f"nonitem_model_{cutoff or 'all'}_{key}.parquet")
    if os.path.exists(path):
        return pl.read_parquet(path)
    g = _read(gpath)
    b = _bank("train")
    if cutoff:
        b = b.filter(pl.col("booking_date") < cutoff)
    per = g.group_by("bank_txn_id").agg(pl.col("category").first(), pl.col("item_id").first())
    d = b.join(per, on="bank_txn_id").with_columns(
        pl.when(pl.col("item_id").is_null() | (pl.col("item_id") == "")).then(pl.col("category"))
        .otherwise(pl.lit("ITEM")).alias("y"),
        pl.col("text").map_elements(pat, return_dtype=pl.Utf8).alias("pat"),
        (pl.col("amount") > 0).alias("pos"))
    m = (d.group_by(["pos", "bank_code", "pat", "y"]).agg(pl.len().alias("n"))
         .with_columns(pl.col("n").sum().over(["pos", "bank_code", "pat"]).alias("tot"))
         .sort("n", descending=True).unique(["pos", "bank_code", "pat"], keep="first")
         .with_columns((pl.col("n") / pl.col("tot")).alias("purity")))
    os.makedirs(CACHE, exist_ok=True)
    m.write_parquet(path)
    return m


def bank_reversals(ph, st):
    rows = ph.bank.select("bank_txn_id", "account_id", "booking_date", "amount", "text").to_dicts()
    by_key = {}
    for r in rows:
        by_key.setdefault((r["account_id"], round(r["amount"] * 100)), []).append(r)
    for r in rows:
        if "ANULACION OPERACION INDEBIDA" in (r["text"] or "").upper() or "REVERSAL OF ERRONEOUS" in (r["text"] or "").upper():
            cands = [o for o in by_key.get((r["account_id"], -round(r["amount"] * 100)), [])
                     if o["booking_date"] <= r["booking_date"] and o["bank_txn_id"] != r["bank_txn_id"]]
            st.classify(r["bank_txn_id"], "BANK_REVERSAL")
            if cands:
                o = max(cands, key=lambda x: x["booking_date"])
                if st.claimed(o["bank_txn_id"]) and st.rows[o["bank_txn_id"]][0][2] != "BANK_REVERSAL":
                    continue
                st.classify(o["bank_txn_id"], "BANK_REVERSAL")


def match(ph, st):
    bank_reversals(ph, st)
    cutoff = ph.start if ph.name in ("dev", "sample") else None
    m = _model(cutoff).filter((pl.col("n") >= MIN_N) & (pl.col("purity") >= MIN_PURITY) & ~pl.col("y").is_in(list(NEVER)))
    lut = {(p, c, t): y for p, c, t, y in m.select("pos", "bank_code", "pat", "y").iter_rows()}
    for t, a, code, txt in ph.bank.select("bank_txn_id", "amount", "bank_code", "text").iter_rows():
        if st.claimed(t):
            continue
        y = lut.get((a > 0, code, pat(txt)))
        if y:
            st.classify(t, y)
