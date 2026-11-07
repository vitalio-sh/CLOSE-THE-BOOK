"""Ejecuta la conciliación de una fase y escribe el CSV de envío.

Ejemplos:
  # Evaluar en un trozo de TRAIN (rápido, con golden):
  python run.py --data data/train --masters data/masters --from 2025-01-01 --to 2025-06-30 --eval
  # Mes de muestra:
  python run.py --data data/sample --masters data/masters --eval
  # LIVE / FINAL:
  python run.py --data data/live  --masters data/masters --out out/live.csv
  python run.py --data data/final --masters data/masters --out out/final.csv
"""
import os
import sys

# Resultados reproducibles: el orden de los set/dict de cadenas depende del hash aleatorio de Python.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)

import argparse
import pickle
from pathlib import Path

import pandas as pd

from recon import config as C
from recon.common import find
from recon.context import Context
import gzip
import time
import shutil
import sys

import json

from recon.engine import official_check, official_score, reconcile, validate
from recon.load import (load_categories, load_fx, load_golden, load_items, load_parties,
                        load_remittances, load_terms, load_txns)
from recon.score import by_rule, score


def prime_from_golden(items, golden, txns_all, start):
    """En TRAIN no hay open_items_at_start: reconstruimos lo abierto en `start` con el golden."""
    dates = pd.Series(txns_all["date"].values, index=txns_all["id"].values)
    g = golden[golden["item_id"] != ""]
    d = g["bank_txn_id"].map(dates)
    g = g[(d < start).values].assign(_d=d[(d < start).values].values).sort_values("_d", kind="stable")
    opn = dict(zip(items["id"], items["amount"]))
    for iid, exc, amt in zip(g["item_id"].tolist(), g["exception"].tolist(), g["amount"].tolist()):
        o = opn.get(iid)
        if o is None:
            continue
        if exc in ("PARTIAL", "SHORT_DISPUTE"):
            opn[iid] = max(0.0, o - amt)
        elif exc == "RETURNED":
            opn[iid] = o + amt
        else:
            opn[iid] = 0.0
    items = items.assign(amount=items["id"].map(opn))
    keep = (items["amount"] > 0.005) | (items["issue"] >= start - pd.Timedelta(days=C.KEEP_CLOSED_DAYS))
    return items[keep]


