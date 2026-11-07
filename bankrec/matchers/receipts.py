"""AR receipts (bank credits) -> customer invoices / credit notes.

Steps per credit line:
  1. payer -> candidate customers: IBAN (counterparty_account / IBAN in text), then name tokens (inverted index);
     confirming / factoring "ORD. <customer>" handled by the name step; parent pays for its group (group_id).
  2. references in the text resolved against the candidates' open items (exact, no prefix, truncated, 1 typo).
  3. amount: referenced items (+ netted credit notes) explain the cash -> rows; single-item shortfalls ->
     SHORT_DISCOUNT / SHORT_WITHHOLDING / SHORT_BANK_CHARGES / PARTIAL; excess -> OVERPAYMENT; already settled ->
     DUPLICATE_PAYMENT; no reference -> exact subset of open items (FIFO first).
  4. FX: account currency != item currency -> compare via fx_rates with 3 % tolerance, FX_DIFFERENCE.
"""
from __future__ import annotations

import bisect
import re
from collections import defaultdict
from datetime import date, timedelta

import polars as pl

from ..ctx import bank_text, item_info
from ..refs import keys_for, norm, tokens

STOP = {"SL", "SA", "SAU", "SLU", "SAS", "SARL", "EURL", "GMBH", "CO", "KG", "EK", "AG", "LTD", "LLC", "INC", "SRL",
        "SPA", "BV", "SP", "ZOO", "LDA", "DE", "DEL", "LA", "LOS", "LAS", "Y", "ET", "AND", "THE", "UND", "COOP",
        "SCOOP", "CV", "RL", "SDE", "LIMITED", "PLC", "SPZOO", "TRANSFERENCIA", "SEPA", "PAYMENT", "PAGO", "FACTURA",
        "FRA", "FRAS", "ORD", "RECHNUNG", "VIREMENT", "ABONO", "CREDIT", "ACH", "CCD", "TRANSFER", "INVOICE", "INV"}
FX_TOL = 0.03
WITHHOLD = (0.0125, 0.015, 0.10)
SEPA = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU",
        "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "GB", "CH", "NO", "IS", "LI", "MC", "SM", "AD", "VA"}
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")


def _words(s) -> list:
    s = (s or "").upper()
    for a, b in (("Á", "A"), ("À", "A"), ("É", "E"), ("È", "E"), ("Ê", "E"), ("Í", "I"), ("Ó", "O"), ("Ö", "O"),
                 ("Ú", "U"), ("Ü", "U"), ("Ñ", "N"), ("Ç", "C"), ("ß", "SS"), ("Ä", "A"), ("Ł", "L"), ("Ś", "S"),
                 ("Ż", "Z"), ("Ę", "E"), ("Ą", "A"), ("Ć", "C"), ("Ó", "O"), ("Ã", "A"), ("Õ", "O"), ("Â", "A")):
        s = s.replace(a, b)
    return [w for w in re.findall(r"[A-Z]{3,}", s) if w not in STOP]


class Ctx:
    def __init__(self, ph):
        self.ph = ph
        cust = ph.masters["customers"]
        self.cust = {r["customer_id"]: r for r in cust.iter_rows(named=True)}
        self.by_iban = defaultdict(set)
        for c in self.cust.values():
            if c["iban"]:
                self.by_iban[norm(c["iban"])].add(c["customer_id"])
        self.group = defaultdict(set)
        for c in self.cust.values():
            if c["group_id"]:
                self.group[c["group_id"]].add(c["customer_id"])
        info = item_info(ph)
        self.info = info
        # items per party (AR side)
        self.by_party = defaultdict(list)
        for iid, r in info.items():
            if r["item_type"] in ("ARI", "ARC"):
                self.by_party[r["party_id"]].append(iid)
        for p in self.by_party:
            self.by_party[p].sort(key=lambda i: (info[i]["issue_date"] or "", info[i]["document_number"] or ""))
        # name inverted index per entity, names from master + invoices (renamed customers keep old names on invoices)
        names = defaultdict(set)
        for c in self.cust.values():
            names[c["customer_id"]].add(c["name"])
            if c["trade_name"]:
                names[c["customer_id"]].add(c["trade_name"])
        for iid, r in info.items():
            if r["item_type"] in ("ARI", "ARC") and r["party_name"]:
                names[r["party_id"]].add(r["party_name"])
        self.inv = defaultdict(set)
        self.nwords = {}
        for cid, ns in names.items():
            ent = self.cust.get(cid, {}).get("entity") or cid[1:5]
            ws = set()
            for n in ns:
                ws.update(_words(n))
            self.nwords[cid] = ws
            for w in ws:
                self.inv[(ent, w)].add(cid)
        self.df = {k: len(v) for k, v in self.inv.items()}
        # fx
        fx = ph.masters["fx_rates"]
        self.fx = defaultdict(list)
        for d, c, u in fx.select("date", "currency", "units_per_eur").iter_rows():
            self.fx[c].append((d, float(u)))
        for c in self.fx:
            self.fx[c].sort()
        terms = ph.masters["payment_terms"]
        self.terms = {r["code"]: r for r in terms.iter_rows(named=True)}
        acc = ph.masters["bank_accounts"]
        self.acc_ent = dict(zip(acc["account_id"].to_list(), acc["entity"].to_list()))

    def rate(self, cur: str, d: str) -> float:
        if cur == "EUR":
            return 1.0
        arr = self.fx.get(cur)
        if not arr:
            return 0.0
        i = bisect.bisect_right(arr, (d, 1e18)) - 1
        return arr[max(i, 0)][1]

    def convert(self, amount: float, frm: str, to: str, d: str) -> float:
        """amount in currency frm -> currency to (reference rate)."""
        if frm == to:
            return amount
        rf, rt = self.rate(frm, d), self.rate(to, d)
        if not rf or not rt:
            return 0.0
        return amount / rf * rt

    def payers(self, row: dict, ent: str) -> list:
        """Candidate customer ids, best first."""
        txt = bank_text(row)
        out = []
        ibans = set()
        if row.get("counterparty_account"):
            ibans.add(norm(row["counterparty_account"]))
        for m in _IBAN.finditer(txt.upper().replace(" ", "")):
            ibans.add(m.group(1))
        for ib in ibans:
            for cid in self.by_iban.get(ib, ()):
                if cid not in out:
                    out.append(cid)
        score = defaultdict(float)
        for w in set(_words(txt)):
            cs = self.inv.get((ent, w))
            if cs and len(cs) <= 60:
                for cid in cs:
                    score[cid] += 1.0 / len(cs)
        ranked = sorted(score.items(), key=lambda kv: -kv[1])
        for cid, s in ranked[:6]:
            if cid not in out and s >= 0.05:
                out.append(cid)
        return out


