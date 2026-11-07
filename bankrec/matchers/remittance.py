"""Customer remittance advices (POLICIES §3 SHORT_DISPUTE / PARTIAL, §5.4).

A bank line is matched to a remittance advice (RA) when a long digit run of the bank text (the customer's payment
reference, e.g. `RMR*IV*9926040209*`) is the payment reference printed in the RA header, AND the RA lines add up to the
bank amount (credit notes are negative lines). Each RA line is resolved to an open item of the same entity through the
document-number index (noisy references tolerated by RefIndex). Layouts: csv, txt (e-mail text), xlsx, pdf (one value
per line). The line is claimed only when every resolved item is still open and the advice total equals the cash.

Amounts (POLICIES §2): fully paid item -> open amount; short payment -> amount applied (SHORT_DISPUTE when the advice
shows a deduction, PARTIAL otherwise); credit notes netted -> full amount, no exception.
"""
from __future__ import annotations

import datetime as dt
import os
import pickle
import re
from datetime import timedelta

from ..ctx import bank_rows, bank_text, item_info, ref_index
from ..data import CACHE
from ..refs import norm

TOL_ABS = 0.01
TOL_REL = 0.005

_DEC = re.compile(r"(?<![\w/])-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{1,2}(?![\w/])")
_DOC = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,4}\d{2}-\d{2}-\d{4,6}|[A-Za-z]{0,4}\d{6,12})(?![A-Za-z0-9])")
_STOP = re.compile(r"(?i)\b(?:total|sum|subtotal|kind regards|saludos|atentamente)\b")
_REF = re.compile(r"\d{9,14}")
_TOKEN_DIGITS = re.compile(r"\d{9,14}")


