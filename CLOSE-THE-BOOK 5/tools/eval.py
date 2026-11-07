"""Evalúa el motor en varias ventanas con el scorer OFICIAL y guarda un resumen comparable.

    python tools/eval.py --tag mi-cambio            # completo: sample + TRAIN 2025-06 + TRAIN 2025-03 (~15 min)
    python tools/eval.py --tag mi-cambio --quick    # sólo sample (~1 min): para iterar, NUNCA para decidir
    python tools/gate.py out/eval/mi-cambio         # ¿mejora frente a eval/baseline.json?

Por qué varias ventanas: el sample (1 mes, 66k partidas) engaña. Dos cambios que subían +3,7 en el sample
bajaban en TRAIN (700k partidas abiertas). La decisión se toma con la media de TRAIN.
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = {
    "sample": ["--data", "data/sample"],
    "train_2506": ["--data", "data/train", "--from", "2025-06-01", "--to", "2025-06-30"],
    "train_2503": ["--data", "data/train", "--from", "2025-03-01", "--to", "2025-03-31"],
}
DECISION = ["train_2506", "train_2503"]   # ventanas que deciden (media)


def run(tag, name, args):
    out = ROOT / "out" / "eval" / tag
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "run.py", *args, "--masters", "data/masters", "--eval",
           "--out", str(out / f"{name}.csv"), "--json", str(out / f"{name}.json")]
    t = time.time()
    with open(out / f"{name}.log", "w") as log:
        rc = subprocess.call(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    js = out / f"{name}.json"
    res = json.loads(js.read_text()) if js.exists() else {"valid": False, "errors": [f"rc={rc}, ver {name}.log"]}
    print(f"  {name:<12} score={res.get('score', 'ERROR')!s:<7} {time.time() - t:5.0f}s  rc={rc}", flush=True)
    return name, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="nombre del experimento (carpeta en out/eval/)")
    ap.add_argument("--quick", action="store_true", help="sólo sample")
    ap.add_argument("--windows", nargs="*", help=f"subconjunto de {list(WINDOWS)}")
    ap.add_argument("--jobs", type=int, default=2, help="ventanas en paralelo (TRAIN usa ~5 GB cada una)")
    a = ap.parse_args()
    names = ["sample"] if a.quick else (a.windows or list(WINDOWS))
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "recon", "run.py"], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip())
    print(f"[eval] {a.tag}: {names} (commit {commit}{' + cambios sin commit' if dirty else ''})")
    with ThreadPoolExecutor(a.jobs) as ex:
        res = dict(ex.map(lambda n: run(a.tag, n, WINDOWS[n]), names))
    dec = [res[n]["score"] for n in DECISION if n in res and "score" in res[n]]
    summary = {"tag": a.tag, "commit": commit, "dirty": dirty, "windows": res,
               "decision_mean": round(sum(dec) / len(dec), 3) if len(dec) == len([n for n in DECISION if n in res]) and dec else None}
    p = ROOT / "out" / "eval" / a.tag / "summary.json"
    p.write_text(json.dumps(summary, indent=1))
    print(f"[eval] media TRAIN: {summary['decision_mean']}  ->  {p}")


if __name__ == "__main__":
    main()
