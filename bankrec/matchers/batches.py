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


def _resolve(
    ph,
    st,
    ref: str,
    entity: str,
    amount: float,
    types=("ARI",),
    party_hint: str | None = None,
):
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
        if (
            abs((r["total_amount"] or 0) - amount) < TOL
            or abs(st.open.get(iid, 0) - amount) < TOL
        ):
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
    acc_ent = dict(
        zip(
            ph.masters["bank_accounts"]["account_id"].to_list(),
            ph.masters["bank_accounts"]["entity"].to_list(),
        )
    )
    cust = ph.masters["customers"]
    mandate_party = dict(
        zip(cust["sepa_mandate_id"].to_list(), cust["customer_id"].to_list())
    )
    batches = defaultdict(list)
    for r in dd.iter_rows(named=True):
        batches[norm(r["batch_id"])].append(r)
    by_mandate = defaultdict(list)
    for b, rs in batches.items():
        for r in rs:
            by_mandate[norm(r["mandate_id"])].append(r)

    collected = {}  # (batch, end_to_end) -> item settled by the collection

    def item_of(r, ret=False):
        if ret and (r["batch_id"], r["end_to_end_id"]) in collected:
            return collected[(r["batch_id"], r["end_to_end_id"])]
        ent = acc_ent.get(r["account_id"], "")
        party = mandate_party.get(r["mandate_id"])
        amt = _f(r["amount"])
        idx = ref_index(ph)

        def amt_ok(i):
            return (
                abs((info[i]["total_amount"] or 0) - amt) < TOL
                or abs(st.open.get(i, 0) - amt) < TOL
            )

        # reference of the receipt, constrained to the mandate's customer and the receipt amount
        ref = r["remittance_info"] or ""
        for tok in [norm(ref)] + tokens(ref):
            c = [
                i
                for i in idx.get(tok, ent)
                if info[i]["item_type"] == "ARI"
                and amt_ok(i)
                and (not party or info[i]["party_id"] == party)
            ]
            if (
                c
            ):  # a return re-opens a settled invoice, a collection settles an open one
                return max(
                    c,
                    key=lambda i: (
                        (st.open.get(i, 0) > 0.005) != ret,
                        info[i]["issue_date"] or "",
                    ),
                )
        # no usable reference: oldest open invoice of the customer with that amount
        if party:
            c = [
                i
                for i in by_party.get(party, ())
                if info[i]["entity"] == ent
                and amt_ok(i)
                and (st.open.get(i, 0) > 0.005) != ret
            ]
            if c:
                return min(c, key=lambda i: info[i]["issue_date"] or "")
        return _resolve(ph, st, ref, ent, amt, ("ARI",), party)

    info = item_info(ph)
    by_party = defaultdict(list)
    for iid, it in info.items():
        if it["item_type"] == "ARI":
            by_party[it["party_id"]].append(iid)

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
                    collected[(r["batch_id"], r["end_to_end_id"])] = iid
            st.assign(t, allocs)
        else:
            # returned receipt(s): find the receipt by mandate in the text, else by amount
            txt = norm(bank_text(row))
            hit = [
                r
                for r in recs
                if norm(r["mandate_id"]) and norm(r["mandate_id"]) in txt
            ]
            if not hit:
                hit = [r for r in recs if abs(_f(r["amount"]) - abs(amt)) < TOL]
            if not hit:
                continue
            r = hit[0]
            iid = item_of(r, ret=True)
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
            recs = [
                r
                for r in by_mandate.get(norm(m), [])
                if r["collection_date"] <= row["booking_date"]
                and abs(_f(r["amount"]) - abs(amt)) < TOL
            ]
            if recs:
                r = max(recs, key=lambda x: x["collection_date"])
                iid = item_of(r, ret=True)
                if iid:
                    st.assign(t, [(iid, _f(r["amount"]), "RETURNED")])
                break
            # collection file not available: the mandate customer's invoice of that amount, settled first
            party = mandate_party.get(m)
            ent = acc_ent.get(row["account_id"], "")
            c = [
                i
                for i in by_party.get(party, ())
                if info[i]["entity"] == ent
                and abs((info[i]["total_amount"] or 0) - abs(amt)) < TOL
                and (info[i]["issue_date"] or "") <= row["booking_date"]
            ]
            closed = [i for i in c if st.open.get(i, 0) <= 0.005]
            pick = (
                max(closed, key=lambda i: info[i]["issue_date"] or "")
                if closed
                else (c[0] if len(c) == 1 else None)
            )
            if pick:
                st.assign(t, [(pick, abs(amt), "RETURNED")])
            break


