"""Dadas unas partidas elegidas y un movimiento, decide importes y excepciones (POLICIES.md §2-3).

Importes SIEMPRE en la divisa de la partida. Las diferencias se calculan en la divisa del movimiento
y se convierten de vuelta con el mismo tipo.
"""
import pandas as pd

from . import config as C
from .context import Row


def settle(t, ids, ctx) -> list[Row]:
    """Filas para liquidar `ids` con el movimiento `t`. Supone que las partidas son las correctas."""
    paid = abs(t.amount)
    opens = {i: ctx.open[i] for i in ids}
    credit = {i: ctx.item[i]["credit"] for i in ids}
    # valor de cada partida en divisa del movimiento (abonos restan)
    conv = {i: ctx.to_txn_ccy(i, opens[i], t) for i in ids}
    expected = sum(-v if credit[i] else v for i, v in conv.items())
    d = expected - paid                      # > 0: pagan de menos; < 0: pagan de más
    rows = [Row(i, round(o, 2)) for i, o in opens.items()]
    inv = [r for r in rows if not credit[r.item_id]]

    # §3 FX_DIFFERENCE: divisa distinta y dentro del 3 % -> todas las facturas, completas
    if any((ctx.item[i]["ccy"] or t.ccy) != t.ccy for i in ids) and abs(d) <= C.FX_MAX_REL * max(expected, 0.01):
        for r in inv:
            r.exception = "FX_DIFFERENCE"
        return rows
    if abs(d) <= C.EXACT_EPS or not inv:
        return rows

    if d < 0:  # §3 OVERPAYMENT: importe = abierto, el exceso queda en cuenta
        inv[-1].exception = "OVERPAYMENT"
        return rows

    # pagan de menos. 1) ¿retención o descuento sobre el total de todas las facturas? -> todas
    if len(inv) > 1:
        tot = sum(conv[r.item_id] for r in inv)
        near = lambda x: abs(d - x) <= C.EXACT_EPS + 0.005 * len(inv)
        if ctx.withholds(ctx.item[inv[0].item_id]["party"]) and any(near(tot * w) for w in C.WITHHOLDING_RATES):
            for r in inv:
                r.exception = "SHORT_WITHHOLDING"
            return rows
        disc = [(r, *_discount(r.item_id, t, ctx)) for r in inv]
        if all(pct and ok for _, pct, ok in disc) and near(sum(round(conv[r.item_id] * pct, 2) for r, pct, _ in disc)):
            for r in inv:
                r.exception = "SHORT_DISCOUNT"
            return rows
    # 2) ¿qué factura absorbe la diferencia?
    blame, why = _blame(inv, conv, d, t, ctx)
    if why in ("SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES"):
        blame.exception = why                # partida completa
        return rows
    # §2 PARTIAL / SHORT_DISPUTE: importe = lo realmente aplicado, en divisa de la partida
    i = blame.item_id
    applied_txn = conv[i] - d
    if applied_txn <= 0.005 and len(ids) > 1:
        # nada de esta factura se cobró: no forma parte del pago (antes salía SHORT_DISPUTE con 0,00)
        return settle(t, [x for x in ids if x != i], ctx)
    blame.amount = round(max(0.0, opens[i] * applied_txn / conv[i]) if conv[i] else 0.0, 2)
    blame.exception = "PARTIAL" if (why == "LATE_DISCOUNT" or len(rows) == 1) else "SHORT_DISPUTE"
    return rows


def _discount(i, t, ctx):
    """(% de descuento, ¿dentro de plazo?) según payment_terms; (0, False) si no tiene."""
    pct, days = ctx.terms.get(ctx.item[i].get("terms", ""), (0.0, 0))
    if not pct:
        return 0.0, False
    issue = ctx.item[i]["issue"]
    ok = pd.isna(issue) or t.date <= issue + pd.Timedelta(days=days + C.DISCOUNT_GRACE_DAYS)
    return pct, ok


def _blame(inv, conv, d, t, ctx):
    """Factura a la que se atribuye la diferencia `d` (divisa del movimiento) y el motivo."""
    for r in inv:
        base = conv[r.item_id]
        # §3: descuento sobre el TOTAL de la factura, redondeado al céntimo
        pct, in_time = _discount(r.item_id, t, ctx)
        if pct and abs(d - round(base * pct, 2)) <= C.EXACT_EPS + 0.005:
            return r, "SHORT_DISCOUNT" if in_time else "LATE_DISCOUNT"
        if ctx.withholds(ctx.item[r.item_id]["party"]) and \
                any(abs(d - round(base * w, 2)) <= C.EXACT_EPS + 0.005 for w in C.WITHHOLDING_RATES):
            return r, "SHORT_WITHHOLDING"
    big = max(inv, key=lambda r: conv[r.item_id])
    eur = d / (ctx.rate(t.ccy, str(t.date)[:10]) or 1.0)
    if (eur <= C.BANK_CHARGES_MAX_EUR and d <= C.BANK_CHARGES_MAX_REL * conv[big.item_id]
            and any(h in t.ntext for h in C.FOREIGN_HINTS)):
        return big, "SHORT_BANK_CHARGES"
    return big, ""
