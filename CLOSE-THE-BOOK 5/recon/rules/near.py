"""Tercero identificado pero el importe no cuadra exacto: sólo se acepta si la diferencia la explica
una política (descuento en plazo, retención, comisiones SHA/BEN, FX, sobrepago pequeño). Si no, nada:
POLICIES pide no forzar."""
from .. import config as C
from ..settle import settle
from .party import _cands, find_combo

# Medido en TRAIN 2025-06: OVERPAYMENT y FX_DIFFERENCE sin referencia casi siempre eran otra factura.
OK = {"SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES"}


def _accept(rows):
    exc = {r.exception for r in rows if r.exception}
    return bool(exc) and exc <= OK


def party_near(t, ctx):
    paid = abs(t.amount)
    cands = [i for i in _cands(t, ctx) if not ctx.item[i]["credit"]]
    if not cands:
        return None
    val = {i: ctx.to_txn_ccy(i, ctx.open[i], t) for i in cands}
    # 1 factura: hueco pequeño (<= 10 % de menos o <= OVERPAY_MAX_REL de más), la más cercana
    near = sorted((abs(val[i] - paid), i) for i in cands if 0 < val[i] - paid <= 0.105 * val[i])
    for _, i in near[:3]:
        rows = settle(t, [i], ctx)
        if _accept(rows):
            return rows
    # varias facturas con retención / descuento sobre el total
    cands = cands[: C.SUBSET_MAX_CANDIDATES]
    rates = sorted({p for p, _ in ctx.terms.values() if p})
    if any(ctx.withholds(p) for p in ctx.parties_in(t)):
        rates = C.WITHHOLDING_RATES + rates
    for w in rates:
        tgt = round(paid / (1 - w) * 100)
        combo = find_combo([(i, round(val[i] * 100)) for i in cands], tgt, C.SUBSET_MAX_SIZE,
                           tol=3)  # redondeo por factura; más tolerancia dispara la búsqueda
        if combo:
            rows = settle(t, combo, ctx)
            if _accept(rows):
                return rows
    return None
