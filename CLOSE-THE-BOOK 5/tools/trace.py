"""Traza de reglas para movimientos concretos (depurar por qué una línea no casa).

    python tools/trace.py --window train_2506 BT-2025-0171234 BT-2025-0171235

Construye el contexto de la ventana una vez (~4 min en TRAIN), reproduce el motor hasta cada movimiento
pedido (para que el libro esté como en la ejecución real) y muestra: terceros detectados, partidas abiertas
candidatas, lo que devuelve CADA regla, y el golden.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os  # noqa: E402
os.chdir(ROOT)
from run import setup  # noqa: E402
from recon.rules import RULES  # noqa: E402
from tools.eval import WINDOWS  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--window", default="train_2506")
    a = ap.parse_args()
    w = WINDOWS[a.window]
    arg = lambda k: w[w.index(k) + 1] if k in w else None
    _, txns, _, golden, ctx, *_ = setup(Path(arg("--data")), Path("data/masters"), arg("--from"), arg("--to"), True)
    want = set(a.ids)
    for t in txns.itertuples(index=False):
        ctx.cur = t.id
        if t.id in want:
            print(f"\n=== {t.id} {t.date.date()} {t.amount} {t.ccy} {t.entity}\n    {t.text[:160]}")
            ps = ctx.parties_in(t)
            print(f"    terceros: {[(p, ctx.party_name.get(p)) for p in ps[:5]]}")
            side = "AR" if t.amount > 0 else "AP"
            for p in ps[:3]:
                oi = ctx.open_items(p, side, t.date)
                print(f"    abiertas de {p}: {len(oi)} -> " + ", ".join(
                    f"{ctx.item[i]['number']}={ctx.open[i]:.2f}{ctx.item[i]['ccy']}({str(ctx.item[i]['issue'])[:10]},{ctx.item[i]['terms']})"
                    for i in oi[:8]))
            for r in RULES:
                try:
                    rows = r(t, ctx)
                except Exception as e:
                    rows = f"ERROR {e!r}"
                if rows:
                    print(f"    {r.__name__:<16} -> {[(x.item_id, x.amount, x.category, x.exception) for x in rows][:6] if isinstance(rows, list) else rows}")
            g = golden[golden.bank_txn_id == t.id]
            print("    golden:", [(i, am, c, e) for i, am, c, e in zip(g.item_id, g.amount, g.category, g.exception)][:6])
            want.discard(t.id)
            if not want:
                break
        rows = None
        for r in RULES:
            try:
                rows = r(t, ctx)
            except Exception:
                rows = None
            if rows:
                break
        if rows:
            ctx.apply(t, rows)


if __name__ == "__main__":
    main()
