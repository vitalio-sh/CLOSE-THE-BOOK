"""Cláusulas que sólo están en el PDF de la factura (POLICIES §5.6-5.8):
plazos ("3 vencimientos (a / b / c EUR)"), retención en garantía, anticipo a deducir y
"anula y sustituye a la factura X".

Resultado:
  partial[ndoc] = {céntimos}   importes que un cobro PARCIAL de esa factura puede tener
  replaces[ndoc_viejo] = ndoc_nuevo
"""
import hashlib
import os
import pickle
import re
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from .common import norm_doc, read_csv

_AMT = re.compile(r"-?\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2}(?!\d)")
_KEY = re.compile(r"vencimiento|plazo|instal|échéance|echeance|rate|retenci|retention|garant|einbehalt|"
                  r"anticip|advance|acompte|anzahlung|proforma|neto a pagar|net à payer|net to pay|"
                  r"a pagar|to pay|à payer|zu zahlen", re.I)
_REPL = re.compile(r"(?:sustituye|remplace|replaces|ersetzt|sostituisce)\D{0,40}?"
                   r"([A-Z]{2,4}[-\s/]?\d{2}[-\s/]?\d{3,8})", re.I)


def _num(s: str) -> float:
    s = s.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace("." if s.rfind(",") > s.rfind(".") else ",", "")
    return float(s.replace(",", "."))


def parse_pdf(p: Path) -> dict:
    txt = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True).stdout
    flat = " ".join(txt.split())
    out = {"amounts": [], "instal": [], "replaces": None}
    m = _REPL.search(flat)
    if m:
        out["replaces"] = norm_doc(m.group(1))
    # frases con palabras clave -> importes citados
    for sent in re.split(r"(?<=[.;])\s+(?=[A-ZÁÉÍÓÚ])", flat):
        if not _KEY.search(sent):
            continue
        vals = [_num(a) for a in _AMT.findall(sent)]
        out["amounts"] += [v for v in vals if v > 0]
        lst = re.search(r"\(([\d.,\s/]+)\s*[A-Z]{3}\)", sent)
        if lst and "/" in lst.group(1):
            out["instal"] = [_num(x) for x in lst.group(1).split("/") if _AMT.search(x)]
    return out


def _safe(p):
    try:
        return parse_pdf(p)
    except Exception:
        return {"amounts": [], "instal": [], "replaces": None}


def load_clauses(data_dir: Path, items: pd.DataFrame, start=None, cache_dir=Path("models")) -> tuple[dict, dict]:
    root = Path(data_dir) / "pdfs"
    ip = root / "index.csv"
    if not ip.exists():
        return {}, {}
    idx = read_csv(ip)
    idx = idx[idx["document_type"] == "invoice"]
    if start is not None:  # facturas que pueden seguir abiertas
        idx = idx[pd.to_datetime(idx["document_date"], errors="coerce") >= start - pd.Timedelta(days=900)]
    cp = Path(cache_dir) / f"pdfclauses_{hashlib.md5(str(root.resolve()).encode()).hexdigest()[:10]}.pkl"
    cache = pickle.load(open(cp, "rb")) if cp.exists() else {}
    todo = [f for f in idx["file"] if f not in cache]
    if todo:
        with ProcessPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            for f, r in zip(todo, ex.map(_safe, [root / f for f in todo], chunksize=32)):
                cache[f] = r
        cp.parent.mkdir(parents=True, exist_ok=True)
        pickle.dump(cache, open(cp, "wb"))
    total = dict(zip(items["ndoc"], items["total"]))
    partial, replaces = {}, {}
    for f, num in zip(idx["file"], idx["document_number"]):
        c = cache.get(f)
        if not c:
            continue
        nd = norm_doc(num)
        if c["replaces"]:
            replaces[c["replaces"]] = nd
        tot = total.get(nd)
        if tot is None or not (c["amounts"] or c["instal"]):
            continue
        s = set()
        for a in c["amounts"]:
            if 0 < a < tot - 0.01:
                s.add(round(a * 100))              # importe a pagar / plazo
                s.add(round((tot - a) * 100))      # total menos retención / anticipo
        for a in c["instal"][:-1]:                 # todos los plazos salvo el último son PARTIAL
            s.add(round(a * 100))
        if s:
            partial[nd] = s
    print(f"[pdf] {len(idx):,} facturas con PDF · {len(partial):,} con cobro parcial previsto · "
          f"{len(replaces):,} sustituciones ({len(todo):,} leídos ahora)")
    return partial, replaces
