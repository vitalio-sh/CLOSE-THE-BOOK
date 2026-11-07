"""DUPLICATE_PAYMENT: se vuelve a pagar una partida ya liquidada (POLICIES §3). Importe = lo pagado."""
from ..context import Row


def duplicate(t, ctx):
    paid = round(abs(t.amount), 2)
    side = "AR" if t.amount > 0 else "AP"
    # 1) la referencia apunta a una partida ya cerrada con ese total
    for nd, _ in ctx.refs_in(t):
        for i in ctx.by_doc.get(nd, ()):
            it = ctx.item[i]
            if it["side"] == side and not it["credit"] and not ctx.is_open(i) \
                    and abs(it["total"] - paid) <= 0.01:
                return [Row(i, paid, "", "DUPLICATE_PAYMENT")]
    # 2) sin referencia: tercero con UNA partida cerrada reciente de ese total y ninguna abierta igual
    lo = t.date.toordinal() - 120
    hits = []
    c = round(paid * 100)
    for p in ctx.parties_in(t):
        for k in (c - 1, c, c + 1):
            for i in ctx.by_total.get((p, side, k), ()):
                if not ctx.is_open(i) and ctx.item[i]["issue_o"] >= lo and i not in hits:
                    hits.append(i)
    if len(hits) == 1:
        return [Row(hits[0], paid, "", "DUPLICATE_PAYMENT")]
    return None