def _open_items(cx, st, cids, types=("ARI", "ARC")):
    out = []
    for cid in cids:
        for iid in cx.by_party.get(cid, ()):
            if cx.info[iid]["item_type"] in types and st.open.get(iid, 0) > 0.005:
                out.append(iid)
    return out


def _subset(values: list, target: int, max_n: int = 28):
    """Indices of a subset of `values` (cents, may be negative) summing to target. FIFO prefix first, then DP."""
    acc = 0
    for i, v in enumerate(values):
        acc += v
        if acc == target:
            return list(range(i + 1))
    vals = values[:max_n]
    pos = sum(v for v in vals if v > 0)
    neg = sum(v for v in vals if v < 0)
    if not (neg <= target <= pos):
        return None
    reach = {0: ()}
    for i, v in enumerate(vals):
        new = {}
        for s, idx in reach.items():
            ns = s + v
            if ns not in reach and ns not in new and (v < 0 or ns <= target - neg):
                new[ns] = idx + (i,)
                if ns == target:
                    return list(new[ns])
        reach.update(new)
        if len(reach) > 40_000:
            break
    return None


def _ref_items(cx, row, cands):
    """Candidate items referenced in the text (exact variant keys, truncated prefix, or one typo)."""
    toks = [t for t in tokens(bank_text(row)) if len(t) >= 5]
    if not toks or not cands:
        return []
    keymap = defaultdict(set)
    full = {}
    for iid in cands:
        dn = cx.info[iid]["document_number"]
        for k in keys_for(dn):
            keymap[k].add(iid)
        if cx.info[iid].get("po"):
            keymap[norm(cx.info[iid]["po"])].add(iid)
        full[iid] = norm(dn)
    out = []
    for t in toks:
        hit = keymap.get(t)
        if not hit:
            # truncated (prefix of the full number) or one-character typo / transposition
            hit = {i for i, f in full.items() if (len(t) >= 7 and f.startswith(t)) or (len(t) == len(f) and _near(t, f))}
            if len(hit) > 1:
                hit = set()
        for i in hit or ():
            if i not in out:
                out.append(i)
    return out


def _near(a: str, b: str) -> bool:
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def _val(cx, st, iid, cur_acc, d):
    """Open amount of item converted to account currency (signed: credit notes negative)."""
    r = cx.info[iid]
    v = st.open.get(iid, 0.0)
    if r["currency"] != cur_acc:
        v = cx.convert(v, r["currency"], cur_acc, d)
    return -v if r["item_type"] in ("ARC", "APC") else v


