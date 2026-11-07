"""Pago de varias facturas sin referencia: el cliente paga sus facturas abiertas de la más antigua a la
más reciente (POLICIES §5.6, FIFO). Las facturas viejas que nunca se cobran (señuelos de años atrás) se
excluyen. Se acepta sólo si el acumulado cuadra al céntimo. Medido en TRAIN 2025-06: en 1.442 de 2.156
pagos exactos de varias facturas sin identificar, el conjunto pagado es ese tramo inicial."""
from .. import config as C
from ..settle import settle


def party_fifo(t, ctx):
    side = "AR" if t.amount > 0 else "AP"
    target = round(abs(t.amount) * 100)
    lo = t.date.toordinal() - C.FIFO_STALE_DAYS
    for p in ctx.parties_in(t)[:3]:
        pool = [i for i in ctx.open_items(p, side, t.date) if ctx.item[i]["issue_o"] >= lo]
        acc, pref = 0, []
        for i in pool[: C.FIFO_MAX_ITEMS]:
            acc += round(ctx.to_txn_ccy(i, ctx.signed_open(i), t) * 100)
            pref.append(i)
            if acc > target + 1 and not ctx.item[i]["credit"]:
                break
            if abs(acc - target) <= 1 and len(pref) >= 2:
                return settle(t, pref, ctx)
    return None
