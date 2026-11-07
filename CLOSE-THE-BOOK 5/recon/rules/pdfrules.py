"""Cobro parcial previsto en el PDF (plazos, retención en garantía, anticipo): PARTIAL con lo cobrado."""
from ..context import Row


def pdf_partial(t, ctx):
    if not ctx.pdf_partial or t.amount <= 0:
        return None
    c = round(t.amount * 100)
    hits = []
    for p in ctx.parties_in(t):
        for i in ctx.open_items(p, "AR", t.date):
            s = ctx.pdf_partial.get(i)
            if s and (c in s or c - 1 in s or c + 1 in s) and (ctx.item[i]["ccy"] or t.ccy) == t.ccy:
                hits.append(i)
    if len(hits) == 1 or (hits and len(set(hits)) == 1):
        return [Row(hits[0], round(abs(t.amount), 2), "", "PARTIAL")]
    return None
