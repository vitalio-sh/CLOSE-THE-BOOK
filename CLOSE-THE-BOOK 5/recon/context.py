"""Estado compartido por las reglas: libro de partidas abiertas, índices y utilidades."""
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from . import config as C
from .common import norm, norm_doc


@dataclass
class Row:
    item_id: str = ""
    amount: float = 0.0
    category: str = ""
    exception: str = ""


class Context:
    def __init__(self, items: pd.DataFrame, parties: pd.DataFrame, remits: dict, fx: dict,
                 cat_model: dict | None = None, categories: list | None = None, terms: dict | None = None):
        self.fx = fx
        self.terms = terms or {}
        self.remits = remits
        self.cat_model = cat_model or {}
        self.categories = set(categories or [])
        # ---- partidas
        self.item = {}                       # item_id -> dict(fila)
        self.open = {}                       # item_id -> importe abierto
        self.by_doc = defaultdict(list)      # ndoc -> [item_id]
        self.by_party = defaultdict(list)    # (party, side) -> [item_id] ordenado por emisión (FIFO)
        self._oi_key, self._oi_cache = None, {}  # memo de open_items dentro de un movimiento
        self.by_party_all = defaultdict(list)  # sin poda (incluye liquidadas): devoluciones, duplicados
        self.by_amt = defaultdict(list)      # (party, side, céntimos abiertos) -> [item_id] FIFO
        self.by_total = defaultdict(list)    # (party, side, céntimos del total) -> [item_id] (incl. cerradas)
        self.fx_parties = set()              # terceros con partidas en divisa != la de su sociedad
        issue_o = [d.toordinal() if not pd.isna(d) else -1 for d in items["issue"]]
        for r, io in zip(items.itertuples(index=False), issue_o):
            d = r._asdict()
            d["issue_o"] = io
            self.item[r.id] = d
            self.open[r.id] = float(r.amount)
            if r.party and not r.credit:
                self.by_total[(r.party, r.side, round(float(r.total) * 100))].append(r.id)
            if r.party and float(r.amount) > 0.005:
                self.by_amt[(r.party, r.side, round(float(r.amount) * 100))].append(r.id)
                if r.ccy and r.ccy != "EUR":
                    self.fx_parties.add(r.party)
            if r.ndoc:
                self.by_doc[r.ndoc].append(r.id)
            if r.party:
                self.by_party[(r.party, r.side)].append(r.id)
                self.by_party_all[r.party].append(r.id)
        # prefijos reales de nº de documento (FV, AB, FA, AV, RE, GS...): sólo esos se aceptan como referencia
        from collections import Counter
        pc = Counter(m.group() for nd in items["ndoc"] if (m := re.match(r"[A-Z]{2,3}(?=\d{2})", nd)))
        self.doc_prefixes = {p for p, n in pc.items() if n >= 50}
        print(f"[ctx] prefijos de documento: {sorted(self.doc_prefixes)}")
        # ---- terceros: índice por primer token del nombre normalizado
        self.party_name = {}
        self.name_index = defaultdict(list)  # primer token -> [(clave, party)]
        self.tax_index = {}
        col = lambda c: parties[c] if c in parties else pd.Series("", index=parties.index)
        self.party_country = dict(zip(parties["id"], col("country")))
        self.party_entity = dict(zip(parties["id"], col("entity")))
        self.iban_index = {re.sub(r"\W", "", i).upper(): pid for pid, i in zip(parties["id"], col("iban")) if len(str(i)) > 12}
        self.full_names = defaultdict(set)   # party -> nombres normalizados (razón social y comercial)
        for r in parties.itertuples(index=False):
            nm = norm(r.name)
            self.party_name.setdefault(r.id, nm)
            self.full_names[r.id].add(nm)
            toks = [t for t in nm.split() if len(t) > 1]
            if not toks:
                continue
            key = " ".join(toks[:2]) if len(toks) > 1 or len(toks[0]) < 5 else toks[0]
            self.name_index[toks[0]].append((key, r.id))
            tax = re.sub(r"\D", "", str(r.tax))
            if len(tax) >= 7:
                self.tax_index[tax[-8:]] = r.id
        self.linked, self.plan, self.reserved = {}, {}, {}   # evidencia documental (setup_evidence)
        self.pdf_partial, self.replaced = {}, {}              # cláusulas de PDF (setup_pdf)
        self.reversals = set()                                # BANK_REVERSAL (ambas líneas)
        self.returns = None                                    # índices para devoluciones
        self.cur = None                                        # movimiento en curso (lo fija el motor)
        self.history = defaultdict(list)     # party -> [(fecha, item_id, importe)] para devoluciones
        print(f"[ctx] {len(self.item):,} partidas, {len(self.party_name):,} terceros")

    # ------------------------------------------------------------ terceros
    def parties_in(self, t) -> list:
        key = getattr(t, "id", None)
        if key is not None and key == getattr(self, "_pc_key", None):
            return self._pc_val
        out = self._parties_in(t)
        if key is not None:
            self._pc_key, self._pc_val = key, out
        return out

    _IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b")

    def _parties_in(self, t) -> list:
        """Terceros citados, de MÁS a MENOS probable. Puntuación = cuánto del nombre completo coincide con
        el texto (prefijo común: soporta nombres cortados por el banco), +IBAN/NIF exactos, y prioridad a los
        terceros de la sociedad de la cuenta (un cobro en AU01 viene de un cliente de AU01; ES00 paga
        facturas de ES01/ES02/PT01). Antes: orden arbitrario y 'POOL SUPPLY' casaba decenas de clientes."""
        txt = t.ntext
        score = {}
        for pos_tok in re.finditer(r"\S+", txt):
            tok = pos_tok.group()
            for key, pid in self.name_index.get(tok, ()):
                best = 0
                for nm in self.full_names.get(pid, ()):
                    seg = txt[pos_tok.start(): pos_tok.start() + len(nm)]
                    n = 0
                    for x, y in zip(seg, nm):
                        if x != y:
                            break
                        n += 1
                    best = max(best, n)
                full = any(len(nm) <= best for nm in self.full_names.get(pid, ()))
                if best >= C.NAME_MIN_PREFIX or full or key in txt:
                    score[pid] = max(score.get(pid, 0), best + (5 if full else 0))
        for num in re.findall(r"\d{8}", txt):
            pid = self.tax_index.get(num)
            if pid:
                score[pid] = max(score.get(pid, 0), 100)
        raw = (getattr(t, "text", "") or "").upper()
        for m in self._IBAN.finditer(raw):
            pid = self.iban_index.get(re.sub(r"\W", "", m.group()))
            if pid:
                score[pid] = max(score.get(pid, 0), 100)
        if not score:
            return []
        ent = getattr(t, "entity", "")
        if ent:
            ok = {ent} | (C.PAYMENT_FACTORY.get(ent, set()) if getattr(t, "amount", 0) < 0 else set())
            same = {p for p in score if self.party_entity.get(p, "") in ok}
            if same:
                score = {p: v for p, v in score.items() if p in same}
        return sorted(score, key=lambda p: -score[p])

    # ------------------------------------------------------------ referencias
    # nº impreso: 2-3 letras + año (2 dígitos) + correlativo. FV/AB (ES), FA/AV (FR), RE/GS (DE)...
    _REF = re.compile(r"\b([A-Z]{2,3})\s?(\d{2})\s?[-/]?\s?(\d{3,7})\b")
    _CONT = re.compile(r"(?<![A-Z0-9-])(\d{6,7})\b")

    def refs_in(self, t) -> list:
        """Números impresos citados en el concepto. Devuelve [(ndoc, truncado?)]."""
        txt = t.text.upper()
        refs, last = [], None
        for m in self._REF.finditer(txt):
            pre, yy, num = m.groups()
            if pre not in self.doc_prefixes:   # 'FRA 05...', 'VTO 04...', 'INV 11...' no son nº nuestros
                continue
            refs.append((pre + yy + num, len(num) < 7))
            last = (pre, yy, num)
        if last:  # 'FV22-0519411 0516064' / 'RE250106232 106234': hereda prefijo y año
            for num in self._CONT.findall(txt):
                full = last[2][: max(0, 7 - len(num))] + num if len(num) < 7 else num
                cand = last[0] + last[1] + full
                if all(cand != r[0] for r in refs):
                    refs.append((cand, False))
        return refs

    def items_for_refs(self, refs, parties, side, date, t=None) -> list:
        """Una partida por nº citado. El mismo nº impreso existe en varias sociedades: se prefiere la
        sociedad de la cuenta, luego el tercero identificado, luego el importe más cercano."""
        out = []
        ent = getattr(t, "entity", "") if t is not None else ""
        paid = abs(t.amount) if t is not None else None
        for ndoc, trunc in refs:
            ndoc = self.replaced.get(ndoc, ndoc)   # "anula y sustituye": se cobra la nueva
            ids = list(self.by_doc.get(ndoc, ()))
            if not ids and trunc and parties:  # referencia cortada: buscar por prefijo dentro del tercero
                ids = [i for p in parties for i in self.open_items(p, side, date)
                       if self.item[i]["ndoc"].startswith(ndoc)]
                if len(ids) != 1:
                    ids = []
            ids = [i for i in ids if self.is_open(i) and self.item[i]["side"] == side and self.free(i)
                   and i not in out]
            if len(ids) > 1 and ent and side == "AR":
                ids = [i for i in ids if self.item[i]["entity"] in ("", ent)] or ids
            if len(ids) > 1 and parties:
                ids = [i for i in ids if self.item[i]["party"] in parties] or ids
            if len(ids) > 1 and paid is not None:
                ids.sort(key=lambda i: abs(self.open[i] - paid))
            if ids:
                out.append(ids[0])
        return out

    # ------------------------------------------------------------ evidencia documental
    def setup_evidence(self, txns, linked):
        from .evidence import DocIndex
        from .rules.evidence_rule import plan
        self.docs = DocIndex(self)
        self.linked = linked
        by_id = {t.id: t for t in txns.itertuples(index=False) if t.id in linked}
        for tid, g in linked.items():
            pl = plan(by_id[tid], g, self)
            if pl:
                self.plan[tid] = pl
                for iid, _ in pl:
                    self.reserved.setdefault(iid, tid)
        n = sum(len(v) for v in self.plan.values())
        print(f"[evidence] {len(self.plan):,} movimientos con plan, {n:,} partidas reservadas")

    def withholds(self, party) -> bool:
        return self.party_country.get(party, "") in C.WITHHOLDING_COUNTRIES

    def setup_pdf(self, partial: dict, replaces: dict):
        """Cláusulas de PDF: cobros parciales previstos por partida y nº sustituidos."""
        self.pdf_partial = {}
        for nd, cents in partial.items():
            for i in self.by_doc.get(nd, ()):
                self.pdf_partial[i] = cents
        self.replaced = replaces

    def free(self, iid) -> bool:
        """La partida no está reservada por el documento de OTRO movimiento."""
        r = self.reserved.get(iid)
        return r is None or r == self.cur

    # ------------------------------------------------------------ libro
    def is_open(self, iid) -> bool:
        return self.open.get(iid, 0) > 0.005

    def open_items(self, party, side, date) -> list:
        """Partidas abiertas del tercero (FIFO). Memo por movimiento: el libro no cambia hasta apply()."""
        if self._oi_key != (self.cur, date):
            self._oi_key, self._oi_cache = (self.cur, date), {}
        k = (party, side)
        hit = self._oi_cache.get(k)
        if hit is not None:
            return list(hit)
        lst = self.by_party.get(k)
        if not lst:
            return []
        op = self.open
        alive = [i for i in lst if op[i] > 0.005]
        if len(alive) < len(lst):
            self.by_party[k] = alive  # poda perezosa
        lim = date.toordinal() + C.ISSUE_SLACK_DAYS
        it, res, cur = self.item, self.reserved, self.cur
        out = [i for i in alive if it[i]["issue_o"] <= lim and res.get(i, cur) == cur]
        self._oi_cache[k] = out
        return list(out)

    def signed_open(self, iid) -> float:
        return -self.open[iid] if self.item[iid]["credit"] else self.open[iid]

    def rate(self, ccy, day) -> float | None:
        """Unidades de `ccy` por 1 EUR el día `day`; fines de semana y festivos: último tipo disponible."""
        if ccy == "EUR":
            return 1.0
        ser = self.fx.get(ccy)
        if not ser:
            return None
        k = bisect_right(ser[0], day)
        return ser[1][k - 1] if k else ser[1][0]

    def to_txn_ccy(self, iid, amount, t) -> float:
        ic = self.item[iid]["ccy"] or t.ccy
        if not ic or not t.ccy or ic == t.ccy:
            return amount
        day = str(t.date)[:10]
        ri, rt = self.rate(ic, day), self.rate(t.ccy, day)
        if not ri or not rt:
            return amount
        return amount / ri * rt

    def apply(self, t, rows, party=None):
        self._oi_key = None  # el libro cambia: invalidar memo
        for r in rows:
            if not r.item_id:
                continue
            if r.exception == "RETURNED":
                self.open[r.item_id] = self.open.get(r.item_id, 0) + r.amount
                continue
            full = r.exception.startswith("SHORT_") or r.exception in ("FX_DIFFERENCE", "OVERPAYMENT", "")
            self.open[r.item_id] = 0.0 if full else max(0.0, self.open[r.item_id] - r.amount)
            p = self.item[r.item_id]["party"]
            self.history[p].append((t.date, r.item_id, r.amount))