def _f(s):
    try:
        return float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _read_text(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        import pypdf
        r = pypdf.PdfReader(path)
        return "\n".join((p.extract_text() or "") for p in r.pages)
    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = []
                for v in row:
                    if v is None:
                        cells.append("")
                    elif isinstance(v, bool):
                        cells.append(str(v))
                    elif isinstance(v, int) and abs(v) <= 999999:
                        cells.append(f"{v}.00")
                    elif isinstance(v, (int, float)):
                        cells.append(f"{v:.2f}" if isinstance(v, float) else str(v))
                    else:
                        cells.append(str(v))
                if any(cells):
                    out.append(" ; ".join(cells))
        wb.close()
        return "\n".join(out)
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _parse(text: str, resolve) -> dict:
    """Return {'header_refs': set, 'records': [(item_or_None, token, value, gross, n_dec)], 'sum': float}."""
    head = text[:600]
    refs = set(_REF.findall(head))
    events = []
    for m in _DOC.finditer(text):
        if m.group(0) in refs:
            continue  # the payment reference itself, not a document
        events.append((m.start(), "doc", m.group(0)))
    for m in _DEC.finditer(text):
        events.append((m.start(), "dec", m.group(0)))
    for m in _STOP.finditer(text):
        events.append((m.start(), "stop", m.group(0)))
    events.sort(key=lambda e: e[0])
    records = []
    cur = None
    for pos, kind, s in events:
        if kind == "stop":
            cur = None
        elif kind == "doc":
            # a document token that is not an invoice number opens a no-item record
            iid = resolve(s)
            cur = {"item": iid, "tok": s, "vals": []}
            records.append(cur)
        elif kind == "dec" and cur is not None:
            v = _f(s)
            if v is not None:
                cur["vals"].append(v)
    out = []
    for r in records:
        if not r["vals"]:
            continue
        paid = r["vals"][-1]
        gross = r["vals"][0] if len(r["vals"]) > 1 else None
        out.append((r["item"], r["tok"], paid, gross, len(r["vals"])))
    return {"refs": refs, "records": out, "sum": round(sum(r[2] for r in out), 2)}


def _ra_index(ph) -> dict:
    """ref digits -> list of parsed advice dicts (cached on disk per file path + mtime)."""
    if "_ra_refs" in ph.extra:
        return ph.extra["_ra_refs"]
    ra = ph.index("remittance_advices")
    ra_rows = []
    if not ra.is_empty():
        lo = dt.date.fromisoformat(ph.start) - timedelta(days=30) if ph.start else None
        hi = dt.date.fromisoformat(ph.end) + timedelta(days=150) if ph.end else None
        for r in ra.iter_rows(named=True):
            if not os.path.exists(r["path"]):
                continue
            try:
                d = dt.date.fromisoformat(r["received_date"])
            except (TypeError, ValueError):
                d = None
            if d and lo and hi and not (lo <= d <= hi):
                continue
            ra_rows.append(r)
    os.makedirs(CACHE, exist_ok=True)
    cache_path = os.path.join(CACHE, f"ra_text_{ph.name}_{len(ra_rows)}.pkl")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                cache = pickle.load(fh)
        except Exception:
            cache = {}
    dirty = False
    texts = {}
    for r in ra_rows:
        p = r["path"]
        key = (p, os.path.getmtime(p))
        if key in cache:
            texts[p] = cache[key]
            continue
        try:
            t = _read_text(p)
        except Exception:
            t = ""
        cache[key] = t
        texts[p] = t
        dirty = True
    if dirty:
        try:
            with open(cache_path, "wb") as fh:
                pickle.dump(cache, fh)
        except Exception:
            pass
    ph.extra["_ra_refs"] = (ra_rows, texts)
    return ph.extra["_ra_refs"]


def _entity_resolver(ph):
    idx = ref_index(ph)
    info = item_info(ph)
    cache = {}

    def make(entity):
        def resolve(tok):
            key = (entity, tok)
            if key in cache:
                return cache[key]
            n = norm(tok)
            hits = [i for i in idx.get(n, entity) if info.get(i, {}).get("item_type") in ("ARI", "ARC")]
            res = hits[0] if len(hits) == 1 else None
            cache[key] = res
            return res
        return resolve
    return make


def match(ph, st) -> None:
    ra_rows, texts = _ra_index(ph)
    if not ra_rows:
        return
    try:
        _match_structured(ph, st, ra_rows)
    except Exception as e:  # never let the structured pass kill the legacy one
        print(f"  remittance structured pass failed: {e!r}")
    try:
        _match_text_refs(ph, st)
    except Exception as e:
        print(f"  remittance text-refs pass failed: {e!r}")
    make = _entity_resolver(ph)
    # reference digits -> advices (header refs), entity from the index row
    by_ref = {}
    by_sum = {}
    parsed = {}
    for r in ra_rows:
        t = texts.get(r["path"], "")
        if not t:
            continue
        ent = r.get("entity") or ""
        p = _parse(t, make(ent))
        if not p["records"]:
            continue
        parsed[r["path"]] = (r, p)
        for ref in p["refs"]:
            by_ref.setdefault(ref, []).append(r["path"])
        by_sum.setdefault(round(p["sum"], 2), []).append(r["path"])

    rows = bank_rows(ph)
    info = item_info(ph)
    for row in rows:
        t = row["bank_txn_id"]
        amt = row["amount"] or 0
        if st.claimed(t) or amt <= 0:
            continue
        refs = set(_TOKEN_DIGITS.findall(bank_text(row)))
        cands = []
        for ref in refs:
            for path in by_ref.get(ref, []):
                if path not in cands:
                    cands.append(path)
        best = None
        for path in cands:
            r, p = parsed[path]
            if abs(p["sum"] - amt) > max(TOL_ABS, TOL_REL * abs(amt)):
                continue
            if r.get("entity") and row.get("entity") and r["entity"] != row["entity"]:
                continue
            best = p
            break
        if best is None:
            # no usable reference: an advice of the same entity, received after the cash, whose lines add up to the
            # cash, provided that it is the only one (POLICIES §5.4)
            hits = []
            for path in by_sum.get(round(amt, 2), []):
                r, p = parsed[path]
                if r.get("entity") != row.get("entity"):
                    continue
                rd = _date(r.get("received_date"))
                bd = _date(row.get("booking_date"))
                if rd is None or bd is None or not (bd <= rd <= bd + timedelta(days=45)):
                    continue
                hits.append(p)
            if len(hits) == 1:
                best = hits[0]
        if best is None:
            continue
        allocs = _allocs(st, info, best)
        if allocs:
            st.assign(t, allocs)


def _date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _allocs(st, info, p):
    """Turn parsed advice records into allocations; None when the advice cannot be applied safely."""
    allocs = []
    seen = set()
    for item, tok, paid, gross, n_dec in p["records"]:
        if item is None:
            return None  # unresolved document in the advice: do not claim the line
        if item in seen:
            return None
        seen.add(item)
        r = info.get(item)
        if r is None:
            return None
        openamt = st.open.get(item, 0.0)
        if openamt <= 0.005:
            return None
        if r["item_type"] == "ARC" or paid < 0:
            allocs.append((item, abs(paid), ""))
            continue
        tol = max(TOL_ABS, TOL_REL * openamt)
        if abs(paid - openamt) <= tol:
            allocs.append((item, openamt, ""))
        elif paid < openamt:
            exc = "SHORT_DISPUTE" if (gross is not None and gross > paid + TOL_ABS) or n_dec >= 3 else "PARTIAL"
            allocs.append((item, paid, exc))
        else:
            return None
    return allocs


# ----------------------------------------------------------------------------------------------------------------------
# Structured pass (advice_big): big key-account advices with dozens/hundreds of documents, per-row deductions/reasons.
# ----------------------------------------------------------------------------------------------------------------------
_TOKS = re.compile(r"[A-Za-z0-9/\-]{6,}")


def _parsed_advices(ph, ra_rows):
    from . import advice_big as A
    key = "_ra_struct"
    if key in ph.extra:
        return ph.extra[key]
    os.makedirs(CACHE, exist_ok=True)
    cache_path = os.path.join(CACHE, "ra_struct_v2.pkl")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                cache = pickle.load(fh)
        except Exception:
            cache = {}
    dirty = False
    out = []
    for r in ra_rows:
        p = r["path"]
        k = (p, os.path.getmtime(p))
        if k not in cache:
            try:
                cache[k] = A.parse_file(p)
            except Exception:
                cache[k] = {"ref": "", "date": None, "total": None, "rows": []}
            dirty = True
        a = cache[k]
        if a["rows"]:
            out.append((r, a))
    if dirty:
        try:
            with open(cache_path, "wb") as fh:
                pickle.dump(cache, fh)
        except Exception:
            pass
    ph.extra[key] = out
    return out


def _match_structured(ph, st, ra_rows):
    from . import advice_big as A
    advs = _parsed_advices(ph, ra_rows)
    if not advs:
        return
    info = item_info(ph)
    # exact printed number per entity (AR side), then the customer's order number, then the noisy index
    docmap, pomap = {}, {}
    for iid, r in info.items():
        if r.get("item_type") not in ("ARI", "ARC"):
            continue
        docmap.setdefault((r["entity"], A.nrm(r.get("document_number"))), []).append(iid)
        if r.get("po"):
            pomap.setdefault((r["entity"], A.nrm(r["po"])), []).append(iid)
    make = _entity_resolver(ph)
    terms = {}
    try:
        for c, pct, dd in ph.masters["payment_terms"].select("code", "early_payment_discount_pct",
                                                              "discount_days").iter_rows():
            terms[c] = (float(pct) if pct not in (None, "") else 0.0, int(float(dd)) if dd not in (None, "") else 0)
    except Exception:
        pass

    by_ent = {}
    for r, a in advs:
        a_sum = round(sum(x[4] for x in a["rows"]), 2)
        d = a.get("date") or _date(r.get("received_date"))
        by_ent.setdefault(r.get("entity") or "", []).append([r, a, a_sum, A.nrm(a.get("ref")), d, False])
    by_sum = {}
    for ent, lst in by_ent.items():
        for e in lst:
            by_sum.setdefault((ent, e[2]), []).append(e)

    def near(e, bd):
        return e[4] is not None and -12 <= (e[4] - bd).days <= 30

    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = row["amount"] or 0
        if amt <= 0 or st.claimed(t):
            continue
        fxl = _fx_target(row)
        if fxl:
            amt = fxl[1]  # the advice is in the invoice currency: compare with the instructed amount
        ent = row.get("entity") or ""
        lst = by_ent.get(ent)
        if not lst:
            continue
        bd = _date(row.get("booking_date"))
        if bd is None:
            continue
        raw = " ".join(str(row.get(c) or "") for c in ("text", "ref1", "ref2", "end_to_end_id", "remittance_info"))
        text = A.nrm(raw)
        sum_ok = lambda e: abs(e[2] - amt) <= 0.011  # noqa: E731
        cands = [e for e in lst if not e[5] and len(e[3]) >= 6 and e[3] in text and sum_ok(e)]
        if len(cands) > 1:
            cands = [e for e in cands if near(e, bd)]
        if not cands:
            toks = {A.nrm(x) for x in _TOKS.findall(raw)}
            toks = {k for k in toks if len(k) >= 6}
            cands = [e for e in lst if not e[5] and len(e[3]) >= 6 and sum_ok(e) and near(e, bd)
                     and any(e[3].startswith(k) for k in toks)]
        if not cands:
            cands = [e for e in by_sum.get((ent, round(amt, 2)), []) if not e[5] and near(e, bd)
                     and len(e[1]["rows"]) >= 2]
        if len(cands) != 1:
            continue
        e = cands[0]
        allocs = _struct_allocs(st, info, e[1], ent, docmap, pomap, make(ent), terms, bd)
        if allocs:
            allocs = _fx_allocs(info, allocs, row, fxl)
        if allocs and st.assign(t, allocs):
            e[5] = True


def _struct_allocs(st, info, a, ent, docmap, pomap, resolve, terms, bd):
    from . import advice_big as A
    allocs, seen = [], set()
    for doc, gross, ded, reason, paid in a["rows"]:
        ids = docmap.get((ent, A.nrm(doc)), [])
        if len(ids) != 1:
            ids = pomap.get((ent, A.nrm(doc)), []) if not ids else ids
        if len(ids) != 1:
            iid = resolve(doc)
            ids = [iid] if iid else []
        if len(ids) != 1:
            return None
        iid = ids[0]
        if iid in seen:
            return None
        seen.add(iid)
        r = info.get(iid)
        openamt = st.open.get(iid, 0.0)
        if r is None or openamt <= 0.005:
            return None
        tol = max(TOL_ABS, TOL_REL * openamt)
        if r["item_type"] == "ARC" or paid < 0:
            if abs(abs(paid) - openamt) > tol:
                return None
            allocs.append((iid, openamt, ""))
            continue
        if abs(ded) < 0.005:
            if abs(paid - openamt) <= tol:
                allocs.append((iid, openamt, ""))
            elif paid < openamt:
                allocs.append((iid, paid, "PARTIAL"))
            else:
                return None
            continue
        if paid > openamt + tol:
            return None
        if reason == "DISCOUNT" or reason.startswith("DTO") or reason.startswith("PRONTO"):
            ok = False
            pt = terms.get(r.get("payment_terms") or "")
            tot = r.get("total_amount") or openamt
            idate = _date(r.get("issue_date"))
            if pt and pt[0] and idate is not None:
                ok = bd <= idate + timedelta(days=pt[1] + 3) and abs(round(tot - tot * pt[0] / 100, 2) - paid) <= 0.011
            allocs.append((iid, openamt, "SHORT_DISCOUNT") if ok else (iid, paid, "PARTIAL"))
        else:
            allocs.append((iid, paid, "SHORT_DISPUTE"))
    return allocs


def _match_text_refs(ph, st):
    """Several (truncated / suffix-only) document numbers in the bank text whose open amounts, net of related pending
    credit notes, add up to the cash exactly and uniquely (POLICIES §5.5). Customer anchored on a full number."""
    from . import advice_big as A
    info = item_info(ph)
    dig = lambda s: re.sub(r"\D", "", str(s or ""))  # noqa: E731
    bydig, bycust = {}, {}
    for iid, r in info.items():
        if r.get("item_type") not in ("ARI", "ARC"):
            continue
        d = dig(r.get("document_number"))
        if len(d) >= 5:
            bydig.setdefault((r["entity"], d), []).append(iid)
        bycust.setdefault((r["entity"], r.get("party_id")), []).append(iid)
    for row in bank_rows(ph):
        t = row["bank_txn_id"]
        amt = row["amount"] or 0
        if amt <= 0 or st.claimed(t):
            continue
        ent = row.get("entity") or ""
        raw = " | ".join(str(row.get(c) or "") for c in ("text", "remittance_info"))
        toks = A.text_tokens(raw)
        if len(toks) < 2:
            continue
        custs = set()
        for k in toks:
            for i in bydig.get((ent, k), []):
                custs.add(info[i].get("party_id"))
        if len(custs) != 1:
            continue
        cust = next(iter(custs))
        bd = str(row.get("booking_date") or "")[:10]
        cur = row.get("currency")
        fxl = _fx_target(row)
        if fxl:
            cur, amt = fxl
        pool = {}
        for i in bycust.get((ent, cust), []):
            r = info[i]
            op = st.open.get(i, 0.0)
            if op <= 0.005 or str(r.get("issue_date") or "")[:10] > bd or (cur and r.get("currency") != cur):
                continue
            pool[i] = (dig(r.get("document_number")), op, r["item_type"] == "ARC", dig(r.get("related_document")))
        sol = A.solve_text(toks, pool, amt)
        if not sol or len(sol) < 2:
            continue
        allocs = _fx_allocs(info, [(i, pool[i][1], "") for i in sol], row, fxl)
        if allocs:
            st.assign(t, allocs)


def _fx_target(row):
    """(invoice currency, instructed amount) when the bank line shows a foreign original amount, else None."""
    oc, oa = row.get("orig_currency"), row.get("orig_amount")
    if not oc or oc == row.get("currency"):
        return None
    try:
        oa = float(str(oa).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return (oc, oa) if oa > 0 else None


def _fx_allocs(info, allocs, row, fxl):
    """POLICIES §3 FX_DIFFERENCE: every fully cleared invoice row of a line in another currency carries it
    (replacing '', SHORT_DISCOUNT...); PARTIAL / SHORT_DISPUTE keep their code; credit notes stay empty."""
    cur = row.get("currency")
    out = []
    for iid, a, exc in allocs:
        r = info.get(iid) or {}
        if fxl and r.get("currency") != fxl[0]:
            return None
        if not fxl and r.get("currency") and cur and r.get("currency") != cur:
            return None
        if r.get("currency") != cur and r.get("item_type") == "ARI" and exc not in ("PARTIAL", "SHORT_DISPUTE"):
            exc = "FX_DIFFERENCE"
        out.append((iid, a, exc))
    return out
