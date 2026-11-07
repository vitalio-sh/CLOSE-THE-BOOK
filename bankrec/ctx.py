"""Shared, lazily-built lookups on a Phase (cached in ph.extra)."""
from __future__ import annotations

import polars as pl

from .data import Phase
from .refs import RefIndex


def _cached(ph: Phase, key: str, fn):
    if key not in ph.extra:
        ph.extra[key] = fn()
    return ph.extra[key]


def item_info(ph: Phase) -> dict:
    """item_id -> dict of the item row."""
    return _cached(ph, "_item_info", lambda: {r["item_id"]: r for r in ph.items.iter_rows(named=True)})


def ref_index(ph: Phase) -> RefIndex:
    return _cached(ph, "_ref_index", lambda: RefIndex(ph.items))


def ref_index_po(ph: Phase) -> RefIndex:
    return _cached(ph, "_ref_index_po", lambda: RefIndex(ph.items.filter(pl.col("po").is_not_null()), cols=("po",)))


def bank_text(row: dict) -> str:
    return " ".join(str(row.get(c) or "") for c in
                    ("text", "ref1", "ref2", "end_to_end_id", "remittance_info", "counterparty_name"))


def bank_rows(ph: Phase) -> list:
    return _cached(ph, "_bank_rows", lambda: ph.bank.to_dicts())


def account_entity(ph: Phase) -> dict:
    return _cached(ph, "_acc_ent", lambda: dict(zip(ph.masters["bank_accounts"]["account_id"].to_list(),
                                                     ph.masters["bank_accounts"]["entity"].to_list())))


def open_amount(st, item_id: str) -> float:
    return st.open.get(item_id, 0.0)
