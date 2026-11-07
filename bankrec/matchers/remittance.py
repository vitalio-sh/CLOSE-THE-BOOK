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
