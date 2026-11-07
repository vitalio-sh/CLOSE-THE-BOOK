"""Movimientos sin partida (comisiones, nóminas, impuestos, cash pooling...).
Modelo aprendido de TRAIN: firma del concepto -> categoría (ver train_categories.py)."""
import re

from .. import config as C
from ..context import Row


def signature(t) -> str:
    sign = "+" if t.amount > 0 else "-"
    toks = [w for w in t.ntext.split() if not re.search(r"\d", w)][:4]
    return sign + " ".join(toks)


def _cat(t, ctx, share_min):
    hit = ctx.cat_model.get(signature(t))
    if hit and hit[1] >= share_min and hit[2] >= C.CATEGORY_MIN_COUNT:
        return [Row("", 0.0, hit[0], "")]
    return None


def category_strict(t, ctx):
    return _cat(t, ctx, C.CATEGORY_MIN_SHARE_STRICT)


def category_loose(t, ctx):
    return _cat(t, ctx, C.CATEGORY_MIN_SHARE)
