"""Bucle principal: recorre los movimientos por fecha y aplica las reglas en orden."""
import time
from collections import Counter

import pandas as pd

from .context import Row
from .rules import RULES


def reconcile(txns: pd.DataFrame, ctx, llm=None) -> tuple[pd.DataFrame, Counter]:
    out, stats, pending = [], Counter(), []
    t0 = time.time()
    for n, t in enumerate(txns.itertuples(index=False), 1):
        rows, who = None, "UNIDENTIFIED"
        ctx.cur = t.id
        for rule in RULES:
            try:
                rows = rule(t, ctx)
            except Exception as e:  # una regla rota no tumba la ejecución
                stats[f"ERROR:{rule.__name__}"] += 1
                if stats[f"ERROR:{rule.__name__}"] <= 3:
                    print(f"[engine] {rule.__name__} falló en {t.id}: {e!r}")
                rows = None
            if rows:
                who = rule.__name__
                break
        if not rows:  # UNIDENTIFIED es para cobros; un pago sin casar suele ser comisión
            rows = ([Row("", 0.0, "UNIDENTIFIED", "UNIDENTIFIED")] if t.amount > 0
                    else [Row("", 0.0, "BANK_FEES", "")])
            pending.append(t)
        ctx.apply(t, rows)
        stats[who] += 1
        for r in rows:
            out.append((t.id, r.item_id, f"{r.amount:.2f}" if r.item_id else "", r.category, r.exception, who))
        if n % 20000 == 0:
            print(f"[engine] {n:,}/{len(txns):,} movimientos · {time.time() - t0:.0f}s")
    df = pd.DataFrame(out, columns=["bank_txn_id", "item_id", "amount", "category", "exception", "rule"])
    if llm is not None and pending:
        try:
            df = llm(df, pending, ctx)
        except Exception as e:  # sin clave, sin paquete, sin red...: seguimos con las reglas
            print(f"[engine] LLM desactivado por error: {e!r}")
    print(f"[engine] terminado en {time.time() - t0:.0f}s")
    return df, stats


EXCEPTIONS = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING",
              "SHORT_DISPUTE", "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]


def validate(df: pd.DataFrame, txns: pd.DataFrame) -> list[str]:
    """Comprobaciones rápidas en memoria (además de official_check sobre el fichero)."""
    errs = []
    missing = set(txns["id"]) - set(df["bank_txn_id"])
    if missing:
        errs.append(f"{len(missing)} movimientos sin fila (cuentan como fallo)")
    unknown = set(df["bank_txn_id"]) - set(txns["id"])
    if unknown:
        errs.append(f"{len(unknown)} bank_txn_id que no son de la fase")
    dup = df[df["item_id"] != ""].duplicated(["bank_txn_id", "item_id"]).sum()
    if dup:
        errs.append(f"{dup} pares (movimiento, partida) duplicados")
    noitem = df[df["item_id"] == ""]
    if noitem["bank_txn_id"].duplicated().any():
        errs.append("más de una fila sin partida en algún movimiento")
    mixed = set(noitem["bank_txn_id"]) & set(df.loc[df["item_id"] != "", "bank_txn_id"])
    if mixed:
        errs.append(f"{len(mixed)} movimientos con partidas y fila de categoría a la vez")
    bad = df[(df["item_id"] != "") & ~df["amount"].str.fullmatch(r"\d+\.\d{2}")]
    if len(bad):
        errs.append(f"{len(bad)} importes mal formados o negativos")
    return errs


def official_check(path, txn_ids, categories) -> dict:
    """Pasa el fichero escrito por el validador OFICIAL (recon/bankrec_score.py, el mismo código del
    servidor). Golden vacío: sólo interesa la validación, no la nota."""
    from . import bankrec_score as S
    g = S.Golden()
    g.exceptions, g.categories = list(EXCEPTIONS), list(categories)
    g.txns = {t: ({}, "", "", "x") for t in txn_ids}
    return S.score_submission(g, open(path, "rb").read(), str(path))


def official_score(path, golden_df, txn_ids, categories) -> dict:
    """Nota OFICIAL (recon/bankrec_score.py) de un fichero contra un golden en memoria
    (TRAIN filtrado a la ventana evaluada). Devuelve el dict del scorer: score, metrics, segments."""
    from . import bankrec_score as S
    g = S.Golden()
    g.exceptions, g.categories = list(EXCEPTIONS), list(categories)
    txns = {t: [{}, "", "", ""] for t in txn_ids}
    for t, item, amt, cat, exc, seg in zip(golden_df["bank_txn_id"], golden_df["item_id"], golden_df["amount"],
                                           golden_df["category"], golden_df["exception"],
                                           golden_df.get("segment", [""] * len(golden_df))):
        rec = txns.setdefault(t, [{}, "", "", ""])
        rec[3] = seg
        if item:
            rec[0][item] = (float(amt), exc)
            g.n_pairs += 1
        else:
            rec[1], rec[2] = cat, exc
    for t, rec in txns.items():
        g.txns[t] = (rec[0], rec[1], rec[2], rec[3] or "unlabelled")
        g.weight[t] = sum(a for a, _ in rec[0].values()) or 1.0
    return S.score_submission(g, open(path, "rb").read(), str(path))