# --------------------------------------------------------------------------- card settlements
def card_settlements(ph, st):
    cs = ph.aux("card_settlements")
    if cs.is_empty():
        return
    groups = defaultdict(list)
    for r in cs.iter_rows(named=True):
        groups[(r["settlement_id"], r["account_id"], r["settlement_date"])].append(r)
    acc_ent = dict(
        zip(
            ph.masters["bank_accounts"]["account_id"].to_list(),
            ph.masters["bank_accounts"]["entity"].to_list(),
        )
    )
    # index bank credits by (account, cents)
    by_amt = defaultdict(list)
    for row in bank_rows(ph):
        if (row["amount"] or 0) > 0:
            by_amt[(row["account_id"], round(row["amount"] * 100))].append(row)
    for (sid, acc, sdate), recs in groups.items():
        net = round(sum(_f(r["net"]) for r in recs) * 100)
        cands = [
            row
            for row in by_amt.get((acc, net), [])
            if not st.claimed(row["bank_txn_id"])
        ]
        if not cands:
            continue
        sd = date.fromisoformat(sdate)
        row = min(
            cands, key=lambda x: abs((date.fromisoformat(x["booking_date"]) - sd).days)
        )
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
    acc_ent = dict(
        zip(
            ph.masters["bank_accounts"]["account_id"].to_list(),
            ph.masters["bank_accounts"]["entity"].to_list(),
        )
    )
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
        return {
            w
            for w in re.findall(r"[A-Z]{3,}", (s or "").upper())
            if w not in {"SARL", "SAS", "EURL", "INC", "LLC", "SPA"}
        }

    for t, d in _token_map(ph, set(deps)).items():
        if st.claimed(t) or (rows[t]["amount"] or 0) <= 0:
            continue
        allocs, used = [], set()
        for r in deps[d]:
            ent = acc_ent.get(r["account_id"], "")
            amt = _f(r["amount"])
            iid = _resolve(ph, st, r["stub_reference"] or "", ent, amt, ("ARI",))
            if (
                iid
                and abs((info[iid]["total_amount"] or 0) - amt) > TOL
                and abs(st.open.get(iid, 0) - amt) > TOL
            ):
                iid = None
            if (
                not iid
            ):  # incomplete stub: drawer name + exact amount among open invoices
                dw = words(r["drawer"])
                cands = [
                    i
                    for i in by_amt.get((ent, round(amt * 100)), [])
                    if st.open.get(i, 0) > 0.005 and i not in used
                ]
                cands = sorted(
                    cands,
                    key=lambda i: (
                        -len(dw & words(info[i]["party_name"])),
                        info[i]["issue_date"],
                    ),
                )
                if cands and (dw & words(info[cands[0]]["party_name"])):
                    iid = cands[0]
            if iid:
                used.add(iid)
                allocs.append((iid, amt, ""))
        st.assign(t, allocs)


# --------------------------------------------------------------------------- payment runs (AP)
FACTORY = {"ES00": ("ES00", "ES01", "ES02", "PT01")}  # POLICIES §5.2 payment factory


def _words(s) -> set:
    return {
        w
        for w in re.findall(r"[A-Z0-9]{3,}", (s or "").upper())
        if w
        not in {
            "S",
            "SL",
            "SA",
            "SLU",
            "SAS",
            "GMBH",
            "LTD",
            "LTDA",
            "INC",
            "LLC",
            "SPA",
            "COOP",
            "PTY",
            "EURL",
            "SARL",
        }
    }