def _single_exception(cx, st, iid, cash_item_cur, row, cust):
    """Cash (in item currency) less than / more than the open amount of one item -> (amount, exception)."""
    r = cx.info[iid]
    op = st.open.get(iid, 0.0)
    total = r["total_amount"] or op
    diff = op - cash_item_cur
    tol = max(0.01, 0.005 * op)
    if abs(diff) <= 0.01:
        return op, ""
    if diff < 0:
        return op, "OVERPAYMENT"
    t = cx.terms.get(r["payment_terms"] or "")
    if t and t["early_payment_discount_pct"]:
        pct = float(t["early_payment_discount_pct"])
        disc_cash = round(total - round(total * pct / 100, 2), 2)
        if abs(disc_cash - cash_item_cur) <= 0.011 and abs(op - total) < 0.01:
            limit = date.fromisoformat(r["issue_date"]) + timedelta(days=int(t["discount_days"] or 0) + 3)
            if date.fromisoformat(row["booking_date"]) <= limit:
                return op, "SHORT_DISCOUNT"
            return cash_item_cur, "PARTIAL"
    country = (cust or {}).get("country") or ""
    if country in ("MX", "BR", "MA"):
        for w in WITHHOLD:
            if abs(round(total * (1 - w), 2) - cash_item_cur) <= 0.02:
                return op, "SHORT_WITHHOLDING"
    if country and country not in SEPA:
        short_eur = cx.convert(diff, r["currency"], "EUR", row["booking_date"]) if r["currency"] != "EUR" else diff
        if short_eur <= 60 and diff <= 0.02 * op:
            return op, "SHORT_BANK_CHARGES"
    return cash_item_cur, "PARTIAL"


def _assign_set(cx, st, t, row, items, fx: bool):
    allocs = []
    for iid in items:
        exc = ""
        if fx and cx.info[iid]["item_type"] == "ARI":
            exc = "FX_DIFFERENCE"
        allocs.append((iid, st.open.get(iid, 0.0), exc))
    return st.assign(t, allocs)


def match(ph, st):
    cx = Ctx(ph)
    ph.extra["_receipts_ctx"] = cx
    rows = [r for r in ph.bank.filter(pl.col("amount") > 0).sort("booking_date").to_dicts()]
    settled_once = defaultdict(int)
    for r in rows:
        t = r["bank_txn_id"]
        if st.claimed(t):
            continue
        ent = r["entity"]
        cur = r["currency"]
        d = r["booking_date"]
        amt = r["amount"]
        cids = cx.payers(r, ent)
        if not cids:
            continue
        # a parent may pay for its group: extend with the group of the best payer
        best = cids[0]
        grp = cx.cust.get(best, {}).get("group_id")
        fam = list(cids[:3])
        if grp:
            fam += [c for c in cx.group[grp] if c not in fam]
        cands = _open_items(cx, st, fam)
        cands = [i for i in cands if cx.info[i]["entity"] == ent or cx.info[i]["currency"] != cur or True]
        cands = [i for i in cands if (cx.info[i]["issue_date"] or "") <= d]
        fx = any(cx.info[i]["currency"] != cur for i in cands)
        refs = _ref_items(cx, r, cands)
        target = round(amt * 100)

        def cents(iid):
            return round(_val(cx, st, iid, cur, d) * 100)

        def close(a, b):
            return abs(a - b) <= max(1, (FX_TOL if fx else 0.0) * abs(b))

        # 1. referenced items explain the cash (optionally with open credit notes of the payer)
        if refs:
            s = sum(cents(i) for i in refs)
            if close(s, target):
                _assign_set(cx, st, t, r, refs, any(cx.info[i]["currency"] != cur for i in refs))
                continue
            cns = [i for i in cands if cx.info[i]["item_type"] == "ARC" and i not in refs]
            if cns:
                idx = _subset([cents(i) for i in cns], target - s, 12)
                if idx is not None:
                    _assign_set(cx, st, t, r, refs + [cns[k] for k in idx], False)
                    continue
            if len(refs) == 1:
                iid = refs[0]
                ic = cx.info[iid]["currency"]
                cash = amt if ic == cur else cx.convert(amt, cur, ic, d)
                a, e = _single_exception(cx, st, iid, cash, r, cx.cust.get(cx.info[iid]["party_id"]))
                if ic != cur and e in ("", "SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES", "OVERPAYMENT"):
                    # within FX tolerance it is a full clearing with FX difference
                    if abs(cash - st.open.get(iid, 0)) <= FX_TOL * st.open.get(iid, 0):
                        a, e = st.open.get(iid, 0), "FX_DIFFERENCE"
                st.assign(t, [(iid, a, e)])
                continue
        # 2. no usable reference: exact amount / subset of the payer's open items (FIFO order)
        inv = [i for i in cands if cx.info[i]["item_type"] == "ARI"]
        same = [i for i in inv if close(cents(i), target)]
        if same:
            _assign_set(cx, st, t, r, [same[0]], cx.info[same[0]]["currency"] != cur)
            continue
        vals = [cents(i) for i in cands]
        if not fx and 1 < len(cands) <= 60:
            idx = _subset(vals, target)
            if idx is not None:
                _assign_set(cx, st, t, r, [cands[k] for k in idx], False)
                continue
        # 3. a referenced item that is already settled -> duplicate payment
        idx_all = [i for c in cids[:2] for i in cx.by_party.get(c, ()) if st.open.get(i, 0) <= 0.005
                   and cx.info[i]["item_type"] == "ARI" and abs((cx.info[i]["total_amount"] or 0) - amt) < 0.01]
        refs_all = _ref_items(cx, r, idx_all)
        if refs_all:
            st.assign(t, [(refs_all[0], amt, "DUPLICATE_PAYMENT")])
            continue
