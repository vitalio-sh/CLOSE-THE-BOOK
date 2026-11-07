"""Números de factura impresos en el concepto -> partidas; si no cuadra, se completa con otras
partidas recientes del mismo tercero (el texto se corta)."""
import re

from ..settle import settle
from .complete import complete

_NUM = re.compile(r"(?<![A-Z0-9])(\d{5,7})(?![0-9])")


def by_reference(t, ctx):
    refs = ctx.refs_in(t)
    side = "AR" if t.amount > 0 else "AP"
    parties = ctx.parties_in(t)
    ids = ctx.items_for_refs(refs, parties, side, t.date, t) if refs else []
    if not ids:
        return None
    party = ctx.item[ids[0]]["party"]
    full = complete(t, ctx, ids, side, party)
    if full:
        return settle(t, full, ctx)
    # Abonos abiertos del mismo tercero que completan exactamente el importe (factura - abono)
    paid = abs(t.amount)
    exp = sum(ctx.signed_open(i) for i in ids)
    if exp - paid > 0.01:
        for c in ctx.open_items(party, side, t.date):
            if ctx.item[c]["credit"] and c not in ids and abs(exp - ctx.open[c] - paid) <= 0.01:
                ids.append(c)
                break
    return settle(t, ids, ctx)
