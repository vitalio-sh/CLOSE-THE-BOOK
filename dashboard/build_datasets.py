"""Write the dashboard's CSV datasets from the LIVE submission (run from repo root).

Outputs dashboard/datasets/*.csv. Methods map the pipeline's matcher names onto the screens' method names.
"""
import csv
import hashlib
import json
import os
import sys

import polars as pl

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from bankrec.data import DATA  # noqa: E402

SUB = os.path.join(ROOT, "out", "sub_live_v2.csv.gz")
OUT = os.path.join(ROOT, "dashboard", "datasets")
os.makedirs(OUT, exist_ok=True)

sub_pq = pl.read_parquet(os.path.join(ROOT, "out", "sub_live_v2.parquet"))
bank = pl.read_csv(os.path.join(DATA, "live", "bank_transactions.csv.gz"), infer_schema=False).with_columns(
    pl.col("amount").cast(pl.Float64))
sub = pl.read_csv(SUB, infer_schema=False)
lines = bank.height
covered = sub["bank_txn_id"].n_unique()

# --- methods (line counts by the matcher that claimed them) ---
m = sub_pq.group_by("bank_txn_id").agg(pl.col("matcher").first()).group_by("matcher").len()
mc = dict(zip(m["matcher"].to_list(), m["len"].to_list()))
batches = sum(mc.get(k, 0) for k in ["payment_runs", "direct_debits", "cheque_deposits", "card_settlements"])
invoice = mc.get("receipts", 0)
cats = mc.get("nonitem", 0)
fallback = mc.get("fallback", 0)
# fallback lines split by the category the fallback gave them
fb = sub_pq.filter(pl.col("matcher") == "fallback").group_by("bank_txn_id").agg(
    pl.col("category").drop_nulls().first().alias("category"))
unid = fb.filter(pl.col("category") == "UNIDENTIFIED").height
fb_cat = fb.height - unid
methods = [
    ("Invoice matching", invoice),
    ("Payment batches", batches),
    ("Unidentified", unid),
    ("Bank categories", cats),
    ("Fallback category", fb_cat),
    ("Remittance advice", mc.get("remittance", 0)),
]
assert sum(v for _, v in methods) == lines, "methods must cover every bank line"

# --- KPI totals, consistent with the outcome definitions ---
invoice_linked = invoice + batches
category_only = cats + fb_cat
unidentified = unid
assert invoice_linked + category_only + unidentified == lines

# --- currency totals ---
cur = bank.group_by("currency").agg(
    pl.len().alias("transactions"),
    pl.col("amount").filter(pl.col("amount") > 0).sum().alias("money_in"),
    pl.col("amount").filter(pl.col("amount") < 0).sum().alias("money_out"),
    pl.col("amount").sum().alias("net")).sort("transactions", descending=True)

# --- submission checks ---
path = SUB
sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
size_mib = os.path.getsize(path) / 1048576
n_adv = sum(1 for _ in csv.DictReader(open(os.path.join(DATA, "live", "remittance_advices", "index.csv"),
                                           encoding="utf-8"))) if os.path.exists(
    os.path.join(DATA, "live", "remittance_advices", "index.csv")) else 0
checks = [
    ("Every transaction represented", "Passed" if covered == lines else "Failed",
     f"{covered:,} of {lines:,} transactions covered"),
    ("Format validator (local)", "Passed", f"{sub.height:,} rows checked by scripts/run.py (local validator, not the organizer's)"),
    ("Organizer format validator", "Not run", "Official parser not run in this session"),
    ("File identity", "Computed", f"SHA-256 {sha[:16]}… of this file (not checked against a platform record)"),
    ("Code stable during the run", "Not checked", "Not verified in this session"),
    ("LIVE accuracy", "Not measured", "No hidden LIVE labels are available locally"),
    ("Remittance advice parsing", "Pending", f"{n_adv:,} advice files in the index; remittance.py not yet registered"),
]
submission = [
    ("File", "out/sub_live_v2.csv.gz"),
    ("Dataset", "LIVE"),
    ("Rows", f"{sub.height:,}"),
    ("Size", f"{size_mib:.2f} MiB (gzip) / 60 MiB limit"),
    ("SHA-256", sha),
    ("Created", "2026-11-07 10:34"),
]

# --- examples: 200 lines from the live example set used by dashboard/build_data.py ---
js = open(os.path.join(ROOT, "dashboard", "data.js"), encoding="utf-8").read()
payload = json.loads(js[js.index("{"):js.rindex("}") + 1])

def write(name, header, rows):
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(name, len(rows))

write("kpis.csv", ["metric", "value"], [
    ("bank_transactions", lines), ("invoice_linked", invoice_linked),
    ("category_only", category_only), ("unidentified", unidentified)])
write("methods.csv", ["method", "lines"], methods)
write("currencies.csv", ["currency", "transactions", "money_in", "money_out", "net"],
      [(r["currency"], r["transactions"], f"{r['money_in']:.2f}", f"{r['money_out']:.2f}", f"{r['net']:.2f}")
       for r in cur.to_dicts()])
write("checks.csv", ["check", "status", "detail"], checks)
write("submission.csv", ["field", "value"], submission)
write("examples.csv", ["id", "booking_date", "entity", "account", "currency", "amount", "party", "text", "ref1",
                       "ref2", "outcome", "category", "allocations_json"],
      [(e["id"], e["booking_date"], e["entity"], e["account"], e["currency"], f"{e['amount']:.2f}", e["party"],
        e["text"], e["ref1"], e["ref2"], e["outcome"], e["category"],
        json.dumps(e["allocations"], ensure_ascii=False)) for e in payload["examples"]])

ents = pl.read_csv(os.path.join(DATA, "masters", "entities.csv"), infer_schema=False)
write("entities.csv", ["entity", "legal_name"], list(zip(ents["entity"].to_list(), ents["legal_name"].to_list())))
