"""Devoluciones (RETURNED): recibo SEPA devuelto, cheque impagado, contracargo de tarjeta, transferencia
rechazada. Importe = lo que se reabre (POLICIES §2-3)."""
from .. import config as C
from ..context import Row


def returned(t, ctx):
    if t.amount >= 0:
        return None
    hint = any(h in t.ntext for h in C.RETURN_HINTS)
    amt = round(abs(t.amount), 2)
    if ctx.returns is not None and (hint or "CHARGEBACK" in t.ntext or "CONTRACARGO" in t.ntext):
        iid = ctx.returns.find(t, ctx)
        if iid:
            return [Row(iid, amt, "", "RETURNED")]
    if not hint:
        return None
    for p in ctx.parties_in(t):  # cobrado en este mismo periodo
        for date, iid, a in reversed(ctx.history.get(p, [])):
            if abs(a - amt) <= 0.01 and ctx.item[iid]["side"] == "AR":
                return [Row(iid, round(a, 2), "", "RETURNED")]
    return None
