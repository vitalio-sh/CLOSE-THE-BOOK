"""Structured parsing of customer remittance advices (POLICIES §3 SHORT_DISPUTE / §5.4).

Every layout (csv, xlsx, e-mail txt, pdf) is turned into
    {'ref': str, 'date': date|None, 'total': float|None, 'rows': [(doc, gross, deduction, reason, paid)]}
The bank line is linked to the advice through the payment reference (normalised alphanumerics) or, failing that, a
unique advice of the same entity whose total equals the cash. Each document is mapped to an item by its printed
number (exact, then the noisy RefIndex).
"""
from __future__ import annotations

import datetime as dt
import re

_NUM = re.compile(r"^-?[\d.,]*\d$")
_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
_ALNUM = re.compile(r"[^A-Z0-9]")


def nrm(s) -> str:
    return _ALNUM.sub("", str(s or "").upper())


def num(s):
    s = str(s or "").strip().replace(" ", "")
    if not s or not _NUM.match(s):
        return None
    if re.search(r",\d{1,2}$", s):  # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def pdate(s):
    s = str(s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _row(doc, gross, ded, reason, paid):
    g, d, p = num(gross), num(ded), num(paid)
    if p is None and g is not None:
        p = round(g + (d or 0.0), 2)
    if g is None:
        g = p
    if p is None or not doc:
        return None
    return (str(doc).strip(), g, d or 0.0, (reason or "").strip().upper(), p)


def parse_csv(text):
    raw = [ln for ln in text.splitlines() if ln.strip()]
    sep = ";" if raw and raw[0].count(";") > raw[0].count(",") else ("\t" if raw and "\t" in raw[0] else ",")
    lines = [ln.split(sep) for ln in raw]
    out = {"ref": "", "date": None, "total": None, "rows": []}
    if not lines:
        return out
    h = lines[0]
    kv = {h[i].strip().lower(): h[i + 1].strip() for i in range(0, len(h) - 1, 2)}
    out["ref"] = kv.get("payment_ref", "")
    out["date"] = pdate(kv.get("payment_date"))
    cols = None
    for ln in lines[1:]:
        low = [c.strip().lower() for c in ln]
        if "document" in low and "paid" in low:
            cols = {c: i for i, c in enumerate(low)}
            continue
        if cols is None:
            continue
        get = lambda k: ln[cols[k]] if k in cols and cols[k] < len(ln) else ""  # noqa: E731
        doc = get("document").strip()
        if doc.upper() == "TOTAL":
            out["total"] = num(get("paid"))
            continue
        r = _row(doc, get("gross"), get("discount_or_deduction") or get("deduction"), get("reason"), get("paid"))
        if r:
            out["rows"].append(r)
    return out


def parse_xlsx_rows(rows):
    out = {"ref": "", "date": None, "total": None, "rows": []}
    cols = None
    for ln in rows:
        ln = ["" if v is None else str(v).strip() for v in ln]
        low = [c.lower() for c in ln]
        if cols is None:
            if "document" in low and "paid" in low:
                cols = {c: i for i, c in enumerate(low)}
                continue
            for i, c in enumerate(low):
                if ("advice" in c or "ref" in c or "aviso" in c) and i + 1 < len(ln) and ln[i + 1] and not out["ref"]:
                    out["ref"] = ln[i + 1]
                    if i + 2 < len(ln):
                        out["date"] = pdate(ln[i + 2])
            continue
        get = lambda k: ln[cols[k]] if k in cols and cols[k] < len(ln) else ""  # noqa: E731
        doc = get("document")
        if not doc:
            continue
        if doc.upper() == "TOTAL":
            out["total"] = num(get("paid"))
            continue
        r = _row(doc, get("gross"), get("deduction") or get("discount_or_deduction"), get("reason"), get("paid"))
        if r:
            out["rows"].append(r)
    return out


_TXT_ROW = re.compile(r"^\s+(\S+)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(-?[\d.,]+)(?:\s+(-?[\d.,]+)\s+(.*?))?\s*$")
_TXT_AMT = re.compile(r"(?:payment of|importe de|montant de|pagamento de|Betrag von|bedrag van)\s+(-?[\d.,]+)", re.I)
_TXT_DATE = re.compile(r"(?:\bon|fecha|du|em|am|op)\s+(\d{1,2}/\d{1,2}/\d{4})")


def parse_txt(text):
    out = {"ref": "", "date": None, "total": None, "rows": []}
    for ln in text.splitlines():
        if ln.lower().startswith("subject:"):
            toks = ln.split()
            out["ref"] = toks[-1] if toks else ""
            continue
        m = _TXT_ROW.match(ln)
        if m:
            doc, _d, gross, ded, reason = m.groups()
            r = _row(doc, gross, ded, reason, None)
            if r:
                out["rows"].append(r)
            continue
        if out["total"] is None:
            m = _TXT_AMT.search(ln)
            if m:
                out["total"] = num(m.group(1))
                m2 = _TXT_DATE.search(ln)
                if m2:
                    out["date"] = pdate(m2.group(1))
    return out


_DED = re.compile(r"^(-?[\d.,]+)\s+(\S.*)$")


def parse_pdf_text(text):
    out = {"ref": "", "date": None, "total": None, "rows": []}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    i = 0
    hdr_end = None
    while i < len(lines):
        ln = lines[i].lower()
        if (ln.startswith("payment ref") or ln in ("ref.", "ref", "réf.", "référence", "referência", "referenz",
                                                    "zahlungsreferenz", "referentie", "riferimento")) \
                and i + 1 < len(lines) and not out["ref"]:
            out["ref"] = lines[i + 1]
        elif ln in ("date", "fecha", "data", "datum") and out["date"] is None and i + 1 < len(lines) and pdate(lines[i + 1]):
            out["date"] = pdate(lines[i + 1])
        elif ln in ("amount", "importe", "montant", "valor", "betrag", "bedrag", "importo") and i + 1 < len(lines):
            out["total"] = num(lines[i + 1].split()[0])
        elif ln in ("paid", "pagado", "payé", "pago", "bezahlt", "betaald", "pagato"):
            hdr_end = i + 1
            break
        i += 1
    if hdr_end is None:
        return out
    i = hdr_end
    while i < len(lines):
        doc = lines[i]
        if (len(doc) > 25 or " " in doc) or i + 2 >= len(lines) or not _DATE.match(lines[i + 1]):
            i += 1
            continue
        gross = num(lines[i + 2])
        if gross is None:
            i += 1
            continue
        j = i + 3
        ded, reason = None, ""
        if j < len(lines):
            m = _DED.match(lines[j])
            if m and num(m.group(1)) is not None:
                ded, reason = m.group(1), m.group(2)
                j += 1
        paid = lines[j] if j < len(lines) and num(lines[j]) is not None else None
        if paid is None:
            i += 1
            continue
        r = _row(doc, lines[i + 2], ded, reason, paid)
        if r:
            out["rows"].append(r)
        i = j + 1
    return out


def parse_file(path: str) -> dict:
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        rows = []
        for ws in wb.worksheets:
            rows.extend(ws.iter_rows(values_only=True))
        wb.close()
        rows = [[(f"{v:.2f}" if isinstance(v, float) else (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v))
                 for v in r] for r in rows]
        return parse_xlsx_rows(rows)
    if ext == "pdf":
        import pypdf
        r = pypdf.PdfReader(path)
        return parse_pdf_text("\n".join((p.extract_text() or "") for p in r.pages))
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if ext == "csv":
        return parse_csv(text)
    return parse_txt(text)


# ----------------------------------------------------------------------------------------------------------------------
# Several document numbers in the bank text (POLICIES §5.5: no prefix, last digits only, truncated at 16/35/38 chars,
# continued in the next record) whose open amounts, minus the pending credit notes, add up to the cash exactly.
# ----------------------------------------------------------------------------------------------------------------------
_DIG = re.compile(r"\d{3,}")


def text_tokens(raw: str) -> list:
    """Digit runs of the bank text, plus the join of a run cut by a record boundary ('0329 | 708')."""
    out = _DIG.findall(raw or "")
    for m in re.finditer(r"(\d{2,})\s*\|\s*(\d{2,})", raw or ""):
        out.append(m.group(1) + m.group(2))
    return out


def solve_text(tokens, pool, amount, max_combos=4096):
    """pool: {item_id: (digits_of_doc, open_amount, is_credit_note, related_digits)}.

    Returns a list of item_ids (unique exact solution) or None.
    Every token either names one item exactly (full number or a distinctive suffix) or is ambiguous (prefix of a
    truncated number / short suffix): ambiguous tokens choose at most one of their candidates. Pending credit notes
    related to a chosen invoice may be netted. Exactly one combination must reach the cash (±0.01).
    """
    if not tokens or not pool:
        return None
    toks = list(dict.fromkeys(t for t in tokens if len(t) >= 3))
    sure, amb = set(), []
    for t in toks:
        exact = [i for i, v in pool.items() if v[0] == t]
        if len(exact) == 1:
            sure.add(exact[0])
            continue
        suf = [i for i, v in pool.items() if len(t) >= 5 and v[0].endswith(t)]
        if len(suf) == 1:
            sure.add(suf[0])
            continue
        pre = [i for i, v in pool.items() if len(t) >= 4 and v[0].startswith(t) and len(v[0]) > len(t)]
        c = set(suf) | set(pre)
        if c:
            amb.append(sorted(c))
    if not sure and not amb:
        return None
    amb = [[i for i in a if i not in sure] for a in amb]
    amb = [a for a in amb if a]
    # merge identical ambiguity groups (the same prefix quoted twice = two different documents)
    n_combo = 1
    for a in amb:
        n_combo *= (len(a) + 1)
        if n_combo > max_combos:
            return None
    cents = round(amount * 100)
    val = {i: (-1 if v[2] else 1) * round(v[1] * 100) for i, v in pool.items()}
    sols = []
    for pick in _product(amb):
        chosen = set(sure)
        ok = True
        for p in pick:
            if p is None:
                continue
            if p in chosen:
                ok = False
                break
            chosen.add(p)
        if not ok:
            continue
        s = sum(val[i] for i in chosen)
        # pending credit notes related to chosen invoices (optional netting, all-or-nothing per subset)
        rel = [i for i, v in pool.items() if v[2] and i not in chosen and v[3] and
               any(pool[c][0] == v[3] for c in chosen if not pool[c][2])]
        found = None
        for k in range(len(rel) + 1):
            if k > 3:
                break
            for sub in _combos(rel, k):
                if abs(s + sum(val[i] for i in sub) - cents) <= 1:
                    found = chosen | set(sub)
                    break
            if found:
                break
        if found:
            sols.append(frozenset(found))
            if len(set(sols)) > 1:
                return None
    sols = set(sols)
    if len(sols) != 1:
        return None
    return sorted(next(iter(sols)))


def _product(groups):
    import itertools
    return itertools.product(*[[None] + g for g in groups])


def _combos(lst, k):
    import itertools
    return itertools.combinations(lst, k)


_ALTOK = re.compile(r"[A-Za-z]*[-/]?\d[\d/\-]*")


def text_tokens_po(raw: str) -> list:
    """Alphanumeric reference tokens normalised (OC872088, PED-44397 -> PED44397, 2024/3336 -> 20243336)."""
    out = []
    for m in _ALTOK.finditer((raw or "").upper()):
        n = nrm(m.group(0))
        if len(n) >= 4 and any(c.isdigit() for c in n):
            out.append(n)
    return out


def solve_refs(tokens, pool, amount, filler=None, max_fill=4, max_combos=4096, min_sure=1):
    """pool: {item_id: (keys:set of normalised refs, open_amount, is_cn, related_key)}.
    Tokens naming exactly one item (exact key) are sure; tokens that are a prefix of keys (truncated) are ambiguous
    (choose <= 1). `filler`: item_ids (no ref of their own) that may complete the sum, up to max_fill of them.
    Unique exact combination or None."""
    import itertools
    sure, amb = set(), []
    for t in dict.fromkeys(tokens):
        ex = [i for i, v in pool.items() if t in v[0]]
        if len(ex) == 1:
            sure.add(ex[0])
            continue
        if ex:
            return None
        pre = [i for i, v in pool.items() if len(t) >= 4 and any(k.startswith(t) and len(k) > len(t) for k in v[0])]
        if pre:
            amb.append(pre)
    if len(sure) < max(1, min_sure):
        return None
    amb = [[i for i in a if i not in sure] for a in amb]
    amb = [a for a in amb if a]
    n = 1
    for a in amb:
        n *= len(a) + 1
        if n > max_combos:
            return None
    cents = round(amount * 100)
    val = {i: (-1 if v[2] else 1) * round(v[1] * 100) for i, v in pool.items()}
    fill = [i for i in (filler or []) if i not in sure]
    sols = set()
    for pick in itertools.product(*[[None] + a for a in amb]):
        chosen = set(sure)
        if any(p is not None and p in chosen for p in pick) or len({p for p in pick if p}) != len([p for p in pick if p]):
            continue
        chosen |= {p for p in pick if p}
        rest = cents - sum(val[i] for i in chosen)
        cand = [i for i in fill if i not in chosen]
        for k in range(0, max_fill + 1):
            if k and len(cand) > 40:
                break
            for sub in itertools.combinations(cand, k):
                if abs(sum(val[i] for i in sub) - rest) <= 1:
                    sols.add(frozenset(chosen | set(sub)))
                    if len(sols) > 1:
                        return None
            if sols:
                break
    return sorted(next(iter(sols))) if len(sols) == 1 else None
