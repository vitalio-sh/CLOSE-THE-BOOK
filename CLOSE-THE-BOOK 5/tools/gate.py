"""Puerta de regresión: compara un experimento con eval/baseline.json (versionado en git).

    python tools/gate.py out/eval/mi-cambio            # PASA / FALLA + tabla de diferencias
    python tools/gate.py out/eval/mi-cambio --accept   # pasa a ser la nueva línea base

Reglas (exit 1 si falla):
  - todas las ventanas generan un fichero VÁLIDO para el validador oficial;
  - la media de TRAIN no baja (tolerancia MIN_GAIN) y ninguna ventana de TRAIN baja más de MAX_DROP;
  - el motor no se vuelve > MAX_SLOWDOWN más lento (FINAL tiene una hora).
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "eval" / "baseline.json"
MIN_GAIN = -0.05      # la media de TRAIN puede bajar como mucho esto (ruido de redondeo)
MAX_DROP = 0.30       # ninguna ventana de TRAIN puede bajar más que esto
MAX_SLOWDOWN = 1.30   # tiempo total TRAIN <= 1,3x la base


def tot_time(s):
    return sum(w.get("timing_s", {}).get("total", 0) for n, w in s["windows"].items() if n.startswith("train"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exp", type=Path)
    ap.add_argument("--accept", action="store_true")
    a = ap.parse_args()
    exp = json.loads((a.exp / "summary.json").read_text())
    if not BASE.exists():
        print("No hay línea base: usa --accept para fijarla.")
        if a.accept:
            BASE.parent.mkdir(exist_ok=True)
            shutil.copy(a.exp / "summary.json", BASE)
        return 0
    base = json.loads(BASE.read_text())
    fails, warns = [], []
    print(f"base {base['tag']} ({base['commit']})  vs  {exp['tag']} ({exp['commit']}{'+dirty' if exp['dirty'] else ''})\n")
    print(f"{'ventana':<12}{'base':>8}{'nuevo':>8}{'Δ':>8}   allocP   allocR   txnAcc   excF1")
    for n, w in exp["windows"].items():
        b = base["windows"].get(n, {})
        if not w.get("valid"):
            fails.append(f"{n}: fichero NO válido {w.get('errors', [])[:2]}")
            continue
        bs, ns = b.get("score"), w.get("score")
        d = (ns - bs) if bs is not None and ns is not None else None
        m, bm = w.get("metrics", {}), b.get("metrics", {})
        delta = lambda k: f"{m.get(k, 0) - bm.get(k, 0):+.3f}" if bm else "   -  "
        print(f"{n:<12}{bs if bs is not None else '-':>8}{ns:>8}{(f'{d:+.2f}' if d is not None else '-'):>8}   "
              f"{delta('precision')}   {delta('recall')}   {delta('txn_accuracy')}   {delta('exception_macro_f1')}")
        if n.startswith("train") and d is not None and d < -MAX_DROP:
            fails.append(f"{n}: baja {d:+.2f} (> {MAX_DROP})")
    bd, nd = base.get("decision_mean"), exp.get("decision_mean")
    if bd is not None and nd is not None:
        print(f"\nMEDIA TRAIN  {bd} -> {nd}  ({nd - bd:+.3f})")
        if nd - bd < MIN_GAIN:
            fails.append(f"media TRAIN baja {nd - bd:+.3f}")
    elif nd is None:
        warns.append("sin ventanas de TRAIN: no se puede decidir (¿--quick?)")
    bt, nt = tot_time(base), tot_time(exp)
    if bt and nt > MAX_SLOWDOWN * bt:
        fails.append(f"más lento: {nt}s vs {bt}s (> x{MAX_SLOWDOWN})")
    # detalle: excepciones, reglas y segmentos que más cambian (ventana TRAIN principal)
    for n in ("train_2506", "sample"):
        w, b = exp["windows"].get(n), base["windows"].get(n)
        if not (w and b and "exc_f1" in w and "exc_f1" in b):
            continue
        print(f"\n[{n}] F1 por excepción (Δ):")
        print("   " + "  ".join(f"{k} {w['exc_f1'].get(k, 0):.2f}({w['exc_f1'].get(k, 0) - b['exc_f1'].get(k, 0):+.2f})"
                               for k in sorted(set(w["exc_f1"]) | set(b["exc_f1"]))))
        print(f"[{n}] precisión por regla (filas, Δ precisión):")
        for r, x in sorted(w.get("rule_precision", {}).items(), key=lambda z: -z[1]["rows"]):
            y = b.get("rule_precision", {}).get(r, {"rows": 0, "precision": 0})
            print(f"   {r:<14}{x['rows']:>8} ({x['rows'] - y['rows']:+d})  {x['precision']:.3f} ({x['precision'] - y['precision']:+.3f})")
        seg = [(k, v["accuracy"] - b.get("segments", {}).get(k, {}).get("accuracy", 0), v["n"])
               for k, v in w.get("segments", {}).items()]
        seg = sorted([s for s in seg if abs(s[1]) >= 0.03], key=lambda s: -abs(s[1]) * s[2])[:10]
        if seg:
            print(f"[{n}] segmentos que más cambian (Δ accuracy, nº movimientos):")
            print("   " + "  ".join(f"{k} {d:+.2f} (n={c})" for k, d, c in seg))
        break
    print()
    for x in warns:
        print("AVISO:", x)
    if fails:
        print("FALLA:\n  - " + "\n  - ".join(fails))
        return 1
    print("PASA")
    if a.accept:
        shutil.copy(a.exp / "summary.json", BASE)
        print(f"nueva línea base: {exp['tag']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
