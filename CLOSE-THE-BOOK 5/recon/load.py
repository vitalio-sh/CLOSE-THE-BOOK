"""Carga de datos (responsable: P1).

Los nombres de columna se detectan con listas de candidatos (common.pick). Al ejecutar
se imprime el mapeo elegido: revisadlo contra FORMAT.md la primera vez.
"""
import re
from pathlib import Path

import pandas as pd

from .common import find, norm, norm_doc, pick, read_csv, to_num

OPEN_FILE = "open_items_at_start.csv"
# fichero -> (lado, abono) por defecto; None = deducir de columnas o del prefijo del id
DOC_FILES = {"ar_invoices.csv": ("AR", False), "ap_invoices.csv": ("AP", False),
             "credit_notes.csv": (None, True)}
ITEM_FILES = [OPEN_FILE, *DOC_FILES]

def _log(name, mapping):
    print(f"[load] {name}: " + ", ".join(f"{k}<-{v}" for k, v in mapping.items()))


# ---------------------------------------------------------------- movimientos
def load_txns(data_dir: Path) -> pd.DataFrame:
    df = read_csv(find(data_dir, "bank_transactions.csv"))
    m = {
        "id": pick(df, "bank_txn_id"),
        "date": pick(df, "booking_date", "operation_date", "date", "txn_date", "value_date"),
        "amount": pick(df, "amount", "signed_amount"),
        "ccy": pick(df, "currency", "ccy", required=False),
        "account": pick(df, "bank_account_id", "account_id", "account", required=False),
        "entity": pick(df, "entity_id", "entity", required=False),
        "bank_code": pick(df, "bank_code", required=False),
        "orig_amount": pick(df, "orig_amount", required=False),
        "orig_ccy": pick(df, "orig_currency", required=False),
    }
    _log("bank_transactions", m)
    out = pd.DataFrame({k: (df[v] if v else "") for k, v in m.items()})
    out["amount"] = to_num(out["amount"])
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    # Todo el texto libre concatenado (concepto, referencias, ordenante, códigos...)
    text_cols = [c for c in df.columns
                 if re.search(r"concept|desc|text|narr|remit|ref|info|memo|counterpart|name|raw|code", c, re.I)
                 and c not in (m["id"], "statement_file", "statement_format")]
    print(f"[load] columnas de texto: {text_cols}")
    out["text"] = df[text_cols].astype(str).agg(" | ".join, axis=1) if text_cols else ""
    out["ntext"] = out["text"].map(norm)
    return out.sort_values(["date", "id"]).reset_index(drop=True)


# ---------------------------------------------------------------- partidas
AMOUNT_COLS = ("open_amount", "amount_open", "outstanding_amount", "total_amount",
               "gross_amount", "amount_total", "amount")


def _side_credit(df, m, src, ids):
    """Lado (AR/AP) y si es abono. Prioridad: columna explícita > fichero > columna de tercero > prefijo."""
    pref = ids.str.upper().str[:3]
    side = pref.str[:2].where(pref.str[:2].isin(["AR", "AP"]), "")
    credit = pref.str[2] == "C"
    f_side, f_credit = DOC_FILES.get(src, (None, None))
    if f_side:
        side = pd.Series(f_side, index=df.index)
    elif m["cust"] and m["supp"]:          # abonos con ambas columnas: la que esté rellena
        side = pd.Series(["AR" if c else "AP" if s else x for c, s, x in
                          zip(df[m["cust"]], df[m["supp"]], side)], index=df.index)
    elif m["cust"] or m["supp"]:
        side = pd.Series("AR" if m["cust"] else "AP", index=df.index).where(side == "", side)
    if f_credit is not None:
        credit = pd.Series(f_credit, index=df.index)
    col = pick(df, "ledger", "side", "item_type", "doc_type", "document_type", "type", required=False)
    if col:  # p.ej. 'AR_INVOICE', 'AP_CREDIT_NOTE', 'credit_note'
        v = df[col].str.upper()
        side = side.mask(v.str.contains(r"\bAR|RECEIVABLE|CUSTOMER|CLIENT"), "AR")
        side = side.mask(v.str.contains(r"\bAP|PAYABLE|SUPPLIER|VENDOR|PROVEED"), "AP")
        credit = credit | v.str.contains("CREDIT|ABONO|RECTIF")
    return side, credit


