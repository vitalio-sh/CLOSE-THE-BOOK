"""Movimientos enlazados con un documento (aviso, remesa SEPA, tarjeta, cheques, fichero de pagos).
El documento manda: partidas, importes y motivos salen de él (POLICIES §2, §3, §5.3-5.4)."""
import re

import pandas as pd
from types import SimpleNamespace

from ..common import norm, tol
from ..context import Row

_DISC = re.compile(r"DISC|DTO|DESC|SKONTO|ESCOMPTE|PRONTO|EARLY", re.I)
FULL = ("", "SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES")


def plan(t, g, ctx) -> list:
    """[(item_id, Line)] para el grupo `g` enlazado con `t` (se calcula una vez, al enlazar)."""
    side = "AP" if g.kind in ("run", "confirming") else "AR"
    out, seen = [], set()
    for l in g.lines:
        iid = ctx.docs.find(l.doc, side, t.entity, l.amount)
        if iid is None and g.kind in ("dd", "cheque") and l.name:
            iid = _by_name(l, side, t, ctx, seen)
        if iid and iid not in seen:
            seen.add(iid)
            out.append((iid, l))
    return out


def _by_due(t, pl, ctx):
    """Confirming: el cargo de hoy paga las facturas del run que vencen hoy (y suman el cargo)."""
    paid = abs(t.amount)
    for w in (0, 1, 3, 7):
        sel = [(i, l) for i, l in pl
               if ctx.item[i]["due"] is not None and not pd.isna(ctx.item[i]["due"])
               and abs((ctx.item[i]["due"] - t.date).days) <= w]
        if sel and abs(sum(l.amount for _, l in sel) - paid) <= 0.01:
            return sel
    return []


def _by_name(l, side, t, ctx, seen):
    """Recibo sin nº legible: tercero por nombre + importe exacto (FIFO)."""
    fake = SimpleNamespace(ntext=norm(l.name))
    for p in ctx.parties_in(fake):
        for i in ctx.open_items(p, side, t.date):
            if i not in seen and abs(ctx.open[i] - abs(l.amount)) <= 0.01:
                return i
    return None


def evidence(t, ctx):
    pl = ctx.plan.get(t.id)
    if not pl:
        return None
    g = ctx.linked[t.id]
    rows = []
    for iid, l in pl:
        o = ctx.open[iid]
        if o <= 0.005:
            continue  # ya liquidada por otro movimiento
        p = abs(l.amount)
        if ctx.item[iid]["credit"]:
            rows.append(Row(iid, round(o, 2)))
            continue
        exc, a = "", o
        if g.kind == "advice" and abs(l.deduction) > 0.005:
            if _DISC.search(l.reason):
                exc = "SHORT_DISCOUNT"
            else:
                exc, a = "SHORT_DISPUTE", p
        elif p < o - tol(o):
            exc, a = "PARTIAL", p
        if (ctx.item[iid]["ccy"] or t.ccy) != t.ccy and exc in FULL:
            exc, a = "FX_DIFFERENCE", o
        rows.append(Row(iid, round(a, 2), "", exc))
    return rows or None
