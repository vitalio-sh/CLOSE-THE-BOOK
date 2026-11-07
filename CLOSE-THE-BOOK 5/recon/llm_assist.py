"""Claude para los movimientos que las reglas no resuelven (opcional).

Sólo se llama sobre los UNIDENTIFIED que tienen candidatos, en paralelo y con tope
(config.LLM_MAX_CALLS). Requiere `pip install anthropic` y ANTHROPIC_API_KEY.
TODO(P3): añadir el texto de los PDFs del tercero (pdftotext) al prompt.
"""
import json
import math
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from . import config as C

EXC = ["", "PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING",
       "SHORT_DISPUTE", "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]

PROMPT = """Eres analista de tesorería. Concilia este movimiento bancario con sus partidas abiertas.
Movimiento: {id} · {date} · {amount} {ccy}
Concepto: {text}

Partidas abiertas candidatas (item_id | nº impreso | tercero | importe abierto | divisa | emisión | abono):
{cands}

Reglas: una fila por partida liquidada; amount = importe liquidado en la divisa de la partida;
descuento pronto pago/comisiones/retención dentro de política -> partida completa + excepción;
si nada encaja con seguridad, devuelve lista vacía. Excepciones válidas: {exc}.
Responde SÓLO JSON: {{"rows":[{{"item_id":"...","amount":0.00,"exception":""}}],"why":"..."}}"""


def _candidates(t, ctx, k=15):
    side = "AR" if t.amount > 0 else "AP"
    ids = []
    for p in ctx.parties_in(t):
        ids += ctx.open_items(p, side, t.date)
    ids.sort(key=lambda i: abs(ctx.open[i] - abs(t.amount)))
    return ids[:k]


def resolve(df: pd.DataFrame, pending, ctx) -> pd.DataFrame:
    import anthropic
    client = anthropic.Anthropic()
    jobs = [(t, c) for t in pending if (c := _candidates(t, ctx))][: C.LLM_MAX_CALLS]
    print(f"[llm] {len(jobs)} movimientos a Claude")

    def ask(job):
        """Nunca lanza: cualquier fallo (red, límite de uso, JSON raro) -> sin filas."""
        t, cands = job
        try:
            lines = "\n".join(
                f"{i} | {ctx.item[i]['number']} | {ctx.party_name.get(ctx.item[i]['party'], '')} | "
                f"{ctx.open[i]:.2f} | {ctx.item[i]['ccy']} | {str(ctx.item[i]['issue'])[:10]} | {ctx.item[i]['credit']}"
                for i in cands)
            msg = client.messages.create(
                model=C.LLM_MODEL, max_tokens=600,
                messages=[{"role": "user", "content": PROMPT.format(
                    id=t.id, date=str(t.date)[:10], amount=t.amount, ccy=t.ccy, text=t.text,
                    cands=lines, exc=", ".join(e or "(vacío)" for e in EXC))}])
            txt = msg.content[0].text
            data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            return t.id, _clean(data.get("rows") or [], cands)
        except Exception as e:
            print(f"[llm] {t.id}: {e!r}")
            return t.id, []

    try:
        with ThreadPoolExecutor(8) as ex:
            results = dict(ex.map(ask, jobs))
    except Exception as e:  # el resultado de las reglas siempre se conserva
        print(f"[llm] abortado, se mantienen las reglas: {e!r}")
        return df
    solved = {tid: rows for tid, rows in results.items() if rows}
    if not solved:
        return df
    new = [(tid, r["item_id"], f"{r['amount']:.2f}", "", r["exception"], "llm")
           for tid, rows in solved.items() for r in rows]
    df = df[~df["bank_txn_id"].isin(solved)]
    print(f"[llm] resueltos {len(solved)}")
    return pd.concat([df, pd.DataFrame(new, columns=df.columns)], ignore_index=True)


def _clean(rows, cands) -> list[dict]:
    """Filtra la respuesta: partidas candidatas, sin repetir, importe numérico > 0, excepción válida."""
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        iid, exc = r.get("item_id"), r.get("exception") or ""
        try:
            amt = round(float(r.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if iid in cands and iid not in seen and exc in EXC and exc != "UNIDENTIFIED" and math.isfinite(amt) and amt > 0:
            seen.add(iid)
            out.append({"item_id": iid, "amount": amt, "exception": exc})
    return out
