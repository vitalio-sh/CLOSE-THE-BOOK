"""Document-number first: a line whose text names open items that add up exactly to the cash.

Runs last (after every other matcher), so it only sees lines nobody claimed. No payer resolution needed:
the document numbers in the bank text (exact keys of RefIndex, same entity) select the items, and the
signed open amounts must equal the cash to the cent. Positive cash -> AR items (ARI +, ARC -);
negative cash -> AP items (API +, APC -). Items must be open, in the line's currency and issued on or before
the booking date. Precision on dev before registration: 19,153 of 19,526 such lines equal the golden items.
"""
from __future__ import annotations

import polars as pl

from ..refs import RefIndex

AR = {"ARI": 1, "ARC": -1}
AP = {"API": 1, "APC": -1}


def _text(r: dict) -> str:
    return " ".join(str(r.get(c) or "") for c in ("text", "remittance_info", "ref1", "ref2", "end_to_end_id"))


def match(ph, st):
    items = ph.items
    ri = RefIndex(items)
    info = {r["item_id"]: r for r in items.select(
        "item_id", "item_type", "currency", "issue_date").iter_rows(named=True)}
    bank = ph.bank.sort("booking_date", "bank_txn_id")
    for r in bank.iter_rows(named=True):
        t = r["bank_txn_id"]
        if st.claimed(t):
            continue
        try:
            amt = float(r["amount"])
            if amt == 0:
                continue
            signs = AR if amt > 0 else AP
            d = r["booking_date"]
            hits = []
            for iid in ri.find(_text(r), r["entity"]):
                it = info.get(iid)
                if (it is None or it["item_type"] not in signs or it["currency"] != r["currency"]
                        or (it["issue_date"] or "") > d or st.open.get(iid, 0.0) <= 0.005):
                    continue
                hits.append(iid)
            if not hits:
                continue
            total = sum(signs[info[i]["item_type"]] * st.open[i] for i in hits)
            if abs(total - abs(amt)) <= 0.01:
                st.assign(t, [(i, st.open[i], "") for i in hits])
        except Exception:
            continue
