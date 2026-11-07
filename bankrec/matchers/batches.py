"""File-driven matchers: SEPA/BACS/ACH direct-debit batches, card settlements, cheque deposits, payment runs.

POLICIES §5.3: a batch credit clears all receipts of the batch; returned receipts re-open the item (RETURNED).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from ..ctx import bank_rows, bank_text, item_info, ref_index
from ..refs import norm, tokens

TOL = 0.011


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _resolve(ph, st, ref: str, entity: str, amount: float, types=("ARI",), party_hint: str | None = None):
    """Item for a receipt/document reference: prefer same entity, matching type, amount ≈ total/open."""
    idx, info = ref_index(ph), item_info(ph)
    cands = []
    for tok in [norm(ref)] + tokens(ref):
        for iid in idx.get(tok, entity) or idx.get(tok):
            r = info.get(iid)
            if r and r["item_type"] in types:
                cands.append(iid)
        if cands:
            break
    if not cands:
        return None
    def score(iid):
        r = info[iid]
        s = 0
        if abs((r["total_amount"] or 0) - amount) < TOL or abs(st.open.get(iid, 0) - amount) < TOL:
            s += 4
        if r["entity"] == entity:
            s += 2
        if party_hint and r["party_id"] == party_hint:
            s += 1
        if st.open.get(iid, 0) > 0.005:
            s += 1
        return s
    return max(cands, key=score)


def _token_map(ph, keys: dict) -> dict:
    """bank_txn_id -> group key, for bank lines whose text contains one of `keys` (normalised)."""
    out = {}
    for row in bank_rows(ph):
        if row["bank_txn_id"] in out:
            continue
        txt = bank_text(row).upper()
        for tok in re.findall(r"[A-Z0-9][A-Z0-9\-]{6,}", txt):
            n = norm(tok)
            if n in keys:
                out[row["bank_txn_id"]] = n
                break
            # an end-to-end id may be glued to more chars or cut: try prefixes
    return out


# --------------------------------------------------------------------------- direct debits
def direct_debits(ph, st):
    dd = ph.aux("direct_debit_batches")
    if dd.is_empty():
        return
    acc_ent = dict(zip(ph.masters["bank_accounts"]["account_id"].to_list(), ph.masters["bank_accounts"]["entity"].to_list()))
    cust = ph.masters["customers"]
    mandate_party = dict(zip(cust["sepa_mandate_id"].to_list(), cust["customer_id"].to_list()))
    batches = defaultdict(list)
    for r in dd.iter_rows(named=True):
        batches[norm(r["batch_id"])].append(r)
    by_mandate = defaultdict(list)
    for b, rs in batches.items():
        for r in rs:
            by_mandate[norm(r["mandate_id"])].append(r)

    def item_of(r):
        ent = acc_ent.get(r["account_id"], "")
        return _resolve(ph, st, r["remittance_info"] or "", ent, _f(r["amount"]), ("ARI",), mandate_party.get(r["mandate_id"]))

    rows = {r["bank_txn_id"]: r for r in bank_rows(ph)}
    tmap = _token_map(ph, set(batches))
    # credits first (collections), then debits (returns)
    for t, b in sorted(tmap.items(), key=lambda kv: rows[kv[0]]["booking_date"]):
        row = rows[t]
        amt = row["amount"] or 0
        if st.claimed(t):
            continue
        recs = batches[b]
        if amt > 0:
            allocs = []
            for r in recs:
                iid = item_of(r)
                if iid:
                    allocs.append((iid, _f(r["amount"]), ""))
            st.assign(t, allocs)
        else:
            # returned receipt(s): find the receipt by mandate in the text, else by amount
            txt = norm(bank_text(row))
            hit = [r for r in recs if norm(r["mandate_id"]) and norm(r["mandate_id"]) in txt]
            if not hit:
                hit = [r for r in recs if abs(_f(r["amount"]) - abs(amt)) < TOL]
            if not hit:
                continue
            r = hit[0]
            iid = item_of(r)
            if iid:
                st.assign(t, [(iid, _f(r["amount"]), "RETURNED")])
    # returns that only quote the mandate (RUM / MREF / MANDATO ...)
    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = row["amount"] or 0
        if st.claimed(t) or amt >= 0:
            continue
        txt = bank_text(row).upper()
        for m in re.findall(r"VTM-[A-Z]{2}-\d{3,}", txt):
            recs = [r for r in by_mandate.get(norm(m), []) if r["collection_date"] <= row["booking_date"]
                    and abs(_f(r["amount"]) - abs(amt)) < TOL]
            if recs:
                r = max(recs, key=lambda x: x["collection_date"])
                iid = item_of(r)
                if iid:
                    st.assign(t, [(iid, _f(r["amount"]), "RETURNED")])
                break


# --------------------------------------------------------------------------- card settlements
def card_settlements(ph, st):
    cs = ph.aux("card_settlements")
    if cs.is_empty():
        return
    groups = defaultdict(list)
    for r in cs.iter_rows(named=True):
        groups[(r["settlement_id"], r["account_id"], r["settlement_date"])].append(r)
    acc_ent = dict(zip(ph.masters["bank_accounts"]["account_id"].to_list(), ph.masters["bank_accounts"]["entity"].to_list()))
    # index bank credits by (account, cents)
    by_amt = defaultdict(list)
    for row in bank_rows(ph):
        if (row["amount"] or 0) > 0:
            by_amt[(row["account_id"], round(row["amount"] * 100))].append(row)
    for (sid, acc, sdate), recs in groups.items():
        net = round(sum(_f(r["net"]) for r in recs) * 100)
        cands = [row for row in by_amt.get((acc, net), []) if not st.claimed(row["bank_txn_id"])]
        if not cands:
            continue
        sd = date.fromisoformat(sdate)
        row = min(cands, key=lambda x: abs((date.fromisoformat(x["booking_date"]) - sd).days))
        ent = acc_ent.get(acc, "")
        allocs = []
        for r in recs:
            iid = _resolve(ph, st, r["order_number"], ent, _f(r["gross"]), ("ARI",))
            if iid:
                allocs.append((iid, _f(r["gross"]), ""))
        st.assign(row["bank_txn_id"], allocs)


# --------------------------------------------------------------------------- cheque deposits
def cheque_deposits(ph, st):
    cq = ph.aux("cheque_deposits")
    if cq.is_empty():
        return
    acc_ent = dict(zip(ph.masters["bank_accounts"]["account_id"].to_list(), ph.masters["bank_accounts"]["entity"].to_list()))
    deps = defaultdict(list)
    for r in cq.iter_rows(named=True):
        deps[norm(r["deposit_id"])].append(r)
    rows = {r["bank_txn_id"]: r for r in bank_rows(ph)}
    info = item_info(ph)
    by_amt = defaultdict(list)
    for iid, r in info.items():
        if r["item_type"] == "ARI":
            by_amt[(r["entity"], round((r["total_amount"] or 0) * 100))].append(iid)

    def words(s):
        return {w for w in re.findall(r"[A-Z]{3,}", (s or "").upper()) if w not in {"SARL", "SAS", "EURL", "INC", "LLC", "SPA"}}

    for t, d in _token_map(ph, set(deps)).items():
        if st.claimed(t) or (rows[t]["amount"] or 0) <= 0:
            continue
        allocs, used = [], set()
        for r in deps[d]:
            ent = acc_ent.get(r["account_id"], "")
            amt = _f(r["amount"])
            iid = _resolve(ph, st, r["stub_reference"] or "", ent, amt, ("ARI",))
            if iid and abs((info[iid]["total_amount"] or 0) - amt) > TOL and abs(st.open.get(iid, 0) - amt) > TOL:
                iid = None
            if not iid:   # incomplete stub: drawer name + exact amount among open invoices
                dw = words(r["drawer"])
                cands = [i for i in by_amt.get((ent, round(amt * 100)), []) if st.open.get(i, 0) > 0.005 and i not in used]
                cands = sorted(cands, key=lambda i: (-len(dw & words(info[i]["party_name"])), info[i]["issue_date"]))
                if cands and (dw & words(info[cands[0]]["party_name"])):
                    iid = cands[0]
            if iid:
                used.add(iid)
                allocs.append((iid, amt, ""))
        st.assign(t, allocs)


# --------------------------------------------------------------------------- payment runs (AP)
def payment_runs(ph, st):
    pr = ph.aux("payment_runs")
    if pr.is_empty():
        return
    groups = defaultdict(list)   # key -> documents
    for r in pr.iter_rows(named=True):
        if r["run_type"] == "transfers":
            groups[norm(r["end_to_end_id"])].append(r)
        else:
            groups[norm(r["run_id"])].append(r)
    rows = {r["bank_txn_id"]: r for r in bank_rows(ph)}
    tmap = _token_map(ph, set(groups))
    for t, k in tmap.items():
        row = rows[t]
        if st.claimed(t):
            continue
        docs = groups[k]
        amt = abs(row["amount"] or 0)
        if docs and docs[0]["run_type"] == "confirming":
            # one debit per maturity date: keep documents whose transfer sum per date matches the debit
            by_date = defaultdict(list)
            for r in docs:
                by_date[r["execution_date"]].append(r)
            pick = None
            for dte, rs in by_date.items():
                tot = sum(_f(x["transfer_amount"]) for x in {x["end_to_end_id"]: x for x in rs}.values())
                if abs(tot - amt) < 0.02:
                    pick = rs
                    break
            docs = pick or docs
        allocs = []
        for r in docs:
            dt = (r["document_type"] or "").upper()
            types = ("APC", "ARC") if "CREDIT" in dt or _f(r["document_amount"]) < 0 else ("API", "ARC")
            iid = _resolve(ph, st, r["document_number"] or "", "", abs(_f(r["document_amount"])), types)
            if iid:
                allocs.append((iid, abs(_f(r["document_amount"])), ""))
        st.assign(t, allocs)
