"""Avisos de remesa: el aviso dice qué facturas liquida el cobro (fuente de verdad en 1:N)."""
import re

from ..context import Row
from ..settle import settle


def remittance(t, ctx):
    if not ctx.remits:
        return None
    lines = None
    for k in re.findall(r"\d{6,}", t.ntext):
        if k in ctx.remits:
            lines = ctx.remits[k]
            break
    if not lines:
        return None
    side = "AR" if t.amount > 0 else "AP"
    ids = []
    for ndoc, _amt in lines:
        for i in ctx.by_doc.get(ndoc, ()):
            if ctx.is_open(i) and ctx.item[i]["side"] == side and i not in ids:
                ids.append(i)
    if not ids:
        return None
    return settle(t, ids, ctx)
