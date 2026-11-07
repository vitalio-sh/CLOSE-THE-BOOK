"""Puntuación local según /reglas:
    Score = 100 × (0.5·Allocation F1 + 0.3·Transaction accuracy + 0.2·Exception macro-F1)
La referencia es el scorer de starter/; esto sirve para el desglose por regla y por excepción.
"""
from collections import defaultdict

import pandas as pd

from .common import tol


def _f1(p, r):
    return 2 * p * r / (p + r) if p + r else 0.0


def score(sub: pd.DataFrame, gold: pd.DataFrame) -> dict:
    sub = sub.fillna("").astype({"amount": str})
    gold = gold.fillna("")
    S = defaultdict(list)
    G = defaultdict(list)
    for r in sub.itertuples(index=False):
        S[r.bank_txn_id].append((r.item_id, float(r.amount or 0), r.category, r.exception))
    for r in gold.itertuples(index=False):
        G[r.bank_txn_id].append((r.item_id, float(r.amount or 0), r.category, r.exception))

    # Allocation F1 sobre pares (movimiento, partida) con importe en tolerancia
    hits = n_sub = n_gold = 0
    exc = defaultdict(lambda: [0, 0, 0])  # clase -> [tp, fp, fn]
    ok_txn = 0
    for tid, grows in G.items():
        srows = S.get(tid, [])
        gi = {g[0]: g for g in grows if g[0]}
        si = {s[0]: s for s in srows if s[0]}
        n_gold += len(gi)
        n_sub += len(si)
        h = sum(1 for k, s in si.items() if k in gi and abs(s[1] - gi[k][1]) <= tol(gi[k][1]))
        hits += h
        # Transaction accuracy: conjunto completo igual
        same = (set(si) == set(gi) and h == len(gi)
                and all(si[k][3] == gi[k][3] for k in gi)
                and sorted((g[2], g[3]) for g in grows if not g[0]) == sorted((s[2], s[3]) for s in srows if not s[0]))
        ok_txn += same
        # Excepciones por fila (clave = partida o '' para filas sin partida)
        ge = {(g[0]): g[3] for g in grows}
        se = {(s[0]): s[3] for s in srows}
        for k in set(ge) | set(se):
            a, b = se.get(k, ""), ge.get(k, "")
            if a == b and a:
                exc[a][0] += 1
            else:
                if a:
                    exc[a][1] += 1
                if b:
                    exc[b][2] += 1
    p = hits / n_sub if n_sub else 0
    r = hits / n_gold if n_gold else 0
    alloc = _f1(p, r)
    acc = ok_txn / len(G) if G else 0
    per = {}
    for c, (tp, fp, fn) in exc.items():
        if tp + fn == 0:
            continue  # sólo clases presentes en el golden
        per[c] = _f1(tp / (tp + fp) if tp + fp else 0, tp / (tp + fn))
    macro = sum(per.values()) / len(per) if per else 0
    return {"score": 100 * (0.5 * alloc + 0.3 * acc + 0.2 * macro),
            "alloc_f1": alloc, "alloc_p": p, "alloc_r": r, "txn_acc": acc,
            "exc_macro_f1": macro, "exc_f1": dict(sorted(per.items(), key=lambda x: x[1]))}


def by_rule(sub: pd.DataFrame, gold: pd.DataFrame) -> pd.DataFrame:
    """Precisión por regla: qué regla mete más errores (para decidir dónde trabajar)."""
    gk = {(r.bank_txn_id, r.item_id, r.category, r.exception) for r in gold.fillna("").itertuples(index=False)}
    sub = sub.copy()
    sub["ok"] = [(a, b, c, d) in gk for a, b, c, d in
                 zip(sub.bank_txn_id, sub.item_id, sub.category, sub.exception)]
    return sub.groupby("rule")["ok"].agg(filas="size", precision="mean").sort_values("filas", ascending=False)
