"""Data loading for every phase, with a parquet cache.

Phases:
  sample  -> data/sample (1 month of TRAIN, golden available, all items treated as open)
  dev     -> TRAIN window (default 2025-01-01..2025-06-30), golden available, open items at window start
             derived from the golden. This is our LIVE simulator: score here, not on the leaderboard.
  train   -> full TRAIN (heavy)
  live    -> data/live, open_items_at_start + live items (details joined from train)
  final   -> data/final, same as live (+ masters_final)
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "out", "cache")

ITEM_COLS = ["item_id", "item_type", "entity", "party_id", "party_name", "document_number", "issue_date", "due_date",
             "currency", "net_amount", "tax_amount", "total_amount", "payment_terms", "po", "payment_method",
             "related_document", "reason"]


def _read(path: str) -> pl.DataFrame:
    """Read a csv(.gz) as strings, cached as parquet."""
    os.makedirs(CACHE, exist_ok=True)
    key = os.path.relpath(path, DATA).replace(os.sep, "__") + ".parquet"
    cp = os.path.join(CACHE, key)
    if os.path.exists(cp) and os.path.getmtime(cp) >= os.path.getmtime(path):
        return pl.read_parquet(cp)
    df = pl.read_csv(path, infer_schema=False)
    df.write_parquet(cp)
    return df


def _money(df: pl.DataFrame, cols) -> pl.DataFrame:
    return df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in cols if c in df.columns])


def load_items_raw(folder: str) -> pl.DataFrame:
    """AR invoices + AP invoices + credit notes of one folder, unified schema."""
    p = os.path.join(DATA, folder)
    ar = _read(os.path.join(p, "ar_invoices.csv.gz")).select(
        "item_id", pl.lit("ARI").alias("item_type"), "entity", pl.col("customer_id").alias("party_id"),
        pl.col("customer_name").alias("party_name"), "document_number", "issue_date", "due_date", "currency",
        "net_amount", "tax_amount", "total_amount", "payment_terms", pl.col("customer_po").alias("po"),
        "payment_method", pl.lit(None, pl.Utf8).alias("related_document"), pl.lit(None, pl.Utf8).alias("reason"))
    ap = _read(os.path.join(p, "ap_invoices.csv.gz")).select(
        "item_id", pl.lit("API").alias("item_type"), "entity", pl.col("supplier_id").alias("party_id"),
        pl.col("supplier_name").alias("party_name"), pl.col("supplier_invoice_number").alias("document_number"),
        "issue_date", "due_date", "currency", "net_amount", "tax_amount", "total_amount", "payment_terms",
        pl.col("our_po").alias("po"), "payment_method", pl.lit(None, pl.Utf8).alias("related_document"),
        pl.lit(None, pl.Utf8).alias("reason"))
    cn = _read(os.path.join(p, "credit_notes.csv.gz")).select(
        "item_id", pl.when(pl.col("side") == "AR").then(pl.lit("ARC")).otherwise(pl.lit("APC")).alias("item_type"),
        "entity", "party_id", "party_name", "document_number", "issue_date", pl.lit(None, pl.Utf8).alias("due_date"),
        "currency", "net_amount", "tax_amount", "total_amount", pl.lit(None, pl.Utf8).alias("payment_terms"),
        pl.lit(None, pl.Utf8).alias("po"), pl.lit(None, pl.Utf8).alias("payment_method"), "related_document", "reason")
    return _money(pl.concat([ar, ap, cn]), ["net_amount", "tax_amount", "total_amount"])


def _open_from_golden(items: pl.DataFrame, golden: pl.DataFrame, bank: pl.DataFrame, start: str) -> pl.DataFrame:
    """Open amount of every item at `start`, replaying golden allocations of bank lines booked before start."""
    before = bank.filter(pl.col("booking_date") < start).select("bank_txn_id")
    g = golden.join(before, on="bank_txn_id").filter(pl.col("item_id").is_not_null() & (pl.col("item_id") != ""))
    g = g.with_columns(pl.col("amount").cast(pl.Float64))
    sign = pl.when(pl.col("exception") == "RETURNED").then(-1.0).otherwise(1.0)
    paid = g.group_by("item_id").agg((pl.col("amount") * sign).sum().alias("paid"))
    out = (items.filter(pl.col("issue_date") < start).join(paid, on="item_id", how="left")
           .with_columns((pl.col("total_amount") - pl.col("paid").fill_null(0.0)).round(2).alias("open_amount"))
           .drop("paid"))
    return out.filter(pl.col("open_amount") > 0.005)


@dataclass
class Phase:
    name: str
    folders: list               # folders whose aux files (batches, RAs, pdfs) are relevant
    bank: pl.DataFrame          # bank lines to answer
    items: pl.DataFrame         # candidate items with open_amount at phase start (+ items issued during the phase)
    golden: pl.DataFrame | None = None
    start: str = ""
    end: str = ""
    extra: dict = field(default_factory=dict)
    aux_folders: list = field(default_factory=list)   # folders to read aux files from (incl. the previous phase)

    @functools.cached_property
    def masters(self) -> dict:
        m = {}
        mdir = os.path.join(DATA, "masters")
        if self.name == "final" and os.path.isdir(os.path.join(DATA, "final", "masters_final")):
            mdir = os.path.join(DATA, "final", "masters_final")
        for f in os.listdir(mdir):
            if f.endswith(".csv"):
                m[f[:-4]] = pl.read_csv(os.path.join(mdir, f), infer_schema=False)
        return m

    def aux(self, kind: str) -> pl.DataFrame:
        """Concatenate an aux folder (direct_debit_batches, card_settlements, cheque_deposits, payment_runs)."""
        if kind in self.extra:
            return self.extra[kind]
        dfs = []
        lo = (date.fromisoformat(self.start) - timedelta(days=100)).strftime("%Y%m") if self.start else "000000"
        hi = self.end[:7].replace("-", "") if self.end else "999999"
        for fo in dict.fromkeys(self.aux_folders or self.folders):
            d = os.path.join(DATA, fo, kind)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                m = re.search(r"_(\d{6})(?:_\d+)?\.csv$", f)
                if m and not (lo <= m.group(1) <= hi):
                    continue
                if f.endswith(".csv"):
                    df = pl.read_csv(os.path.join(d, f), infer_schema=False)
                    dfs.append(df.with_columns(pl.lit(f).alias("_file"), pl.lit(fo).alias("_folder")))
        df = pl.concat(dfs, how="diagonal_relaxed") if dfs else pl.DataFrame()
        self.extra[kind] = df
        return df

    def index(self, kind: str) -> pl.DataFrame:
        """pdfs/index.csv or remittance_advices/index.csv with an absolute `path` column."""
        dfs = []
        for fo in self.folders:
            p = os.path.join(DATA, fo, kind, "index.csv")
            if os.path.exists(p):
                df = pl.read_csv(p, infer_schema=False)
                dfs.append(df.with_columns((pl.lit(os.path.join(DATA, fo, kind) + os.sep) + pl.col("file")).alias("path")))
        return pl.concat(dfs, how="diagonal_relaxed") if dfs else pl.DataFrame()


def _bank(folder: str) -> pl.DataFrame:
    df = _read(os.path.join(DATA, folder, "bank_transactions.csv.gz"))
    return _money(df, ["amount", "orig_amount"])


def load_phase(name: str, start: str = "2025-01-01", end: str = "2025-06-30") -> Phase:
    if name == "sample":
        bank = _bank("sample")
        items = load_items_raw("sample").with_columns(pl.col("total_amount").alias("open_amount"))
        golden = _read(os.path.join(DATA, "sample", "golden_allocations.csv.gz"))
        return Phase("sample", ["sample"], bank, items, golden, bank["booking_date"].min(), bank["booking_date"].max())
    if name in ("dev", "train"):
        bank_all = _bank("train")
        items_all = load_items_raw("train")
        golden = _read(os.path.join(DATA, "train", "golden_allocations.csv.gz"))
        if name == "train":
            start, end = bank_all["booking_date"].min(), bank_all["booking_date"].max()
            items = items_all.with_columns(pl.col("total_amount").alias("open_amount"))
            return Phase("train", ["train"], bank_all, items, golden, start, end)
        bank = bank_all.filter((pl.col("booking_date") >= start) & (pl.col("booking_date") <= end))
        opened = _open_from_golden(items_all, golden, bank_all, start)
        new = items_all.filter((pl.col("issue_date") >= start) & (pl.col("issue_date") <= end)).with_columns(
            pl.col("total_amount").alias("open_amount"))
        items = pl.concat([opened, new])
        golden = golden.join(bank.select("bank_txn_id"), on="bank_txn_id")
        return Phase("dev", ["train"], bank, items, golden, start, end)
    if name in ("live", "final"):
        prev = ["train"] if name == "live" else ["train", "live"]
        detail = pl.concat([load_items_raw(f) for f in prev if os.path.isdir(os.path.join(DATA, f))])
        ois = _money(_read(os.path.join(DATA, name, "open_items_at_start.csv.gz")), ["total_amount", "open_amount"])
        opened = ois.select("item_id", "open_amount").join(detail, on="item_id", how="left")
        # items unknown to the detail tables keep the columns of open_items_at_start
        missing = opened.filter(pl.col("item_type").is_null()).select("item_id")
        if missing.height:
            fill = ois.join(missing, on="item_id").rename({}).select(
                [c for c in ois.columns if c in ITEM_COLS or c == "open_amount"])
            opened = pl.concat([opened.filter(pl.col("item_type").is_not_null()), fill], how="diagonal_relaxed")
        new = load_items_raw(name).with_columns(pl.col("total_amount").alias("open_amount"))
        items = pl.concat([opened.select(new.columns), new])
        bank = _bank(name)
        return Phase(name, [name], bank, items, None, bank["booking_date"].min(), bank["booking_date"].max(),
                     aux_folders=[prev[-1], name])
    raise ValueError(name)
