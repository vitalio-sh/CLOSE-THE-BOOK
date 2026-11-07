"""AP (supplier-side) payments not covered by payment runs (POLICIES §1-§5).

- ap_direct_debit: a supplier collects an AP invoice by SEPA/BACS direct debit from our account. The debit amount
  equals the open amount of one `dd` supplier invoice (FIFO when several have the same amount, text tie-breaks).
- ap_supplier_refund: a supplier pays back one of its credit notes (APC) by transfer. Credit-note amount equal and the
  supplier named in the bank text or the credit-note number quoted (precision: no name/reference, no claim).
- ap_paid_by_holding: a transfer from a holding/payment-factory account (ES00 ...) pays several invoices of ONE supplier
  booked in a subsidiary. The payment reference `PR<ent><yy><mm><dd><seq>` encodes the paying entity number and the
  booking date. The lines are matched to a subset of that supplier's open invoices summing to the debit.
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta

from ..ctx import account_entity, bank_rows, bank_text, item_info, ref_index, _cached

TOL = 0.011
LEGAL = {"PTY", "LTD", "GMBH", "CO", "KG", "SARL", "SAS", "BV", "INC", "LLC", "CORP", "SLU", "SONS", "LLP", "EURL",
         "SRL", "SPA", "SAU", "LDA", "UNIPESSOAL", "GROUP", "COMPANY", "LIMITED", "HOLDING", "AND", "THE"}
PR_REF = re.compile(r"PR(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")
MAX_SUBSET_ITEMS = 8
MAX_SUBSET_SIZE = 5


def _days_before(iso: str, days: int) -> str:
    try:
        return (date.fromisoformat(iso[:10]) - timedelta(days=days)).isoformat()
    except ValueError:
        return ""


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _fold(s) -> str:
    """Upper-case, accents removed."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).upper()


def _name_words(name) -> list:
    return [w for w in re.findall(r"[A-Z]{4,}", _fold(name)) if w not in LEGAL]


def _name_hits(name, text_u: str) -> int:
    return sum(1 for w in _name_words(name) if w in text_u)


def _index(ph) -> dict:
    """Open AP items by open-amount cents, and transfer-paid AP items by (entity, party)."""
    def build():
        by_cents = defaultdict(list)
        by_party = defaultdict(list)
        for r in ph.items.filter(ph.items["item_type"].is_in(["API", "APC"])).iter_rows(named=True):
            oa = _f(r["open_amount"])
            if oa <= 0.005:
                continue
            by_cents[(r["item_type"], round(oa * 100))].append(r["item_id"])
            if r["item_type"] == "API" and r["payment_method"] != "dd":
                by_party[(r["entity"], r["party_id"])].append(r["item_id"])
        return {"cents": by_cents, "party": by_party}
    return _cached(ph, "_pay_index", build)


def _by_amount(ph, st, typ: str, amt: float) -> list:
    idx = _index(ph)["cents"]
    c = round(amt * 100)
    out = []
    for k in (c - 1, c, c + 1):
        for iid in idx.get((typ, k), []):
            if st.open.get(iid, 0.0) > 0.005 and abs(st.open[iid] - amt) < TOL:
                out.append(iid)
    return out


# --------------------------------------------------------------------------- direct debits by suppliers
def ap_direct_debits(ph, st):
    info = item_info(ph)
    ri = ref_index(ph)
    acc_ent = account_entity(ph)
    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = _f(row["amount"])
        if amt >= 0 or st.claimed(t):
            continue
        pay = -amt
        cands = []
        for iid in _by_amount(ph, st, "API", pay):
            r = info.get(iid)
            if r and r["payment_method"] == "dd" and (r["issue_date"] or "") <= row["booking_date"]:
                cands.append(iid)
        if not cands:
            continue
        txt = bank_text(row)
        txt_u = _fold(txt)
        ref_hits = set(ri.find(txt))
        ent = acc_ent.get(row["account_id"], "")
        scored = []
        for iid in cands:
            r = info[iid]
            s = 0
            if r["entity"] == ent:
                s += 1
            if _name_hits(r["party_name"], txt_u):
                s += 2
            if iid in ref_hits:
                s += 4
            scored.append((-s, r["issue_date"] or "", iid))
        scored.sort()
        best = scored[0][2]
        st.assign(t, [(best, st.open[best], "")])