def setup(data, masters, start=None, end=None, with_golden=False, catmodel=Path("models/categories.pkl")):
    """Carga datos y prepara el contexto (partidas, terceros, PDF, evidencia, devoluciones).
    Lo usan run.py y tools/trace.py."""
    T0 = time.time()
    txns_all = load_txns(data)
    start = pd.Timestamp(start) if start else txns_all["date"].min()
    end = pd.Timestamp(end) if end else txns_all["date"].max()
    txns = txns_all[(txns_all["date"] >= start) & (txns_all["date"] <= end)].reset_index(drop=True)
    print(f"[run] {len(txns):,} movimientos entre {start.date()} y {end.date()}")

    items = load_items(data, until=end + pd.Timedelta(days=C.ISSUE_SLACK_DAYS), start=start)
    golden = None
    if with_golden:
        golden = load_golden(find(data, "golden_allocations.csv"))
        if not find(data, "open_items_at_start.csv"):
            items = prime_from_golden(items, golden, txns_all, start)

    cat_model = pickle.load(open(catmodel, "rb")) if Path(catmodel).exists() else {}
    ctx = Context(items, load_parties(masters), {}, load_fx(masters),
                  cat_model, load_categories(masters), load_terms(masters))
    from recon.pdfclauses import load_clauses
    ctx.setup_pdf(*load_clauses(data, items, start))
    T_ctx = time.time() - T0
    print(f"[run] {T_ctx:.0f}s · contexto listo")
    from recon.evidence import link, load_advices, load_batches
    groups = load_advices(data, Path("models"), start, end) + load_batches(data, start, end)
    ctx.setup_evidence(txns, link(txns, groups, {}))
    from recon.rules.keywords import find_reversals
    from recon.evidence import ReturnIndex
    ctx.reversals = find_reversals(txns)
    ctx.returns = ReturnIndex(data, masters, start, end)
    return txns_all, txns, items, golden, ctx, start, end, T_ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--masters", required=True, type=Path)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--out", type=Path, default=Path("out/submission.csv"))
    ap.add_argument("--eval", action="store_true", help="puntuar contra golden_allocations.csv")
    ap.add_argument("--catmodel", type=Path, default=Path("models/categories.pkl"))
    ap.add_argument("--llm", action="store_true", help="usar Claude en los no identificados")
    ap.add_argument("--json", type=Path, help="escribir métricas (nota oficial, tiempos, reglas) en JSON")
    a = ap.parse_args()

    T0 = time.time()
    txns_all, txns, items, golden, ctx, start, end, T_ctx = setup(a.data, a.masters, a.start, a.end, a.eval, a.catmodel)
    T_ev = time.time() - T0
    print(f"[run] {T_ev:.0f}s · evidencia lista")
    llm = None
    if a.llm or C.LLM_ENABLED:
        from recon.llm_assist import resolve
        llm = resolve

    df, stats = reconcile(txns, ctx, llm)
    T_eng = time.time() - T0
    print("[run] movimientos por regla:")
    for k, v in stats.most_common():
        print(f"   {k:<20} {v:>8,}  {v / len(txns):6.1%}")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    df.drop(columns="rule").to_csv(a.out, index=False)
    df.to_csv(a.out.with_suffix(".debug.csv"), index=False)
    gz = a.out.with_name(a.out.name + ".gz")
    with open(a.out, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g)
    errs = validate(df, txns)
    # En LIVE/FINAL la fase es el fichero entero; con --from/--to sólo el tramo evaluado
    phase_ids = txns["id"] if (a.start or a.end) else txns_all["id"]
    res = official_check(gz, phase_ids, load_categories(a.masters))
    errs += [f"fila {e['row']}: {e['message']}" for e in res["errors"]]
    n_in, n_out = len(txns), df["bank_txn_id"].nunique()
    print(f"[run] escrito {gz} ({len(df):,} filas, {n_out:,}/{n_in:,} movimientos, "
          f"{gz.stat().st_size / 1e6:.1f} MB)")
    if res["ok"] and not errs:
        print("[run] VALIDACIÓN: OK (validador oficial + comprobaciones locales)")
    else:
        print("[run] VALIDACIÓN: FALLA — NO SUBIR")
        for e in errs[:20]:
            print("   ", e)
        for f in (a.out, gz):  # que no se pueda subir por error
            f.rename(f.with_name(f.name.replace(".csv", ".INVALID.csv")))

    out = {"data": str(a.data), "from": str(start.date()), "to": str(end.date()), "lines": len(txns),
           "rows": len(df), "valid": bool(res["ok"] and not errs), "errors": errs[:20],
           "timing_s": {"context": round(T_ctx), "evidence": round(T_ev - T_ctx), "engine": round(T_eng - T_ev),
                        "total": round(time.time() - T0)},
           "rules": {k: v for k, v in stats.most_common()}}
    if golden is not None:
        g = golden[golden["bank_txn_id"].isin(set(txns["id"]))]
        if res["ok"]:
            off = official_score(gz, g, txns["id"], load_categories(a.masters))
            out.update(score=off["score"], metrics=off["metrics"],
                       segments={k: v for k, v in off["segments"].items()})
        br = by_rule(df, g)
        out["rule_precision"] = {r: {"rows": int(x.filas), "precision": round(float(x.precision), 4)}
                                 for r, x in br.iterrows()}
    if golden is not None:
        s = score(df, g)
        out["exc_f1"] = {k: round(v, 4) for k, v in s["exc_f1"].items()}
        print(f"\n=== SCORE {s['score']:.2f} ===  allocF1 {s['alloc_f1']:.3f} "
              f"(P {s['alloc_p']:.3f} R {s['alloc_r']:.3f}) · txnAcc {s['txn_acc']:.3f} · excF1 {s['exc_macro_f1']:.3f}")
        print("F1 por excepción (peor primero):")
        for k, v in s["exc_f1"].items():
            print(f"   {k:<20} {v:.3f}")
        print("\nPrecisión por regla:\n", by_rule(df, g))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(out, indent=1, default=str))
        print(f"[run] métricas en {a.json}")
    return 0 if out["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