def _norm_items(df: pd.DataFrame, src: str) -> pd.DataFrame:
    m = {
        "id": pick(df, "item_id"),
        "number": pick(df, "document_number", "doc_number", "printed_number", "invoice_number",
                       "credit_note_number", "supplier_invoice_number", "number", "doc_no", required=False),
        "cust": pick(df, "customer_id", required=False),
        "supp": pick(df, "supplier_id", required=False),
        "party": pick(df, "counterparty_id", "party_id", "partner_id", required=False),
        "amount": pick(df, *AMOUNT_COLS),
        "ccy": pick(df, "currency", "ccy", required=False),
        "issue": pick(df, "issue_date", "invoice_date", "document_date", "date", required=False),
        "due": pick(df, "due_date", required=False),
        "entity": pick(df, "entity_id", "entity", required=False),
        "terms": pick(df, "payment_terms", required=False),
    }
    _log(src, m)
    col = lambda k: df[m[k]] if m[k] else pd.Series("", index=df.index)
    party = col("party")
    for k in ("supp", "cust"):  # customer_id gana si ambas están rellenas
        party = col(k).where(col(k) != "", party)
    out = pd.DataFrame({"id": df[m["id"]], "number": col("number"), "party": party,
                        "amount": to_num(df[m["amount"]]).abs(), "ccy": col("ccy"),
                        "issue": col("issue"), "due": col("due"), "entity": col("entity"),
                        "terms": col("terms"),
                        "total": to_num(df[pick(df, "total_amount", "gross_amount", "amount_total", *AMOUNT_COLS)]).abs()})
    out["side"], out["credit"] = _side_credit(df, m, src, out["id"])
    out["src"] = src
    return out


def load_items(data_dir: Path, until=None, start=None) -> pd.DataFrame:
    """Partidas candidatas con su importe abierto.

    Con open_items_at_start.csv: los datos (nº, tercero, divisa...) salen de los ficheros de
    documentos y el importe abierto del fichero de abiertos. Lo emitido antes de `start` que NO
    figura como abierto ya estaba liquidado y se descarta.
    """
    data_dir = Path(data_dir)
    docs = [_norm_items(read_csv(find(data_dir, f)), f) for f in DOC_FILES if find(data_dir, f)]
    items = pd.concat(docs, ignore_index=True).drop_duplicates("id", keep="first") if docs else None
    op = find(data_dir, OPEN_FILE)
    if op:
        o = _norm_items(read_csv(op), OPEN_FILE)
        open_amt = dict(zip(o["id"], o["amount"]))
        if items is None:
            items = o
        else:
            # abiertos que no están en los documentos: se usan tal cual
            items = pd.concat([items, o[~o["id"].isin(items["id"])]], ignore_index=True)
            # completar huecos (p.ej. divisa o tercero) con lo que traiga el fichero de abiertos
            oi = o.set_index("id")
            for c in ("number", "party", "ccy", "issue", "due", "entity", "terms"):
                fill = items["id"].map(oi[c])
                items[c] = items[c].where(items[c] != "", fill.fillna(""))
        is_open = items["id"].isin(open_amt.keys())
        items.loc[is_open, "amount"] = items.loc[is_open, "id"].map(open_amt)
        print(f"[load] {OPEN_FILE}: {len(open_amt):,} abiertas al inicio")
    if items is None:
        raise FileNotFoundError(f"No hay ficheros de partidas en {data_dir}")
    items["issue"] = pd.to_datetime(items["issue"], errors="coerce")
    items["due"] = pd.to_datetime(items["due"], errors="coerce")
    if op and start is not None:
        from .config import KEEP_CLOSED_DAYS
        old = items["issue"].notna() & (items["issue"] < start) & ~items["id"].isin(open_amt.keys())
        items.loc[old, "amount"] = 0.0          # ya liquidadas: abierto 0
        drop = old & (items["issue"] < start - pd.Timedelta(days=KEEP_CLOSED_DAYS))
        print(f"[load] {old.sum():,} partidas ya liquidadas al inicio ({(old & ~drop).sum():,} se conservan con abierto 0)")
        items = items[~drop]
    if until is not None:
        items = items[items["issue"].isna() | (items["issue"] <= until)]
    if (items["side"] == "").any():
        print(f"[load] AVISO: {(items['side'] == '').sum():,} partidas sin lado AR/AP")
    items["ndoc"] = items["number"].map(norm_doc)
    print(f"[load] partidas: {len(items):,} · " + ", ".join(
        f"{k}={v:,}" for k, v in items.groupby(["side", "credit"]).size().items()))
    return items.sort_values(["issue", "id"]).reset_index(drop=True)