# --------------------------------------------------------------------------- supplier refunds (APC)
def ap_supplier_refunds(ph, st):
    info = item_info(ph)
    ri = ref_index(ph)
    acc_ent = account_entity(ph)
    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = _f(row["amount"])
        if amt <= 0 or st.claimed(t):
            continue
        cands = _by_amount(ph, st, "APC", amt)
        if not cands:
            continue
        txt = bank_text(row)
        txt_u = _fold(txt)
        ref_hits = set(ri.find(txt))
        ent = acc_ent.get(row["account_id"], "")
        scored = []
        for iid in cands:
            r = info[iid]
            name = _name_hits(r["party_name"], txt_u)
            ref = iid in ref_hits
            if not (name or ref):
                continue   # precision: an amount alone is not enough (unidentified receipts look alike)
            s = 4 * ref + 2 * (name > 0) + (r["entity"] == ent)
            scored.append((-s, r["issue_date"] or "", iid))
        if not scored:
            continue
        scored.sort()
        if len(scored) > 1 and scored[0][0] == scored[1][0] and scored[0][1] == scored[1][1]:
            continue   # genuinely ambiguous
        st.assign(t, [(scored[0][2], st.open[scored[0][2]], "")])


# --------------------------------------------------------------------------- paid by holding (PR reference)
def _subset_solutions(items: list, target_c: int) -> list:
    """All minimal-size subsets (cents) of `items` (list of (iid, cents)) summing to target_c."""
    n = len(items)
    for size in range(1, min(n, MAX_SUBSET_SIZE) + 1):
        sols = [c for c in itertools.combinations(items, size) if sum(x[1] for x in c) == target_c]
        if sols:
            return [[x[0] for x in c] for c in sols]
    return []


def ap_paid_by_holding(ph, st):
    info = item_info(ph)
    party_idx = _index(ph)["party"]
    # entity number ("02") -> list of (issue_date, item id, party id), oldest first
    by_no = defaultdict(list)
    for (ent, party), ids in party_idx.items():
        for iid in ids:
            by_no[ent[-2:]].append((info[iid]["issue_date"] or "", iid, party))
    for v in by_no.values():
        v.sort()
    dates = {k: [x[0] for x in v] for k, v in by_no.items()}

    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = _f(row["amount"])
        if amt >= 0 or st.claimed(t):
            continue
        m = PR_REF.search((bank_text(row) or "").upper())
        if not m:
            continue
        ent_no, yy, mm, dd = m.group(1), m.group(3), m.group(4), m.group(5)
        bd = row["booking_date"] or ""
        if not (bd[2:4] == yy and bd[5:7] == mm and bd[8:10] == dd):
            continue
        if ent_no not in by_no:
            continue
        target_c = round(-amt * 100)
        # invoices issued up to the payment date and within ~150 days before it
        lo_date = _days_before(bd, 150)
        hi = bisect_right(dates[ent_no], bd)
        lo = bisect_left(dates[ent_no], lo_date)
        per_party = defaultdict(list)
        for _, iid, party in by_no[ent_no][lo:hi]:
            if st.open.get(iid, 0.0) > 0.005 and round(st.open[iid] * 100) <= target_c:
                per_party[party].append((iid, round(st.open[iid] * 100)))
        found = []   # (number of invoices, oldest invoice date, party, item ids)
        for party, avail in per_party.items():
            if sum(c for _, c in avail) < target_c:
                continue
            avail.sort(key=lambda x: info[x[0]]["issue_date"] or "")
            avail = avail[-MAX_SUBSET_ITEMS:]
            for sol in _subset_solutions(avail, target_c):
                found.append((len(sol), min(info[i]["issue_date"] or "" for i in sol), party, sol))
        if not found:
            continue
        found.sort(key=lambda x: (x[0], x[1]))
        # several suppliers explain the same debit with the same number of invoices: no claim
        if len(found) > 1 and found[0][:2] == found[1][:2] and found[0][2] != found[1][2]:
            continue
        st.assign(t, [(i, st.open[i], "") for i in found[0][3]])



def match(ph, st):
    """Registered in matchers/__init__.py before nonitem (after receipts, so AR receipts keep priority)."""
    for fn in (ap_direct_debits, ap_paid_by_holding, ap_supplier_refunds):
        try:
            fn(ph, st)
        except Exception as e:  # never crash the run
            print(f"   payments.{fn.__name__} failed: {e}")
