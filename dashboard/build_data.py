"""Build dashboard/data.json from a submission + LIVE data (run from repo root).

Usage: .venv/bin/python dashboard/build_data.py [submission.csv.gz] [phase]
Outcome per bank line:
  linked       - item rows, no exception      (Invoice linked)
  exception    - item rows, some exception    (Exception)
  category     - no item, category != UNIDENTIFIED (Category only)
  unidentified - category UNIDENTIFIED or no row at all (Unidentified)
"""
import json
import os
import sys

import polars as pl

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from bankrec.data import DATA, load_items_raw  # noqa: E402

sub_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "out", "sub_live_v2.csv.gz")
phase = sys.argv[2] if len(sys.argv) > 2 else "live"

sub = pl.read_csv(sub_path, infer_schema=False)
bank = pl.read_csv(os.path.join(DATA, phase, "bank_transactions.csv.gz"), infer_schema=False)
items = load_items_raw(phase).select(
    "item_id", "item_type", "party_name", "document_number", "currency", "total_amount")

agg = sub.group_by("bank_txn_id").agg(
    pl.col("item_id").is_not_null().any().alias("has_item"),
    pl.col("exception").is_not_null().any().alias("has_exc"),
    pl.col("category").drop_nulls().first().alias("category"),
    pl.len().alias("n_rows"),
)
b = bank.join(agg, on="bank_txn_id", how="left").with_columns(
    pl.col("has_item").fill_null(False), pl.col("has_exc").fill_null(False))
b = b.with_columns(pl.when(pl.col("has_item") & pl.col("has_exc")).then(pl.lit("exception"))
                   .when(pl.col("has_item")).then(pl.lit("linked"))
                   .when(pl.col("category").is_not_null() & (pl.col("category") != "UNIDENTIFIED"))
                   .then(pl.lit("category")).otherwise(pl.lit("unidentified")).alias("outcome"))

counts = b.group_by("outcome").len().to_dicts()
counts = {r["outcome"]: r["len"] for r in counts}
total = b.height
kpi = {
    "bank_transactions": total,
    "invoice_linked": counts.get("linked", 0) + counts.get("exception", 0),
    "category_only": counts.get("category", 0),
    "unidentified": counts.get("unidentified", 0),
    "period": [b["booking_date"].min(), b["booking_date"].max()],
}

# representative sample: 200 lines, stratified by outcome, deterministic
quota = {"linked": 90, "exception": 40, "category": 35, "unidentified": 35}
samp = []
for o, n in quota.items():
    part = b.filter(pl.col("outcome") == o)
    samp.append(part.sample(n=min(n, part.height), seed=7))
samp = pl.concat(samp).sort("booking_date", "bank_txn_id")

subj = sub.join(items, on="item_id", how="left")
examples = []
for r in samp.iter_rows(named=True):
    allocs = []
    lines = subj.filter(pl.col("bank_txn_id") == r["bank_txn_id"])
    for a in lines.iter_rows(named=True):
        if a["item_id"] is None:
            continue
        allocs.append({
            "item_id": a["item_id"], "document_number": a["document_number"] or "",
            "party_name": a["party_name"] or "", "currency": a["currency"] or r["currency"],
            "total": float(a["total_amount"]) if a["total_amount"] else None,
            "amount": float(a["amount"]), "exception": a["exception"] or "",
        })
    examples.append({
        "id": r["bank_txn_id"], "booking_date": r["booking_date"], "account": r["account_id"],
        "entity": r["entity"], "amount": float(r["amount"]), "currency": r["currency"],
        "party": r["counterparty_name"] or "", "text": r["text"] or "",
        "ref1": r["ref1"] or "", "ref2": r["ref2"] or "", "remittance": r["remittance_info"] or "",
        "outcome": r["outcome"], "category": (lines["category"].drop_nulls().to_list() or [""])[0],
        "allocations": allocs,
    })

ents = pl.read_csv(os.path.join(DATA, "masters", "entities.csv"), infer_schema=False)
entity_names = dict(zip(ents["entity"].to_list(), ents["legal_name"].to_list()))
payload = {"phase": phase, "kpi": kpi, "entities": entity_names, "examples": examples}
out = os.path.join(ROOT, "dashboard", "data.js")  # script tag, so index.html works from file://
with open(out, "w", encoding="utf-8") as f:
    f.write("window.DASHBOARD_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n")
print(json.dumps(kpi, ensure_ascii=False)); print(len(examples), "examples ->", out)