def _next_bday(d: str) -> str:
    x = date.fromisoformat(d)
    while x.weekday() >= 5:
        x = date.fromordinal(x.toordinal() + 1)
    return x.isoformat()


def payment_runs(ph, st):
    pr = ph.aux("payment_runs")
    if pr.is_empty():
        return
    info = item_info(ph)
    acc_ent = dict(
        zip(
            ph.masters["bank_accounts"]["account_id"].to_list(),
            ph.masters["bank_accounts"]["entity"].to_list(),
        )
    )
    sup = ph.masters["suppliers"]
    by_iban, by_name = defaultdict(set), defaultdict(set)
    for sid, nm, ib in zip(
        sup["supplier_id"].to_list(), sup["name"].to_list(), sup["iban"].to_list()
    ):
        if ib:
            by_iban[norm(ib)].add(sid)
        by_name[norm(nm)].add(sid)
    by_doc = defaultdict(list)  # exact printed number -> AP/AR items
    for iid, r in info.items():
        if r["item_type"] in ("API", "APC", "ARC"):
            by_doc[norm(r["document_number"])].append(iid)

    def resolve(r, ents, parties):
        neg = (
            "CREDIT" in (r["document_type"] or "").upper()
            or _f(r["document_amount"]) < 0
        )
        types = ("APC", "ARC") if neg else ("API", "ARC")
        damt = abs(_f(r["document_amount"]))
        best, bs, n_ent = None, -1, 0
        for iid in by_doc.get(norm(r["document_number"]), ()):
            it = info[iid]
            if it["item_type"] not in types:
                continue
            s = 0
            if it["party_id"] in parties:
                s += 8
            if it["entity"] in ents:
                s += 4
                n_ent += 1
            if (
                abs((it["total_amount"] or 0) - damt) < TOL
                or abs(st.open.get(iid, 0) - damt) < TOL
            ):
                s += 2
            if st.open.get(iid, 0) > 0.005:
                s += 1
            if it["item_type"] == "ARC":
                s -= 1
            if s > bs:
                best, bs = iid, s
        # precision: the supplier, or same entity with the amount, or the only such number in the entity
        return best if bs >= 6 or (bs >= 4 and n_ent == 1) else None

    # documents per transfer (end_to_end_id) and per run
    by_e2e = defaultdict(list)
    for r in pr.iter_rows(named=True):
        by_e2e[norm(r["end_to_end_id"])].append(r)
    by_run = defaultdict(dict)  # run -> e2e -> docs
    for k, docs in by_e2e.items():
        by_run[norm(docs[0]["run_id"])][k] = docs
    run_len = {len(k) for k in by_run}

    resolved = {}

    def allocs_of(k):
        if k in resolved:
            return resolved[k]
        docs = by_e2e[k]
        r0 = docs[0]
        ent = acc_ent.get(r0["debtor_account_id"], "")
        ents = FACTORY.get(ent, (ent,))
        parties = set(by_iban.get(norm(r0["beneficiary_iban"]), ())) | set(
            by_name.get(norm(r0["beneficiary"]), ())
        )
        out = []
        for r in docs:
            iid = resolve(r, ents, parties)
            if iid:
                out.append((iid, abs(_f(r["document_amount"]))))
        resolved[k] = out
        return out

    def transfer_amt(k):
        return _f(by_e2e[k][0]["transfer_amount"])

    def finish(t, row, keys):
        cur = row["currency"]
        allocs = []
        for k in keys:
            for iid, a in allocs_of(k):
                it = info[iid]
                op = st.open.get(iid, 0)
                if (row["amount"] or 0) > 0:
                    exc = "RETURNED"
                elif it["item_type"] == "API" and it["currency"] != cur:
                    exc = "FX_DIFFERENCE"
                elif it["item_type"] == "API" and op > a + max(0.01, 0.005 * a):
                    exc = "PARTIAL"
                else:
                    exc = ""
                allocs.append((iid, a, exc))
        if allocs:
            st.assign(t, allocs)

    def maturity(k):
        ds = [info[i]["due_date"] for i, _ in allocs_of(k) if info[i]["due_date"]]
        return max(ds) if ds else None

    tok_re = re.compile(r"[A-Z0-9][A-Z0-9\-]{6,}")
    for row in sorted(
        bank_rows(ph), key=lambda x: (x["booking_date"], x["bank_txn_id"])
    ):
        t = row["bank_txn_id"]
        if st.claimed(t) or not row["amount"]:
            continue
        amt = abs(row["amount"])
        txt = bank_text(row).upper()
        toks = [norm(x) for x in tok_re.findall(txt)]
        keys = None
        for n in toks:  # full end_to_end_id
            if n in by_e2e and by_e2e[n][0]["run_type"] == "transfers":
                keys = [n]
                break
        if keys is None:
            for n in toks:
                run = next((n[:L] for L in run_len if n[:L] in by_run), None)
                if not run:
                    continue
                e2es = by_run[run]
                rtype = next(iter(e2es.values()))[0]["run_type"]
                if rtype == "transfers":
                    # truncated end_to_end_id: prefix + transfer amount, then beneficiary name
                    c = [
                        k
                        for k in e2es
                        if k.startswith(n) and abs(transfer_amt(k) - amt) < 0.02
                    ]
                    if len(c) != 1:
                        c = [k for k in e2es if abs(transfer_amt(k) - amt) < 0.02]
                    if not c:  # FX: the bank shows the converted amount
                        c = [
                            k for k in e2es if e2es[k][0]["currency"] != row["currency"]
                        ]
                    if len(c) > 1:
                        tw = _words(txt)
                        c = [k for k in c if _words(e2es[k][0]["beneficiary"]) <= tw]
                    if len(c) == 1:
                        keys = c
                elif rtype == "aggregated":
                    if abs(sum(transfer_amt(k) for k in e2es) - amt) < 0.02:
                        keys = list(e2es)
                    else:  # FX: transfers in another currency, converted by the bank (spread <= 3 %, POLICIES §3)
                        keys = _agg_fx(ph, e2es, transfer_amt, row, amt)
                else:  # confirming: one debit per maturity date (max due date of the transfer, next business day)
                    groups = defaultdict(list)
                    for k in e2es:
                        m = maturity(k)
                        if m:
                            groups[m].append(k)
                    bd = row["booking_date"]
                    g = [
                        k for m, ks in groups.items() if _next_bday(m) == bd for k in ks
                    ]
                    if g and abs(sum(transfer_amt(k) for k in g) - amt) < 0.02:
                        keys = g
                    else:  # holidays: widest run of maturity dates ending on or before the booking date
                        ds = sorted(m for m in groups if m <= bd)
                        tot, acc = 0.0, []
                        for m in reversed(ds):
                            tot += sum(transfer_amt(k) for k in groups[m])
                            acc += groups[m]
                            if abs(tot - amt) < 0.02:
                                keys = list(acc)
                                break
                            if tot > amt + 0.02:
                                break
                if keys:
                    break
        if keys:
            finish(t, row, keys)


def _agg_fx(ph, e2es, transfer_amt, row, amt):
    """Aggregated run mixing currencies: same-currency transfers at face value + foreign ones converted at the
    reference rate of the booking date; accept when the gap is within 3 % of the converted foreign part."""
    try:
        from .payments import _fx_rates, _rate
    except Exception:
        return None
    cur = row.get("currency")
    day = str(row.get("booking_date") or "")[:10]
    fx = _fx_rates(ph)
    same, conv = 0.0, 0.0
    for k, docs in e2es.items():
        c = docs[0]["currency"]
        a = transfer_amt(k)
        if c == cur:
            same += a
        else:
            rf, rt = _rate(fx, c, day), _rate(fx, cur, day)
            if not rf or not rt:
                return None
            conv += a / rf * rt
    if conv <= 0:
        return None
    if abs(same + conv - amt) <= 0.03 * conv + 0.02:
        return list(e2es)
    return None
