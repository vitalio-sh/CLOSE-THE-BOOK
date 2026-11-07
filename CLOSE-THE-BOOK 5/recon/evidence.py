"""Evidencia documental (POLICIES §5.3-5.4): avisos de remesa, remesas SEPA, liquidaciones de tarjeta,
remesas de cheques y ficheros de pagos.

Cada fichero se convierte en "grupos" (= lo que liquida UN apunte bancario):
    Group(kind, keys, entity, account, date, total, lines=[Line(doc, amount, gross, deduction, reason, ...)])

Se enlazan con los movimientos ANTES del bucle por fechas (los avisos llegan días después del cobro):
  1) por clave: id de aviso / remesa / liquidación / run / end_to_end_id citado en el texto del banco;
  2) sin clave: misma cuenta (o sociedad) + importe exacto + fecha cercana, y sólo si es único.
Las partidas enlazadas quedan reservadas para su movimiento: las reglas difusas no las pueden coger.
"""
import hashlib
import pickle
import re
import subprocess
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .common import norm, norm_doc, read_csv, to_num


@dataclass
class Line:
    doc: str                 # nº de documento tal cual (normalizado con norm_doc al resolver)
    amount: float            # lo pagado por ese documento (signo: <0 abonos)
    gross: float = 0.0
    deduction: float = 0.0
    reason: str = ""
    name: str = ""           # deudor / librador / beneficiario
    credit: bool = False


@dataclass
class Group:
    kind: str                # advice | dd | card | cheque | run
    keys: list
    entity: str = ""
    account: str = ""
    date: object = None
    total: float = 0.0       # importe esperado del apunte (divisa del apunte si se conoce)
    lines: list = field(default_factory=list)
    src: str = ""
    transfers: dict = field(default_factory=dict)   # confirming: e2e -> (importe, [Line])


