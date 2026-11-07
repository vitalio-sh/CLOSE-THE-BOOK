"""Completar un cobro: el texto del banco se corta y sólo cita las primeras facturas. Con las citadas
como ancla, se buscan otras partidas abiertas RECIENTES del mismo tercero (facturas y abonos) que
cuadren al céntimo con lo que falta."""
from .. import config as C
from ..evidence import _subset_k


def complete(t, ctx, anchors, side, party):
    """Lista de partidas (anclas + añadidas) que suma el cobro, o None."""
    paid = round(abs(t.amount) * 100)
    sval = lambda i: round(ctx.to_txn_ccy(i, ctx.signed_open(i), t) * 100)
    rest = paid - sum(sval(i) for i in anchors)
    if abs(rest) <= 1:
        return list(anchors)
    # sólo cobros a clientes cuyo texto se cortó: faltan facturas ENTERAS (medido en TRAIN: completar
    # pagos a proveedores o huecos pequeños crea sumas casuales -> precisión 0,5)
    if not anchors or side != "AR" or rest < C.COMPLETE_MIN_REST * paid:
        return None
    io = [ctx.item[i]["issue_o"] for i in anchors]
    lo, hi = min(io) - C.COMPLETE_WINDOW_DAYS, max(io) + C.COMPLETE_WINDOW_DAYS
    pool = [i for i in ctx.open_items(party, side, t.date)
            if i not in anchors and lo <= ctx.item[i]["issue_o"] <= hi]
    if not pool or len(pool) > C.COMPLETE_MAX_POOL:
        pool = pool[-C.COMPLETE_MAX_POOL:]
    inv = [(sval(i), i) for i in pool if not ctx.item[i]["credit"]]
    cre = [(sval(i), i) for i in pool if ctx.item[i]["credit"]]
    # 1) prefijo por fecha (pagan todo lo vencido hasta una fecha)
    acc, pref = 0, []
    for v, i in sorted(inv + cre, key=lambda x: ctx.item[x[1]]["issue_o"]):
        acc += v
        pref.append(i)
        if abs(acc - rest) <= 1:
            return list(anchors) + pref
    # 2) subconjunto de facturas, con 0..2 abonos restados
    inv_s = sorted([(v, i) for v, i in inv if v > 0], reverse=True)
    cre_opts = [[]] + [[c] for c in cre] + [[a, b] for k, a in enumerate(cre) for b in cre[k + 1:]][:30]
    for cs in cre_opts:
        tgt = rest - sum(v for v, _ in cs)
        if tgt <= 0:
            continue
        pick = _subset_k([x for x in inv_s if x[0] <= tgt + 1], tgt, None, budget=C.COMPLETE_BUDGET,
                         kmax=C.COMPLETE_MAX_K)
        if pick:
            return list(anchors) + pick + [i for _, i in cs]
    return None
