"""Aprende de TRAIN qué categoría llevan los movimientos sin partida, según la firma del concepto.

  python train_categories.py --data data/train
Guarda models/categories.pkl: {firma: (categoría, cuota, nº de casos)}.
"""
import argparse
import pickle
from collections import Counter, defaultdict
from pathlib import Path

from recon.common import find
from recon.load import load_golden, load_txns
from recon.rules.categories import signature


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("models/categories.pkl"))
    a = ap.parse_args()
    txns = load_txns(a.data)
    g = load_golden(find(a.data, "golden_allocations.csv"))
    has_item = set(g.loc[g["item_id"] != "", "bank_txn_id"])
    cat = dict(zip(g.loc[g["item_id"] == "", "bank_txn_id"], g.loc[g["item_id"] == "", "category"]))
    counts = defaultdict(Counter)
    for t in txns.itertuples(index=False):
        sig = signature(t)
        if t.id in cat:
            counts[sig][cat[t.id]] += 1
        elif t.id in has_item:
            counts[sig]["__HAS_ITEM__"] += 1   # la firma también aparece en movimientos con factura
    model = {}
    for sig, c in counts.items():
        best, n = c.most_common(1)[0]
        total = sum(c.values())
        if best != "__HAS_ITEM__":
            model[sig] = (best, n / total, n)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(model, open(a.out, "wb"))
    print(f"{len(model):,} firmas guardadas en {a.out}")
    for sig, (c, s, n) in sorted(model.items(), key=lambda x: -x[1][2])[:25]:
        print(f"  {n:>7,}  {s:5.1%}  {c:<22} {sig}")


if __name__ == "__main__":
    main()
