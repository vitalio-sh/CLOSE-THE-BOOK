"""Sin referencia: identificar al tercero por nombre/NIF y casar por importe."""
from .. import config as C
from ..settle import settle


def _cands(t, ctx):
    side = "AR" if t.amount > 0 else "AP"
    out = []
    for p in ctx.parties_in(t):
        out += ctx.open_items(p, side, t.date)
    return out


def party_exact(t, ctx):
    """Tercero + importe exacto, la más antigua (FIFO). Índice por importe: O(1) por tercero."""
    paid = abs(t.amount)
    side = "AR" if t.amount > 0 else "AP"
    c = round(paid * 100)
    lim = t.date.toordinal() + C.ISSUE_SLACK_DAYS
    for p in ctx.parties_in(t):
        for k in (c, c - 1, c + 1):
            for i in ctx.by_amt.get((p, side, k), ()):
                it = ctx.item[i]
                if (not it["credit"] and ctx.is_open(i) and ctx.free(i) and it["issue_o"] <= lim
                        and (it["ccy"] or t.ccy) == t.ccy and abs(ctx.open[i] - paid) <= 0.01):
                    return settle(t, [i], ctx)
        if p in ctx.fx_parties and getattr(t, "orig_amount", ""):  # FX sólo con importe original
            i = fx_match(t, ctx, ctx.open_items(p, side, t.date), paid)
            if i:
                return settle(t, [i], ctx)
    return None


def fx_match(t, ctx, ids, paid):
    """Partida en otra divisa (POLICIES §3 FX): importe original exacto si el banco lo da; si no,
    el banco convierte a un tipo PEOR que el de referencia (0,1-1,5 %): ventana unilateral."""
    try:
        oa = float(str(getattr(t, "orig_amount", "") or "").replace(",", "."))
    except ValueError:
        oa = 0.0
    oc = getattr(t, "orig_ccy", "")
    best = []
    for i in ids:
        it = ctx.item[i]
        if it["credit"] or (it["ccy"] or t.ccy) == t.ccy:
            continue
        # sin importe original no se intenta: con miles de partidas en divisa siempre hay alguna
        # dentro del margen del tipo de cambio (medido en TRAIN: precisión 0,28)
        if oa and oc == it["ccy"] and abs(ctx.open[i] - oa) <= 0.01:
            return i
    return None


def party_subset(t, ctx):
    paid = abs(t.amount)
    cands = _cands(t, ctx)[: C.SUBSET_MAX_CANDIDATES]
    if len(cands) < 2:
        return None
    val = {i: round(ctx.to_txn_ccy(i, ctx.signed_open(i), t) * 100) for i in cands}
    target = round(paid * 100)
    # 1) prefijo FIFO: las N más antiguas
    acc, pref = 0, []
    for i in cands:
        acc += val[i]
        pref.append(i)
        if abs(acc - target) <= 1 and len(pref) > 1:
            return settle(t, pref, ctx)
    combo = find_combo([(i, val[i]) for i in cands], target, C.SUBSET_MAX_SIZE)
    # (completar sin facturas citadas se probó: precisión 0,36 en TRAIN 2025-06 -> descartado)
    return settle(t, combo, ctx) if combo else None


def find_combo(vals, target, kmax, tol=1):
    """Combinación de 2..kmax partidas que suma `target` (céntimos, ±tol). Hashing: O(n²)."""
    n = len(vals)
    def two(lo, tgt, excl=()):
        seen = {}
        for j in range(lo, n):
            if j in excl:
                continue
            i, v = vals[j]
            for d in range(-tol, tol + 1):
                k = seen.get(tgt + d - v)
                if k is not None:
                    return [vals[k][0], i]
            seen.setdefault(v, j)
        return None
    r = two(0, target)
    if r or kmax < 3:
        return r
    for a in range(n):
        r = two(a + 1, target - vals[a][1])
        if r:
            return [vals[a][0]] + r
    if kmax < 4:
        return None
    pairs = {}
    for a in range(n):
        for b in range(a + 1, n):
            pairs.setdefault(vals[a][1] + vals[b][1], []).append((a, b))
    for s1, lst in pairs.items():
        for d in range(-tol, tol + 1):
            for (a, b) in lst:
                for (c, e) in pairs.get(target + d - s1, ()):
                    if len({a, b, c, e}) == 4:
                        return [vals[x][0] for x in (a, b, c, e)]
    return None