# ------------------------------------------------------------------------- utilidades
_AMT = re.compile(r"(?<![\d/])-?\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2}(?![\d/])|(?<![\d/.,])-?\d+[.,]\d{2}(?![\d/])")
_DATE = re.compile(r"\b(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
_DOCTOK = re.compile(r"^[A-Z]{0,5}[-/]?[A-Z]{0,3}\d[\w\-/.]*$")
_REFHDR = re.compile(r"(?:payment[ _]?ref\.?|ref\.?|r[ée]f[ée]rence|referenz|subject:.*?(?:advice|aviso de pago|avis de paiement|"
                     r"zahlungsavis|aviso)|payment advice|aviso de pago)[\s,;:]*([A-Z0-9][A-Z0-9/\-]{5,})", re.I)


def amt(s) -> float:
    s = str(s).strip().replace(" ", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace("." if s.rfind(",") > s.rfind(".") else ",", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def digits(s) -> str:
    return re.sub(r"\D", "", str(s))


def key_of(s) -> str:
    """Clave de enlace: sólo dígitos (los bancos cortan o separan prefijos y barras)."""
    return digits(s)


# ------------------------------------------------------------------------- avisos de remesa
def _advice_text(p: Path) -> str:
    ext = p.suffix.lower()
    if ext == ".pdf":
        return subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True).stdout
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        out = []
        for ws in wb:
            for row in ws.iter_rows(values_only=True):
                cells = []
                for c in row:
                    if c is None:
                        cells.append("")
                    elif isinstance(c, float):
                        cells.append(f"{c:.2f}")
                    else:
                        cells.append(str(c)[:10] if hasattr(c, "year") else str(c))
                out.append("   ".join(cells))
        return "\n".join(out)
    txt = p.read_text(errors="ignore")
    if ext == ".csv":  # columnas -> separadas por espacios (mismo parser que el resto)
        delim = ";" if txt.count(";") > txt.count(",") else ","
        txt = "\n".join("   ".join(c.strip() for c in ln.split(delim)) for ln in txt.splitlines())
    return txt


def parse_advice(p: Path) -> dict:
    """{'refs': [...], 'date': 'YYYY-MM-DD'|None, 'lines': [Line]} de cualquier formato."""
    txt = _advice_text(p)
    lines, refs = [], []
    head = "\n".join(txt.splitlines()[:12])
    for m in _REFHDR.finditer(head):
        refs.append(m.group(1))
    m = re.search(r"payment_ref\s+(\S+)", head)  # csv: 'payment_ref,REF,...'
    if m:
        refs.append(m.group(1))
    m = re.search(r"Payment advice\s+(\S+)", head, re.I)  # xlsx
    if m:
        refs.append(m.group(1))
    for ln in txt.splitlines():
        parts = ln.strip().split()
        if not parts:
            continue
        doc = parts[0].upper()
        if not _DOCTOK.match(doc) or len(digits(doc)) < 4 or _DATE.fullmatch(doc):
            continue
        rest = ln.strip()[len(parts[0]):]
        dm = _DATE.search(rest)
        if not dm:
            continue
        rest = rest[dm.end():]
        nums = [(m.start(), m.group()) for m in _AMT.finditer(rest)]
        if not nums:
            continue
        vals = [amt(v) for _, v in nums]
        if len(vals) == 1:
            gross = paid = vals[0]
            ded = 0.0
        elif len(vals) == 2:
            gross, paid = vals
            ded = round(paid - gross, 2)
        else:
            gross, ded, paid = vals[0], vals[1], vals[2]
        # motivo = letras entre la deducción y lo pagado (o tras ellas)
        reason = ""
        if abs(ded) > 0.005:
            seg = rest[nums[0][0] + len(nums[0][1]): nums[-1][0]] if len(nums) >= 2 else rest
            reason = " ".join(w for w in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.\-]+", seg))
            if not reason:  # csv: motivo en columna aparte, tras el importe pagado
                reason = " ".join(re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.\-]+", rest))
        lines.append(Line(doc, paid, gross, ded, reason.strip(), credit=paid < 0 or gross < 0))
    date = None
    m = re.search(r"(?:payment_date|date|fecha|datum)\s*[:\s]\s*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", head, re.I)
    if m:
        date = pd.to_datetime(m.group(1), dayfirst="/" in m.group(1), errors="coerce")
    return {"refs": list(dict.fromkeys(refs)), "date": date, "lines": lines}


def _in_window(stem_date: str, start, end) -> bool:
    if start is None or not stem_date:
        return True
    d = pd.Timestamp(stem_date)
    return start - pd.Timedelta(days=45) <= d <= end + pd.Timedelta(days=30)


def _safe_parse(p):
    try:
        return parse_advice(p)
    except Exception as e:
        print(f"[evidence] aviso ilegible {p.name}: {e!r}")
        return {"refs": [], "date": None, "lines": []}


def load_advices(data_dir: Path, cache_dir: Path | None = None, start=None, end=None) -> list[Group]:
    root = Path(data_dir) / "remittance_advices"
    if not root.exists():
        return []
    idx = {}
    ip = root / "index.csv"
    if ip.exists():
        df = read_csv(ip)
        idx = {r["file"]: r for _, r in df.iterrows()}
    cache = {}
    cp = Path(cache_dir or "models") / f"advices_{hashlib.md5(str(root.resolve()).encode()).hexdigest()[:10]}.pkl"
    if cp.exists():
        cache = pickle.load(open(cp, "rb"))
    out, dirty = [], False
    files = []
    for p in sorted(root.rglob("RA_*")):
        m = re.match(r"RA_(\d{8})_", p.name)
        if _in_window(m.group(1) if m else "", start, end):
            files.append((p, str(p.relative_to(root)), p.stat().st_mtime))
    todo = [(p, rel, mt) for p, rel, mt in files if (rel, mt) not in cache]
    if todo:  # pdftotext es lo lento: en paralelo (FINAL llega con miles de avisos nuevos)
        from concurrent.futures import ProcessPoolExecutor
        import os
        with ProcessPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            for (p, rel, mt), a in zip(todo, ex.map(_safe_parse, [p for p, _, _ in todo], chunksize=16)):
                cache[(rel, mt)] = a
        dirty = True
        print(f"[evidence] {len(todo):,} avisos nuevos leídos")
    for p, rel, mt in files:
        a = cache[(rel, mt)]
        if not a["lines"]:
            continue
        meta = idx.get(rel, {})
        refs = a["refs"] + [m for m in re.findall(r"\d{6,}", p.stem)[-1:]]
        date = a["date"] if a["date"] is not None and not pd.isna(a["date"]) else pd.to_datetime(meta.get("received_date"), errors="coerce")
        out.append(Group("advice", [key_of(r) for r in refs if len(key_of(r)) >= 6],
                         entity=meta.get("entity", rel.split("/")[0]), date=date,
                         total=round(sum(l.amount for l in a["lines"]), 2), lines=a["lines"],
                         src=rel))
        for l in a["lines"]:
            l.name = meta.get("payer_name", "")
    if dirty:
        cp.parent.mkdir(parents=True, exist_ok=True)
        pickle.dump(cache, open(cp, "wb"))
    print(f"[evidence] avisos: {len(out):,} con líneas, {sum(len(g.lines) for g in out):,} documentos")
    return out


# ------------------------------------------------------------------------- ficheros de lotes
def _read_dir(data_dir, sub, start=None, end=None, back_days=45):
    root = Path(data_dir) / sub
    if not root.exists():
        return pd.DataFrame()
    files = [p for p in sorted(root.glob("*.csv*")) if p.name != "index.csv"]
    if start is not None:
        lo = (start - pd.Timedelta(days=back_days)).strftime("%Y%m")
        hi = (end + pd.Timedelta(days=31)).strftime("%Y%m")
        files = [p for p in files if not (m := re.search(r"_(\d{6})(?:_|\.)", p.name))
                 or lo <= m.group(1) <= hi]
    fr = [read_csv(p).assign(_src=p.name) for p in files]
    return pd.concat(fr, ignore_index=True) if fr else pd.DataFrame()


def load_batches(data_dir: Path, start=None, end=None) -> list[Group]:
    out = []
    dd = _read_dir(data_dir, "direct_debit_batches", start, end)
    if len(dd):
        dd["amount"] = to_num(dd["amount"])
        for bid, g in dd.groupby("batch_id", sort=False):
            r = g.iloc[0]
            out.append(Group("dd", [key_of(bid)], entity=r["_src"][:4], account=r["account_id"],
                             date=pd.to_datetime(r["collection_date"]), total=round(g["amount"].sum(), 2),
                             lines=[Line(ri, a, name=n, gross=a) for ri, a, n in
                                    zip(g["remittance_info"], g["amount"], g["debtor_name"])], src=r["_src"]))
    cs = _read_dir(data_dir, "card_settlements", start, end)
    if len(cs):
        cs["gross"], cs["net"] = to_num(cs["gross"]), to_num(cs["net"])
        for sid, g in cs.groupby("settlement_id", sort=False):
            r = g.iloc[0]
            out.append(Group("card", [key_of(sid)], entity=r["_src"][:4], account=r["account_id"],
                             date=pd.to_datetime(r["settlement_date"]), total=round(g["net"].sum(), 2),
                             lines=[Line(o, a, gross=a) for o, a in zip(g["order_number"], g["gross"])], src=r["_src"]))
    ch = _read_dir(data_dir, "cheque_deposits", start, end)
    if len(ch):
        ch["amount"] = to_num(ch["amount"])
        for did, g in ch.groupby("deposit_id", sort=False):
            r = g.iloc[0]
            out.append(Group("cheque", [key_of(did)], entity=r["_src"][:4], account=r["account_id"],
                             date=pd.to_datetime(r["deposit_date"]), total=round(g["amount"].sum(), 2),
                             lines=[Line(s, a, name=n, gross=a) for s, a, n in
                                    zip(g["stub_reference"], g["amount"], g["drawer"])], src=r["_src"]))
    pr = _read_dir(data_dir, "payment_runs", start, end, back_days=120)  # confirming vence tarde
    if len(pr):
        pr["document_amount"], pr["transfer_amount"] = to_num(pr["document_amount"]), to_num(pr["transfer_amount"])
        # transfers: un cargo por transferencia (end_to_end_id); aggregated: uno por run;
        # confirming: un cargo por fecha de VENCIMIENTO de las facturas -> grupo por run y se
        # filtra por vencimiento al resolver (rules/evidence_rule.plan)
        pr["_grp"] = [e if t == "transfers" else r
                      for t, r, e in zip(pr["run_type"], pr["run_id"], pr["end_to_end_id"])]
        for gid, g in pr.groupby("_grp", sort=False):
            r = g.iloc[0]
            keys = [key_of(r["run_id"])] + ([key_of(r["end_to_end_id"])] if r["run_type"] == "transfers" else [])
            total = (r["transfer_amount"] if r["run_type"] == "transfers" else g["document_amount"].sum())
            lines = [Line(d, a, gross=a, name=b, credit=(a < 0 or "CREDIT" in str(ty).upper()))
                     for d, a, b, ty in zip(g["document_number"], g["document_amount"],
                                            g["beneficiary"], g["document_type"])]
            grp = Group("confirming" if r["run_type"] == "confirming" else "run", keys,
                        entity=r["_src"][:4], account=r["debtor_account_id"],
                        date=pd.to_datetime(r["execution_date"]), total=round(-abs(total), 2),
                        lines=lines, src=r["_src"])
            if grp.kind == "confirming":  # un cargo = N transferencias completas del run
                for e2e, ta, l in zip(g["end_to_end_id"], g["transfer_amount"], lines):
                    grp.transfers.setdefault(e2e, [round(abs(ta), 2), []])[1].append(l)
            out.append(grp)
    c = defaultdict(int)
    for g in out:
        c[g.kind] += 1
    print(f"[evidence] lotes: {dict(c)}")
    return out


# ------------------------------------------------------------------------- enlace con movimientos
def link(txns: pd.DataFrame, groups: list[Group], accounts: dict) -> dict:
    """{bank_txn_id: Group}. accounts: {account_id: entity}."""
    by_key = defaultdict(list)
    for g in groups:
        for k in g.keys:
            if len(k) >= 8:
                by_key[k].append(g)
    by_acc_amt = defaultdict(list)    # (cuenta, importe) -> grupos   (lotes)
    by_ent_amt = defaultdict(list)    # (sociedad, importe) -> grupos (avisos)
    for g in groups:
        if g.kind == "advice":
            by_ent_amt[(g.entity, round(abs(g.total), 2))].append(g)
        elif g.account:
            by_acc_amt[(g.account, round(abs(g.total), 2))].append(g)
    linked, used = {}, set()
    near = lambda g, t, lo, hi: g.date is None or pd.isna(g.date) or lo <= (t.date - g.date).days <= hi

    def fits(g, t):
        if (g.total < 0) != (t.amount < 0):          # p.ej. comisión de una remesa de cobros
            return False
        if g.kind == "advice":
            return g.entity in ("", t.entity) and near(g, t, -20, 20)
        if g.kind == "confirming":                   # un cargo por vencimiento: fechas lejanas
            return True
        return (not g.account or g.account == t.account or g.kind == "run") and near(g, t, -10, 10)

    # lotes: el apunte es el total exacto del lote; avisos y confirming: no (deducciones, FX, vencimientos)
    exact_only = lambda g: g.kind not in ("advice", "confirming")

    for t in txns.itertuples(index=False):
        # 1) por clave
        cands = []
        for tok in dict.fromkeys(re.split(r"[\s|+:]+", t.text.upper())):
            d = digits(tok)
            if len(d) < 8:
                continue
            for n in range(len(d), 7, -1):  # la clave puede ir cortada o con sufijo
                for g in by_key.get(d[:n], ()):
                    if g not in cands and fits(g, t):
                        cands.append(g)
        conf = [g for g in cands if g.kind == "confirming"]
        if conf:
            sub = _confirming_debit(t, conf[0])
            if sub is not None:
                linked[t.id] = sub
                continue
        cands = [g for g in cands if g.kind != "confirming"]
        if cands:
            amts = _amounts(t)
            exact = [g for g in cands if any(abs(abs(g.total) - x) <= 0.01 for x in amts)]
            cands = [g for g in cands if not exact_only(g) or g in exact]
            pick = exact[0] if len(exact) == 1 else (cands[0] if len(cands) == 1 else None)
            if pick is None and exact:
                pick = min(exact, key=lambda g: abs((t.date - g.date).days) if g.date is not None else 99)
            if pick is not None and id(pick) not in used:
                linked[t.id] = pick
                if pick.kind != "confirming":       # confirming: varios cargos por run
                    used.add(id(pick))
                continue
        # 2) sin clave: importe exacto en la misma cuenta / sociedad y fecha cercana, único
        cands = []
        for amtt in _amounts(t):
            cands += [g for g in by_acc_amt.get((t.account, amtt), []) + by_ent_amt.get((t.entity, amtt), [])
                      if id(g) not in used and fits(g, t) and g.kind != "confirming" and g not in cands]
        if len(cands) == 1:
            linked[t.id] = cands[0]
            used.add(id(cands[0]))
    c = defaultdict(int)
    for g in linked.values():
        c[g.kind] += 1
    print(f"[evidence] movimientos enlazados: {dict(c)} de {len(groups):,} grupos")
    return linked


def _amounts(t) -> list:
    """Importes con los que puede cuadrar un documento: el del apunte y, si lo hay, el original
    (pago en divisa: el fichero de pagos va en la divisa de la factura)."""
    out = [round(abs(t.amount), 2)]
    try:
        oa = round(abs(float(str(getattr(t, "orig_amount", "") or "0").replace(",", "."))), 2)
    except ValueError:
        oa = 0.0
    if oa and oa not in out:
        out.append(oa)
    return out


_NPROV = re.compile(r"(\d+)\s+PROVEEDORES")


def _confirming_debit(t, g):
    """Cargo de confirming: N transferencias completas aún no usadas del run que suman el cargo."""
    target = round(abs(t.amount) * 100)
    m = _NPROV.search(t.text.upper())
    k = int(m.group(1)) if m else None
    used = g.__dict__.setdefault("_used", set())
    items = sorted(((round(a * 100), e) for e, (a, _) in g.transfers.items() if e not in used), reverse=True)
    pick = _subset_k(items, target, k)
    if pick is None:
        return None
    used.update(pick)
    lines = [l for e in pick for l in g.transfers[e][1]]
    return Group("run", g.keys, entity=g.entity, account=g.account, date=t.date,
                 total=-abs(t.amount), lines=lines, src=g.src)


def _subset_k(items, target, k, tol=1, budget=300_000, kmax=8):
    """Subconjunto de `items` [(céntimos, id)] (orden desc.) que suma target±tol; tamaño k si se da.
    Búsqueda en profundidad con poda; None si no hay o se agota el presupuesto."""
    n = len(items)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + items[i][0]
    cnt = [0]
    chosen = []

    def dfs(i, rem, left):
        cnt[0] += 1
        if cnt[0] > budget:
            return False
        if abs(rem) <= tol and (left is None or left == 0):
            return True
        if i >= n or rem < -tol or suffix[i] < rem - tol or (left is not None and (left <= 0 or n - i < left)):
            return False
        v, e = items[i]
        if v <= rem + tol:
            chosen.append(e)
            if dfs(i + 1, rem - v, None if left is None else left - 1):
                return True
            chosen.pop()
        return dfs(i + 1, rem, left)

    if k is None:
        for kk in range(1, min(n, kmax) + 1):
            chosen.clear(); cnt[0] = 0
            if dfs(0, target, kk):
                return list(chosen)
        return None
    return list(chosen) if dfs(0, target, k) else None


# ------------------------------------------------------------------------- resolución de documentos
class DocIndex:
    """Nº de documento -> partidas, tolerando prefijo omitido, ceros y truncado."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.tail = defaultdict(list)      # últimos 7 dígitos -> ids
        for iid, it in ctx.item.items():
            d = digits(it["number"])
            if len(d) >= 5:
                self.tail[d[-7:]].append(iid)
        self.sorted_docs = sorted(ctx.by_doc)

    def find(self, doc, side, entity=None, amount=None, credit=None) -> str | None:
        ctx = self.ctx
        nd = norm_doc(doc)
        ids = list(ctx.by_doc.get(nd, ()))
        if not ids:
            d = digits(doc)
            if len(d) >= 5:
                ids = list(self.tail.get(d[-7:], ()))
        if not ids and len(nd) >= 6:  # referencia truncada: prefijo único
            k = bisect_left(self.sorted_docs, nd)
            pref = []
            while k < len(self.sorted_docs) and self.sorted_docs[k].startswith(nd) and len(pref) < 20:
                pref += ctx.by_doc[self.sorted_docs[k]]
                k += 1
            ids = pref
        ids = [i for i in ids if ctx.item[i]["side"] == side and (credit is None or ctx.item[i]["credit"] == credit)]
        if entity and side == "AR":
            ids = [i for i in ids if ctx.item[i]["entity"] in ("", entity)] or ids
        if len(ids) > 1:
            op = [i for i in ids if ctx.is_open(i)]
            ids = op or ids
        if len(ids) > 1 and amount is not None:
            ids.sort(key=lambda i: abs(ctx.item[i]["total"] - abs(amount)))
        return ids[0] if ids else None


# ------------------------------------------------------------------------- devoluciones
_MANDATE = re.compile(r"VTM-[A-Z]{2}-\d{5,}")
_CHEQUE = re.compile(r"(?:CHEQUE IMPAYE|RETURNED ITEM CHK|CHQ|CHEQUE)\D{0,12}(\d{6,8})")
_CARD = re.compile(r"\*{2,}(\d{4})")


class ReturnIndex:
    """Datos para saber QUÉ partida vuelve en una devolución (POLICIES §3 RETURNED):
    mandato SEPA -> recibos cobrados; nº de cheque -> cheque remesado; tarjeta -> pedido."""

    def __init__(self, data_dir, masters, start=None, end=None):
        self.dd = defaultdict(list)       # mandato -> [(fecha, importe, doc, deudor)]
        self.chq = {}                     # nº cheque -> (importe, doc, librador)
        self.card = defaultdict(list)     # últimos 4 -> [(importe, pedido)]
        self.mandate_cust = {}
        dd = _read_dir(data_dir, "direct_debit_batches", start, end, back_days=120)
        for r in dd.itertuples(index=False):
            self.dd[r.mandate_id].append((r.collection_date, amt(r.amount), r.remittance_info, r.debtor_name))
        ch = _read_dir(data_dir, "cheque_deposits", start, end, back_days=120)
        for r in ch.itertuples(index=False):
            self.chq[digits(r.cheque_number)] = (amt(r.amount), r.stub_reference, r.drawer)
        cs = _read_dir(data_dir, "card_settlements", start, end, back_days=120)
        for r in cs.itertuples(index=False):
            self.card[digits(r.card)[-4:]].append((amt(r.gross), r.order_number))
        p = Path(masters) / "customers.csv"
        if p.exists():
            c = read_csv(p)
            if "sepa_mandate_id" in c:
                self.mandate_cust = {m: i for m, i in zip(c["sepa_mandate_id"], c["customer_id"]) if m}
        print(f"[evidence] devoluciones: {len(self.dd):,} mandatos, {len(self.chq):,} cheques, "
              f"{len(self.mandate_cust):,} mandatos en maestro")

    def find(self, t, ctx) -> str | None:
        txt, a = t.text.upper(), round(abs(t.amount), 2)
        docs = []
        m = _MANDATE.search(txt)
        if m:
            docs = [d for dt, x, d, _ in sorted(self.dd.get(m.group(), []), reverse=True)
                    if abs(x - a) <= 0.01 and dt <= str(t.date)[:10]]
            if not docs:  # recibo de un mes anterior que no tenemos: tercero del mandato + importe
                cust = self.mandate_cust.get(m.group())
                if cust:
                    return _last_paid(ctx, cust, a, t.date)
        if not docs:
            m = _CHEQUE.search(txt)
            if m and digits(m.group(1)) in self.chq:
                x, d, _ = self.chq[digits(m.group(1))]
                docs = [d] if abs(x - a) <= 0.01 else []
        if not docs and ("CHARGEBACK" in txt or "CONTRACARGO" in txt):
            m = _CARD.search(txt)
            if m:
                docs = [o for x, o in self.card.get(m.group(1), []) if abs(x - a) <= 0.01]
        for d in docs:
            iid = ctx.docs.find(d, "AR", t.entity, a)
            if iid:
                return iid
        return None


def _last_paid(ctx, party, a, date):
    """Partida AR del tercero con ese total, la más reciente emitida antes de `date`."""
    best, lim, c = None, date.toordinal(), round(a * 100)
    for k in (c - 1, c, c + 1):
        for iid in ctx.by_total.get((party, "AR", k), ()):
            io = ctx.item[iid]["issue_o"]
            if io <= lim and (best is None or io > ctx.item[best]["issue_o"]):
                best = iid
    return best
