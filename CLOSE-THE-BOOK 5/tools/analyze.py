"""Análisis de errores de un experimento (salida de tools/eval.py).

    python tools/analyze.py errors out/eval/<tag> --window train_2506   # dónde fallamos, con ejemplos
    python tools/analyze.py oracle out/eval/<tag> --window train_2506   # cuántos puntos vale cada segmento

`errors` escribe out/eval/<tag>/<window>_errors.md. `oracle` sustituye nuestras filas por el golden en cada
segmento y re-puntúa con el scorer oficial: la ganancia es el máximo que se puede sacar de ese segmento.
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from recon.common import find, read_csv  # noqa: E402
from tools.eval import WINDOWS  # noqa: E402


def window_data(win):
    args = WINDOWS[win]
    data = ROOT / args[args.index("--data") + 1]
    return data, (args[args.index("--from") + 1] if "--from" in args else None), \
        (args[args.index("--to") + 1] if "--to" in args else None)


def load(exp, win):
    data, _, _ = window_data(win)
    d = pd.read_csv(exp / f"{win}.debug.csv", dtype=str, keep_default_na=False)
    ids = set(d.bank_txn_id)
    g = read_csv(find(data, "golden_allocations.csv"))
    g = g[g.bank_txn_id.isin(ids)].copy()
    t = read_csv(find(data, "bank_transactions.csv"))
    t = t[t.bank_txn_id.isin(ids)].set_index("bank_txn_id")
    return data, d, g, t


def numbers(data):
    num = {}
    for f, col in (("ar_invoices.csv", "document_number"), ("ap_invoices.csv", "supplier_invoice_number"),
                   ("credit_notes.csv", "document_number"), ("open_items_at_start.csv", "document_number")):
        p = find(data, f)
        if p:
            x = read_csv(p)
            num.update(zip(x["item_id"], x[col]))
    return num


def errors(exp, win, n_examples=4):
    data, d, g, t = load(exp, win)
    num = numbers(data)
    seg = dict(zip(g.bank_txn_id, g.segment))
    gk = {(a, b): (float(c or 0), e) for a, b, c, e in zip(g.bank_txn_id, g.item_id, g.amount, g.exception)}
    ours = set(zip(d.bank_txn_id, d.item_id))
    gi = g[g.item_id != ""]
    gi = gi.assign(hit=[(a, b) in ours for a, b in zip(gi.bank_txn_id, gi.item_id)])
    rec = gi.groupby("segment").agg(pairs=("hit", "size"), recall=("hit", "mean"))
    rec["missed"] = (rec.pairs * (1 - rec.recall)).round().astype(int)
    rec = rec.sort_values("missed", ascending=False)

    def status(r):
        k = gk.get((r.bank_txn_id, r.item_id))
        if not r.item_id:
            gc = g[(g.bank_txn_id == r.bank_txn_id) & (g.item_id == "")]
            return "ok" if len(gc) and gc.category.iloc[0] == r.category else "wrong_noitem"
        if not k:
            return "wrong_item"
        if abs(float(r.amount) - k[0]) > max(0.01, 0.005 * k[0]):
            return "wrong_amount"
        return "ok" if r.exception == k[1] else "wrong_exception"
    di = d[d.item_id != ""].copy()
    di["st"], di["seg"] = di.apply(status, axis=1), di.bank_txn_id.map(seg)
    wrong = di[di.st != "ok"].groupby(["rule", "seg"]).size().sort_values(ascending=False).head(15)
    rule_of = d.drop_duplicates("bank_txn_id").set_index("bank_txn_id").rule

    out = [f"# Errores · {exp.name} · {win}\n",
           "## Pares del golden que no damos (recall por segmento)\n", rec.head(15).to_markdown(), "",
           "## Filas que damos y están mal (regla × segmento)\n", wrong.to_frame("filas").to_markdown(), "",
           "## Ejemplos de los segmentos con más pérdida\n"]
    for s in rec.index[:4]:
        miss = gi[(gi.segment == s) & ~gi.hit]
        out.append(f"### {s}  (faltan {rec.loc[s, 'missed']} de {rec.loc[s, 'pairs']})\n")
        for tid in miss.bank_txn_id.unique()[:n_examples]:
            r = t.loc[tid]
            gold = [num.get(i, i) for i in g[g.bank_txn_id == tid].item_id if i][:8]
            mine = [num.get(i, i) or "(sin partida)" for i in d[d.bank_txn_id == tid].item_id][:8]
            out.append(f"- `{tid}` {r.entity} {r.booking_date} **{r.amount} {r.currency}** · regla `{rule_of.get(tid)}`  \n"
                       f"  texto: `{r.text[:140]}`  \n  golden ({(g.bank_txn_id == tid).sum()}): {' '.join(gold)}  \n"
                       f"  nuestro: {' '.join(mine)}")
        out.append("")
    p = exp / f"{win}_errors.md"
    p.write_text("\n".join(out))
    print("\n".join(out[:6]))
    print(f"\n-> {p}")


def oracle(exp, win, top=25):
    from recon.engine import official_score
    from recon.load import load_categories
    data, d, g, t = load(exp, win)
    cats = load_categories(ROOT / "data" / "masters")
    F = ["bank_txn_id", "item_id", "amount", "category", "exception"]
    tmp = exp / f"_oracle_{win}.csv"

    def sc(rows):
        rows[F].to_csv(tmp, index=False)
        return official_score(tmp, g, t.index, cats)["score"]
    base = sc(d)
    seg = g.drop_duplicates("bank_txn_id").set_index("bank_txn_id").segment
    gold = g.assign(amount=g.amount.where(g.item_id != "", ""))
    res = []
    for s in sorted(seg.unique()):
        tx = set(seg[seg == s].index)
        res.append((round(sc(pd.concat([d[~d.bank_txn_id.isin(tx)], gold[gold.bank_txn_id.isin(tx)]])) - base, 2),
                    s, len(tx)))
    tmp.unlink(missing_ok=True)
    res.sort(reverse=True)
    print(f"[{win}] nota actual {base}. Puntos que vale arreglar cada segmento (techo):")
    for v, s, n in res[:top]:
        print(f"  {v:+6.2f}  {s:<30} {n:6d} movimientos")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["errors", "oracle"])
    ap.add_argument("exp", type=Path)
    ap.add_argument("--window", default="train_2506")
    a = ap.parse_args()
    (errors if a.cmd == "errors" else oracle)(a.exp, a.window)