# ---------------------------------------------------------------- maestros
def load_parties(masters: Path) -> pd.DataFrame:
    frames = []
    for f in ["customers.csv", "suppliers.csv"]:
        p = Path(masters) / f
        if not p.exists():
            continue
        df = read_csv(p)
        m = {"id": pick(df, "customer_id", "supplier_id", "party_id", "id"),
             "name": pick(df, "name", "legal_name", "customer_name", "supplier_name"),
             "tax": pick(df, "tax_id", "vat_id", "nif", "cif", "vat_number", required=False),
             "country": pick(df, "country", required=False),
             "entity": pick(df, "entity", required=False),
             "iban": pick(df, "iban", required=False)}
        _log(f, m)
        frames.append(pd.DataFrame({k: (df[v] if v else "") for k, v in m.items()}))
        tn = pick(df, "trade_name", required=False)
        if tn:  # el banco imprime a menudo el nombre comercial
            t = df[df[tn] != ""]
            frames.append(pd.DataFrame({"id": t[m["id"]], "name": t[tn], "tax": "",
                                        "country": t[m["country"]] if m["country"] else "",
                                        "entity": t[m["entity"]] if m["entity"] else "", "iban": ""}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["id", "name", "tax", "country", "entity", "iban"])


def load_terms(masters: Path) -> dict:
    """{código: (descuento como fracción, días de descuento)} de payment_terms.csv."""
    p = find(masters, "payment_terms.csv")
    if not p:
        return {}
    df = read_csv(p)
    pct, days = to_num(df["early_payment_discount_pct"]) / 100, to_num(df["discount_days"])
    return {c: (float(a), int(b)) for c, a, b in zip(df["code"], pct, days)}


def load_categories(masters: Path) -> list[str]:
    p = Path(masters) / "categories.csv"
    if not p.exists():
        return []
    df = read_csv(p)
    return df[pick(df, "category", "code", "category_code", "id")].tolist()


def load_fx(masters: Path):
    """{divisa: (fechas ordenadas 'YYYY-MM-DD', tipos)} con tipo = unidades de divisa por 1 EUR."""
    p = find(masters, "fx_rates.csv")
    if not p:
        return {}
    df = read_csv(p)
    d, c, r = (pick(df, "date", "rate_date"), pick(df, "currency", "ccy", "quote_currency"),
               pick(df, "units_per_eur", "rate", "eur_rate", "fx_rate"))
    print(f"[load] fx_rates: date<-{d}, ccy<-{c}, rate<-{r} (unidades por EUR)")
    df = pd.DataFrame({"d": df[d].str[:10], "c": df[c], "r": to_num(df[r])})
    df = df[df["r"] > 0].sort_values(["c", "d"])
    return {ccy: (g["d"].tolist(), g["r"].tolist()) for ccy, g in df.groupby("c")}


# ---------------------------------------------------------------- avisos de remesa
_LINE = re.compile(r"\b([A-Z]{2}\d{2}-?\d{4,7})\b.*?(-?[\d\.]+,\d{2}|-?\d+\.\d{2})\s*$")
_ADV = re.compile(r"(?:AVISO|ADVICE|REMESA|REMITTANCE)[^\d]{0,15}(\d{6,})", re.I)


def _amt(s: str) -> float:
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def load_remittances(data_dir: Path) -> dict:
    """{advice_key: [(ndoc, importe), ...]}. Claves: nº de aviso y nombre de fichero.
    Cubre .txt/.eml/.csv. TODO (P3): .xlsx y .pdf."""
    out = {}
    root = Path(data_dir) / "remittance_advices"
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file() or p.name == "index.csv":
            continue
        lines = []
        try:
            if p.suffix.lower() in (".txt", ".eml", ""):
                text = p.read_text(errors="ignore")
                for ln in text.splitlines():
                    mm = _LINE.search(ln.strip())
                    if mm:
                        lines.append((norm_doc(mm.group(1)), _amt(mm.group(2))))
                keys = set(_ADV.findall(text))
            elif p.suffix.lower() == ".csv":
                df = read_csv(p)
                dc = next((c for c in df.columns if re.search(r"doc|invoice|factura|number|ref", c, re.I)), None)
                ac = next((c for c in df.columns if re.search(r"amount|importe|paid", c, re.I)), None)
                if dc and ac:
                    lines = [(norm_doc(a), _amt(b)) for a, b in zip(df[dc], df[ac]) if a]
                keys = set()
            else:
                continue
        except Exception as e:  # un fichero roto no debe parar el proceso
            print(f"[remit] error en {p.name}: {e}")
            continue
        if not lines:
            continue
        keys |= set(re.findall(r"\d{6,}", p.stem))
        for k in keys:
            out[k] = lines
    print(f"[load] avisos de remesa: {len(out)} claves")
    return out


def load_golden(path: Path) -> pd.DataFrame:
    g = read_csv(path).copy()
    g["amount"] = to_num(g["amount"]) if "amount" in g else 0.0
    return g
