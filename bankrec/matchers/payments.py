"""AP (supplier-side) payments not covered by payment runs (POLICIES §1-§5).

- ap_direct_debit: a supplier collects an AP invoice by SEPA/BACS direct debit from our account. The debit amount
  equals the open amount of one `dd` supplier invoice (FIFO when several have the same amount, text tie-breaks).
- ap_supplier_refund: a supplier pays back one of its credit notes (APC) by transfer. Credit-note amount equal and the
  supplier named in the bank text or the credit-note number quoted (precision: no name/reference, no claim).
- ap_run_transfers: a per-transfer debit quoting a payment-run reference `PR<12 digits>[-<seq>]` clears the documents
  of its transfer in `payment_runs/` (cut end-to-end ids, payment factory ES00, other-currency debits, partials).
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict

from ..ctx import account_entity, bank_rows, bank_text, item_info, ref_index, _cached

TOL = 0.011
LEGAL = {
    "PTY",
    "LTD",
    "GMBH",
    "CO",
    "KG",
    "SARL",
    "SAS",
    "BV",
    "INC",
    "LLC",
    "CORP",
    "SLU",
    "SONS",
    "LLP",
    "EURL",
    "SRL",
    "SPA",
    "SAU",
    "LDA",
    "UNIPESSOAL",
    "GROUP",
    "COMPANY",
    "LIMITED",
    "HOLDING",
    "AND",
    "THE",
}


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
        for r in ph.items.filter(ph.items["item_type"].is_in(["API", "APC"])).iter_rows(
            named=True
        ):
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
            if (
                r
                and r["payment_method"] == "dd"
                and (r["issue_date"] or "") <= row["booking_date"]
            ):
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
                continue  # precision: an amount alone is not enough (unidentified receipts look alike)
            s = 4 * ref + 2 * (name > 0) + (r["entity"] == ent)
            scored.append((-s, r["issue_date"] or "", iid))
        if not scored:
            continue
        scored.sort()
        if (
            len(scored) > 1
            and scored[0][0] == scored[1][0]
            and scored[0][1] == scored[1][1]
        ):
            continue  # genuinely ambiguous
        st.assign(t, [(scored[0][2], st.open[scored[0][2]], "")])


# --------------------------------------------------------------------------- per-transfer debits of a payment run
def _dkey(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", _fold(s))


def _runs(ph) -> dict:
    """(run_id, debtor account) -> {end_to_end_id -> [document rows]} for `transfers` runs (run ids repeat across
    entities); plus doc-number index of AP items."""

    def build():
        pr = ph.aux("payment_runs")
        runs = defaultdict(lambda: defaultdict(list))
        if not pr.is_empty():
            for r in pr.filter(pr["run_type"] == "transfers").iter_rows(named=True):
                runs[(r["run_id"], r["debtor_account_id"])][r["end_to_end_id"]].append(
                    r
                )
        docs = defaultdict(list)  # doc key -> [(item id, type, party key, party words)]
        words = {}
        for iid, typ, dn, pn in (
            ph.items.filter(ph.items["item_type"].is_in(["API", "APC"]))
            .select("item_id", "item_type", "document_number", "party_name")
            .iter_rows()
        ):
            if dn:
                if pn not in words:
                    words[pn] = (_dkey(pn), frozenset(_name_words(pn)))
                docs[_dkey(dn)].append((iid, typ) + words[pn])
        return {"runs": runs, "docs": docs}

    return _cached(ph, "_pay_runs", build)


def _fx_rates(ph) -> dict:
    """currency -> (sorted dates, units per EUR)."""

    def build():
        fx = ph.masters["fx_rates"]
        out = defaultdict(lambda: ([], []))
        for d, c, u in (
            fx.sort("date").select("date", "currency", "units_per_eur").iter_rows()
        ):
            out[c][0].append(str(d)[:10])
            out[c][1].append(_f(u))
        return dict(out)

    return _cached(ph, "_pay_fx", build)


def _rate(fx: dict, ccy: str, day: str) -> float:
    if ccy == "EUR":
        return 1.0
    if ccy not in fx:
        return 0.0
    ds, us = fx[ccy]
    i = bisect_right(ds, day) - 1
    return us[max(i, 0)]


OCMT = re.compile(r"/OCMT/([A-Z]{3})([\d.,]+)")


def _orig(row, txt: str):
    """(currency, amount) of the instructed amount when the bank shows it."""
    if row.get("orig_amount") and row.get("orig_currency"):
        return row["orig_currency"], abs(_f(row["orig_amount"]))
    m = OCMT.search(txt)
    if m:
        v = (
            m.group(2).replace(".", "").replace(",", ".")
            if "," in m.group(2)
            else m.group(2)
        )
        return m.group(1), _f(v)
    return None, 0.0


def _resolve_doc(st, info, docs, d, pkey, pwords) -> str | None:
    typ = (
        "APC"
        if "CREDIT" in (d["document_type"] or "").upper()
        or _f(d["document_amount"]) < 0
        else "API"
    )
    cands = [x for x in docs.get(_dkey(d["document_number"]), ()) if x[1] == typ]
    if not cands:
        return None
    da = abs(_f(d["document_amount"]))
    scored = []
    for i, _, k, w in cands:
        nm = 3 if k == pkey else len(w & pwords)
        o = st.open.get(i, 0.0)
        amt_ok = abs(o - da) < TOL or abs(_f(info[i]["total_amount"]) - da) < TOL
        scored.append((-nm, -amt_ok, -(o > 0.005), info[i]["issue_date"] or "", i))
    scored.sort()
    if scored[0][0] == 0 and len(scored) > 1:
        # beneficiary is not the supplier (factor / confirming): only a unique amount match identifies the item
        hits = [s for s in scored if s[1] and s[2]]
        return hits[0][-1] if len(hits) == 1 else None
    return scored[0][-1]


def ap_run_transfers(ph, st):
    """POLICIES §5.2-§5.3: a per-transfer debit clears the documents of its transfer in `payment_runs/`, also when the
    end-to-end id is cut to `PR<run>-0` (payment factory ES00 paying subsidiaries' invoices) or the debit is in another
    currency than the transfer (FX_DIFFERENCE). A document paid below the item's open amount is PARTIAL (§3)."""
    R = _runs(ph)
    runs, docs = R["runs"], R["docs"]
    if not runs:
        return
    info = item_info(ph)
    fx = _fx_rates(ph)
    used = set()
    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = _f(row["amount"])
        if amt >= 0 or st.claimed(t):
            continue
        txt = bank_text(row).upper()
        m = re.search(r"PR(\d{12})(?:-(\d{4}))?", txt)
        if not m:
            continue
        run = runs.get(("PR" + m.group(1), row["account_id"]))
        if not run:
            continue
        pay = -amt
        bccy = row["currency"]
        full = m.group(0) if m.group(2) else None
        if full and full in run:
            cands = [full]
        else:
            occy, oamt = _orig(row, txt)
            day = row["booking_date"] or ""
            cands = []
            for e2e, ds in run.items():
                if e2e in used:
                    continue
                tc, ta = ds[0]["currency"], _f(ds[0]["transfer_amount"])
                if tc == bccy:
                    ok = abs(ta - pay) < TOL
                elif occy:
                    ok = occy == tc and abs(ta - oamt) < TOL
                else:
                    rb, rt = _rate(fx, bccy, day), _rate(fx, tc, day)
                    ok = rb > 0 and rt > 0 and abs(ta * rb / rt - pay) <= 0.03 * pay
                if ok:
                    cands.append(e2e)
            if (
                len(cands) > 1
            ):  # tie-break: document numbers and beneficiary in the text
                key = _dkey(txt)
                sc = sorted(
                    (
                        (
                            sum(1 for d in run[e] if _dkey(d["document_number"]) in key)
                            + 2 * _name_hits(run[e][0]["beneficiary"], _fold(txt)),
                            e,
                        )
                        for e in cands
                    ),
                    reverse=True,
                )
                if sc[0][0] == sc[1][0]:
                    continue
                cands = [sc[0][1]]
        if len(cands) != 1:
            continue
        e2e = cands[0]
        allocs, seen = [], set()
        ben = run[e2e][0]["beneficiary"]
        pkey, pwords = _dkey(ben), frozenset(_name_words(ben))
        for d in run[e2e]:
            iid = _resolve_doc(st, info, docs, d, pkey, pwords)
            if not iid or iid in seen:
                continue
            seen.add(iid)
            da = abs(_f(d["document_amount"]))
            o = st.open.get(iid, 0.0)
            if info[iid]["item_type"] == "APC":
                allocs.append((iid, da, ""))
            elif o <= 0.005:  # already settled: we pay it again
                allocs.append((iid, da, "DUPLICATE_PAYMENT"))
            elif da < o - max(0.01, 0.005 * o):
                allocs.append((iid, da, "PARTIAL"))
            else:
                exc = "FX_DIFFERENCE" if info[iid]["currency"] != bccy else ""
                allocs.append((iid, o, exc))
        if allocs and st.assign(t, allocs):
            used.add(e2e)


def match(ph, st):
    """Registered in matchers/__init__.py before nonitem (after receipts, so AR receipts keep priority)."""
    for fn in (ap_run_transfers, ap_direct_debits, ap_supplier_refunds):
        try:
            fn(ph, st)
        except Exception as e:  # never crash the run
            print(f"   payments.{fn.__name__} failed: {e}")
