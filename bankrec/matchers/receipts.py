"""AR receipts (bank credits) -> customer invoices / credit notes.

Steps per credit line:
  1. payer -> candidate customers: IBAN (counterparty_account / IBAN in text), then name tokens (inverted index);
     confirming / factoring "ORD. <customer>" handled by the name step; parent pays for its group (group_id).
  2. references in the text resolved against the candidates' open items (exact, no prefix, truncated, 1 typo).
  3. amount: referenced items (+ netted credit notes) explain the cash -> rows; single-item shortfalls ->
     SHORT_DISCOUNT / SHORT_WITHHOLDING / SHORT_BANK_CHARGES / PARTIAL; excess -> OVERPAYMENT; already settled ->
     DUPLICATE_PAYMENT; no reference -> exact subset of open items (FIFO first).
  4. FX: account currency != item currency -> compare via fx_rates with 3 % tolerance, FX_DIFFERENCE.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import defaultdict
from datetime import date, timedelta

import polars as pl

from ..ctx import bank_text, item_info
from ..refs import keys_for, norm, tokens

# bank-text noise (payment wording), not part of customer names. Legal forms (SPA, GMBH, COOP...) are kept: they
# are part of names (e.g. "Spa Delta S.A.") and IDF weighting makes them cheap.
STOP = {
    "TRANSFERENCIA",
    "SEPA",
    "PAYMENT",
    "PAGO",
    "FACTURA",
    "FRA",
    "FRAS",
    "ORD",
    "RECHNUNG",
    "VIREMENT",
    "ABONO",
    "CREDIT",
    "ACH",
    "CCD",
    "TRANSFER",
    "INVOICE",
    "INV",
    "CONCEPTO",
    "ORDENANTE",
    "TRANSF",
    "EREF",
    "SVWZ",
    "NOTPROVIDED",
    "NONREF",
    "REMI",
    "PENDIENTE",
    "MATERIAL",
    "INMEDIATA",
    "ECAGGEXX",
}
FX_TOL = 0.03
FUZZY_MAX = 3000  # fuzzy (prefix / typo) reference scan only over candidate sets up to this size
OVERPAY_STRICT = False
# POLICIES §3 OVERPAYMENT is an excess over ONE invoice; cash far above the item (March: every true case <= 1.33x,
# 233 false ones > 1.5x) is a multi-invoice payment with a stray reference -> leave it to ar_multi / fallback
OVERPAY_MAX_RATIO = 1.5
WITHHOLD = (0.0125, 0.015, 0.10)
SEPA = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
    "GB",
    "CH",
    "NO",
    "IS",
    "LI",
    "MC",
    "SM",
    "AD",
    "VA",
}
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")


_ACC = (
    ("Á", "A"),
    ("À", "A"),
    ("É", "E"),
    ("È", "E"),
    ("Ê", "E"),
    ("Í", "I"),
    ("Ó", "O"),
    ("Ö", "O"),
    ("Ú", "U"),
    ("Ü", "U"),
    ("Ñ", "N"),
    ("Ç", "C"),
    ("ß", "SS"),
    ("Ä", "A"),
    ("Ł", "L"),
    ("Ś", "S"),
    ("Ż", "Z"),
    ("Ę", "E"),
    ("Ą", "A"),
    ("Ć", "C"),
    ("Ã", "A"),
    ("Õ", "O"),
    ("Â", "A"),
)
_NN = re.compile(r"[^A-Z0-9]+")
NAME_MIN = 12  # minimum matched characters for a (possibly truncated) full-name match


def _nname(s) -> str:
    s = (s or "").upper()
    for a, b in _ACC:
        s = s.replace(a, b)
    return _NN.sub(" ", s).strip()


def _words(s) -> list:
    s = (s or "").upper()
    for a, b in (
        ("Á", "A"),
        ("À", "A"),
        ("É", "E"),
        ("È", "E"),
        ("Ê", "E"),
        ("Í", "I"),
        ("Ó", "O"),
        ("Ö", "O"),
        ("Ú", "U"),
        ("Ü", "U"),
        ("Ñ", "N"),
        ("Ç", "C"),
        ("ß", "SS"),
        ("Ä", "A"),
        ("Ł", "L"),
        ("Ś", "S"),
        ("Ż", "Z"),
        ("Ę", "E"),
        ("Ą", "A"),
        ("Ć", "C"),
        ("Ó", "O"),
        ("Ã", "A"),
        ("Õ", "O"),
        ("Â", "A"),
    ):
        s = s.replace(a, b)
    return [w for w in re.findall(r"[A-Z]{3,}", s) if w not in STOP]


class Ctx:
    def __init__(self, ph):
        self.ph = ph
        cust = ph.masters["customers"]
        self.cust = {r["customer_id"]: r for r in cust.iter_rows(named=True)}
        self.by_iban = defaultdict(set)
        for c in self.cust.values():
            if c["iban"]:
                self.by_iban[norm(c["iban"])].add(c["customer_id"])
        self.group = defaultdict(set)
        for c in self.cust.values():
            if c["group_id"]:
                self.group[c["group_id"]].add(c["customer_id"])
        info = item_info(ph)
        self.info = info
        # items per party (AR side)
        self.by_party = defaultdict(list)
        for iid, r in info.items():
            if r["item_type"] in ("ARI", "ARC"):
                self.by_party[r["party_id"]].append(iid)
        for p in self.by_party:
            self.by_party[p].sort(
                key=lambda i: (
                    info[i]["issue_date"] or "",
                    info[i]["document_number"] or "",
                )
            )
        # per-party lists pruned of settled items as receipts runs (open amounts only decrease in this matcher)
        self.live = {p: list(v) for p, v in self.by_party.items()}
        # reference keys of every AR item (document number variants + PO), and its full normalised number
        self.keymap = defaultdict(set)
        self.full = {}
        for p, iids in self.by_party.items():
            for iid in iids:
                dn = info[iid]["document_number"]
                for k in keys_for(dn):
                    self.keymap[k].add(iid)
                if info[iid].get("po"):
                    self.keymap[norm(info[iid]["po"])].add(iid)
                self.full[iid] = norm(dn)
        # name inverted index per entity, names from master + invoices (renamed customers keep old names on invoices)
        names = defaultdict(set)
        for c in self.cust.values():
            names[c["customer_id"]].add(c["name"])
            if c["trade_name"]:
                names[c["customer_id"]].add(c["trade_name"])
        for iid, r in info.items():
            if r["item_type"] in ("ARI", "ARC") and r["party_name"]:
                names[r["party_id"]].add(r["party_name"])
        self.inv = defaultdict(set)
        self.nwords = {}
        for cid, ns in names.items():
            ent = self.cust.get(cid, {}).get("entity") or cid[1:5]
            ws = set()
            for n in ns:
                ws.update(_words(n))
            self.nwords[cid] = ws
            for w in ws:
                self.inv[(ent, w)].add(cid)
        self.df = {k: len(v) for k, v in self.inv.items()}
        # per entity: sorted words (prefix lookup for truncated names) and name variants as word lists
        self.sorted_words = defaultdict(list)
        for ent, w in self.inv:
            self.sorted_words[ent].append(w)
        for ent in self.sorted_words:
            self.sorted_words[ent].sort()
        self._wcache = {}
        self._tot = {}
        self._rank_cache = {}
        self.variants = defaultdict(list)
        for cid, ns in names.items():
            for n in ns:
                ws = set(_words(n))
                if ws:
                    self.variants[cid].append(ws)
        # per entity: sorted normalised full names -> customers (bank texts carry the name, often truncated)
        nm = defaultdict(lambda: defaultdict(set))
        for cid, ns in names.items():
            ent = self.cust.get(cid, {}).get("entity") or cid[1:5]
            for n in ns:
                k = _nname(n)
                if len(k) >= NAME_MIN:
                    nm[ent][k].add(cid)
        self.name_keys = {e: sorted(v) for e, v in nm.items()}
        self.name_ids = {e: dict(v) for e, v in nm.items()}
        self._full_cache = {}
        # fx
        fx = ph.masters["fx_rates"]
        self.fx = defaultdict(list)
        for d, c, u in fx.select("date", "currency", "units_per_eur").iter_rows():
            self.fx[c].append((d, float(u)))
        for c in self.fx:
            self.fx[c].sort()
        terms = ph.masters["payment_terms"]
        self.terms = {r["code"]: r for r in terms.iter_rows(named=True)}
        acc = ph.masters["bank_accounts"]
        self.acc_ent = dict(zip(acc["account_id"].to_list(), acc["entity"].to_list()))

    def rate(self, cur: str, d: str) -> float:
        if cur == "EUR":
            return 1.0
        arr = self.fx.get(cur)
        if not arr:
            return 0.0
        i = bisect.bisect_right(arr, (d, 1e18)) - 1
        return arr[max(i, 0)][1]

    def convert(self, amount: float, frm: str, to: str, d: str) -> float:
        """amount in currency frm -> currency to (reference rate)."""
        if frm == to:
            return amount
        rf, rt = self.rate(frm, d), self.rate(to, d)
        if not rf or not rt:
            return 0.0
        return amount / rf * rt

    def payers(self, row: dict, ent: str) -> list:
        """Candidate customer ids, best first."""
        txt = bank_text(row)
        out = []
        ibans = set()
        if row.get("counterparty_account"):
            ibans.add(norm(row["counterparty_account"]))
        for m in _IBAN.finditer(txt.upper().replace(" ", "")):
            ibans.add(m.group(1))
        for ib in ibans:
            for cid in self.by_iban.get(ib, ()):
                if cid not in out:
                    out.append(cid)
        self.last_score = {c: 2.0 for c in out}
        exact, trunc = self._full_names(row, ent)
        for cs, sc in ((exact, 1.6), (trunc, 1.5)):
            for cid in sorted(cs):
                if cid not in out:
                    out.append(cid)
                    self.last_score[cid] = sc
        for cid, sc in self._name_rank(txt, ent)[:8]:
            if cid not in out:
                out.append(cid)
                self.last_score[cid] = sc
        return out

    def _full_names(self, row: dict, ent: str):
        """Customers whose full normalised name starts at a word of the text (exact), or whose name the text
        quotes truncated (>= NAME_MIN chars, cut at a word/field boundary). Returns (exact set, truncated set)."""
        keys = self.name_keys.get(ent)
        if not keys:
            return set(), set()
        s = " | ".join(
            _nname(row.get(c))
            for c in ("text", "counterparty_name", "remittance_info")
            if row.get(c)
        )
        ck = (ent, s)
        hit = self._full_cache.get(ck)
        if hit is not None:
            return hit
        ids = self.name_ids[ent]
        exact, trunc = set(), set()
        starts = [0] + [m.end() for m in re.finditer(r"[ |]+", s)]
        for p in starts:
            q = s[p : p + 80]
            if len(q) < NAME_MIN:
                continue
            i = bisect.bisect_left(keys, q)
            best = 0
            for j in (i - 1, i):
                if 0 <= j < len(keys):
                    k = keys[j]
                    n = 0
                    m = min(len(k), len(q))
                    while n < m and k[n] == q[n]:
                        n += 1
                    best = max(best, n)
            if best < NAME_MIN:
                continue
            pre = q[:best]
            if best < len(q) and q[best] not in " |":
                if pre[-1] != " ":
                    continue
                pre = (
                    pre.rstrip()
                )  # cut at a word boundary, text goes on with other words
            if pre in ids:
                exact |= ids[pre]
                continue
            lo = bisect.bisect_left(keys, pre)
            hi = bisect.bisect_left(keys, pre + "\uffff")
            if 0 < hi - lo <= 12:
                for k in keys[lo:hi]:
                    trunc |= ids[k]
        trunc -= exact
        res = self._full_cache[ck] = (exact, trunc)
        return res

    def _name_rank(self, txt: str, ent: str) -> list:
        """Customers whose name (a variant) is covered by the words of the bank text.

        A word of the text matches a name word when equal, or when the text word is a truncated prefix of it
        (len >= 5). Coverage is IDF-weighted over the name words; a name is accepted with >= 60 % coverage and
        either two covered words or one rare covered word (df <= 3). Returns [(cid, score)] best first.
        """
        ncust = max(1, len(self.cust))
        arr = self.sorted_words.get(ent, [])
        words = frozenset(_words(txt))
        ck = (ent, words)
        hitc = self._rank_cache.get(ck)
        if hitc is not None:
            return hitc
        res = self._rank_cache[ck] = self._rank_words(words, ent, ncust, arr)
        return res

    def _rank_words(self, words, ent, ncust, arr) -> list:
        matched = set()
        for t in words:
            if (ent, t) in self.inv:
                matched.add(t)
            if len(t) >= 5 and arr:
                i = bisect.bisect_left(arr, t)
                hit = []
                while i < len(arr) and arr[i].startswith(t):
                    hit.append(arr[i])
                    i += 1
                    if len(hit) > 30:
                        break
                if len(hit) <= 30:
                    matched.update(hit)
        if not matched:
            return []
        # retrieve candidates only through reasonably rare words (common words such as legal forms still count
        # for coverage); keeps the scoring loop small on large customer masters
        cand = set()
        for lim in (400, 150, 40):
            cand = set()
            for w in matched:
                cs = self.inv.get((ent, w), ())
                if len(cs) <= lim:
                    cand |= cs
            if len(cand) <= 500:
                break
        if not cand or len(cand) > 800:
            return []

        wcache = self._wcache.setdefault(ent, {})

        def wt(w):
            v = wcache.get(w)
            if v is None:
                v = wcache[w] = math.log(
                    1.0 + ncust / max(1, len(self.inv.get((ent, w), ())))
                )
            return v

        out = []
        for cid in cand:
            best = None
            for k, v in enumerate(self.variants.get(cid, ())):
                tk = (ent, cid, k)
                tot = self._tot.get(tk)
                if tot is None:
                    tot = self._tot[tk] = sum(wt(w) for w in v)
                cov = [w for w in v if w in matched and (ent, w) in self.inv]
                if not cov or tot <= 0:
                    continue
                frac = sum(wt(w) for w in cov) / tot
                rare = min(len(self.inv[(ent, w)]) for w in cov)
                if frac < 0.6 or (len(cov) < 2 and rare > 3):
                    continue
                key = (round(frac, 4), sum(wt(w) for w in cov))
                if best is None or key > best:
                    best = key
            if best is not None:
                out.append((cid, best))
        out.sort(key=lambda kv: (-kv[1][0], -kv[1][1]))
        return [(cid, k[0]) for cid, k in out]


def _open_items(cx, st, cids, types=("ARI", "ARC")):
    out = []
    for cid in cids:
        lv = cx.live.get(cid)
        if not lv:
            continue
        lv = [i for i in lv if st.open.get(i, 0) > 0.005]
        cx.live[cid] = lv
        out.extend(i for i in lv if cx.info[i]["item_type"] in types)
    return out


def _subset(values: list, target: int, max_n: int = 28, cap: int = 8000):
    """Indices of a subset of `values` (cents, may be negative) summing to target. FIFO prefix first, then DP."""
    acc = 0
    for i, v in enumerate(values):
        acc += v
        if acc == target:
            return list(range(i + 1))
    vals = values[:max_n]
    pos = sum(v for v in vals if v > 0)
    neg = sum(v for v in vals if v < 0)
    if not (neg <= target <= pos):
        return None
    reach = {0: ()}
    for i, v in enumerate(vals):
        new = {}
        for s, idx in reach.items():
            ns = s + v
            if ns not in reach and ns not in new and (v < 0 or ns <= target - neg):
                new[ns] = idx + (i,)
                if ns == target:
                    return list(new[ns])
        reach.update(new)
        if len(reach) > cap:
            break
    return None


def _ref_items(cx, row, cands):
    """Candidate items referenced in the text (exact variant keys, truncated prefix, or one typo)."""
    toks = [t for t in tokens(bank_text(row)) if len(t) >= 5]
    if not toks or not cands:
        return []
    cset = set(cands)
    full = None
    out = []
    for t in toks:
        hit = cx.keymap.get(t, set()) & cset
        if not hit:
            if len(cands) > FUZZY_MAX:
                continue
            if full is None:
                full = {i: cx.full[i] for i in cands}
            # truncated (prefix of the full number) or one-character typo / transposition
            hit = {
                i
                for i, f in full.items()
                if (len(t) >= 7 and f.startswith(t))
                or (len(t) == len(f) and _near(t, f))
            }
            if len(hit) > 1:
                hit = set()
        for i in hit or ():
            if i not in out:
                out.append(i)
    return out


def _near(a: str, b: str) -> bool:
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return (
        len(diff) == 2
        and diff[1] == diff[0] + 1
        and a[diff[0]] == b[diff[1]]
        and a[diff[1]] == b[diff[0]]
    )


_INST = re.compile(
    r"(?:RATE|PLAZO|CUOTA|VTO|VENC\w*|INSTALMENT|INSTALLMENT)\s*\.?\s*(\d{1,2})\s*/\s*(\d{1,2})"
)


def _last_instalment(txt: str) -> bool:
    """'RATE 3/3', 'PLAZO 2/2': the bank text names instalment k of n; k == n is the last one."""
    for m in _INST.finditer((txt or "").upper()):
        k, n = int(m.group(1)), int(m.group(2))
        if n >= 2 and k == n:
            return True
    return False


_MONTHS = {
    "ENERO": 1,
    "JANUARY": 1,
    "JAN": 1,
    "FEBRERO": 2,
    "FEBRUARY": 2,
    "FEB": 2,
    "MARZO": 3,
    "MARCH": 3,
    "MAR": 3,
    "ABRIL": 4,
    "APRIL": 4,
    "APR": 4,
    "MAYO": 5,
    "MAY": 5,
    "JUNIO": 6,
    "JUNE": 6,
    "JUN": 6,
    "JULIO": 7,
    "JULY": 7,
    "JUL": 7,
    "AGOSTO": 8,
    "AUGUST": 8,
    "AUG": 8,
    "SEPTIEMBRE": 9,
    "SEPTEMBER": 9,
    "SEP": 9,
    "SEPT": 9,
    "OCTUBRE": 10,
    "OCTOBER": 10,
    "OCT": 10,
    "NOVIEMBRE": 11,
    "NOVEMBER": 11,
    "NOV": 11,
    "DICIEMBRE": 12,
    "DECEMBER": 12,
    "DEZ": 12,
    "DIC": 12,
    "DEC": 12,
}
_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?\s*(\d{2,4})?"
)


def _service_month(txt: str):
    """(month, year or None) named in the bank text, e.g. 'SERVICIOS MAYO 2025', 'MANT. MAYO 25'.

    Full month names are taken with or without year; 3-letter abbreviations only with a year ('MAR' is a company
    word otherwise). Exactly one month must be named."""
    up = (txt or "").upper()
    found = []
    for m in _MONTH_RE.finditer(up):
        word = m.group(1)
        y = m.group(2)
        if len(word) <= 3 and not y:
            continue
        if y:
            y = int(y) + (2000 if len(y) == 2 else 0)
        found.append((_MONTHS[word], y))
    return found[0] if len(found) == 1 else None


def _service_match(cx, iid, sm) -> bool:
    """POLICIES §5.6: a monthly fee is invoiced at the beginning of the month after the service month."""
    issue = cx.info[iid]["issue_date"] or ""
    if len(issue) < 7:
        return False
    y, m = int(issue[:4]), int(issue[5:7])
    mon, yr = sm
    if yr is None:
        return m == mon % 12 + 1
    nxt = (yr + 1, 1) if mon == 12 else (yr, mon + 1)
    return (y, m) == nxt


def _val(cx, st, iid, cur_acc, d):
    """Open amount of item converted to account currency (signed: credit notes negative)."""
    r = cx.info[iid]
    v = st.open.get(iid, 0.0)
    if r["currency"] != cur_acc:
        v = cx.convert(v, r["currency"], cur_acc, d)
    return -v if r["item_type"] in ("ARC", "APC") else v


def _single_exception(cx, st, iid, cash_item_cur, row, cust):
    """Cash (in item currency) less than / more than the open amount of one item -> (amount, exception)."""
    r = cx.info[iid]
    op = st.open.get(iid, 0.0)
    total = r["total_amount"] or op
    diff = op - cash_item_cur
    tol = max(0.01, 0.005 * op)
    if abs(diff) <= 0.01:
        return op, ""
    if diff < 0:
        return op, "OVERPAYMENT"
    t = cx.terms.get(r["payment_terms"] or "")
    if t and t["early_payment_discount_pct"]:
        pct = float(t["early_payment_discount_pct"])
        disc_cash = round(total - round(total * pct / 100, 2), 2)
        if abs(disc_cash - cash_item_cur) <= 0.011 and abs(op - total) < 0.01:
            limit = date.fromisoformat(r["issue_date"]) + timedelta(
                days=int(t["discount_days"] or 0) + 3
            )
            if date.fromisoformat(row["booking_date"]) <= limit:
                return op, "SHORT_DISCOUNT"
            return cash_item_cur, "PARTIAL"
    country = (cust or {}).get("country") or ""
    if country in ("MX", "BR", "MA"):
        for w in WITHHOLD:
            if abs(round(total * (1 - w), 2) - cash_item_cur) <= 0.02:
                return op, "SHORT_WITHHOLDING"
    if country and country not in SEPA:
        short_eur = (
            cx.convert(diff, r["currency"], "EUR", row["booking_date"])
            if r["currency"] != "EUR"
            else diff
        )
        if short_eur <= 60 and diff <= 0.02 * op:
            return op, "SHORT_BANK_CHARGES"
    return cash_item_cur, "PARTIAL"


def _assign_set(cx, st, t, row, items, fx: bool):
    allocs = []
    for iid in items:
        exc = ""
        if fx and cx.info[iid]["item_type"] == "ARI":
            exc = "FX_DIFFERENCE"
        allocs.append((iid, st.open.get(iid, 0.0), exc))
    return st.assign(t, allocs)


def match(ph, st):
    cx = Ctx(ph)
    ph.extra["_receipts_ctx"] = cx
    # same-day receipts: the golden gives the oldest invoice to the highest bank_txn_id (observed on sample)
    rows = [
        r
        for r in ph.bank.filter(pl.col("amount") > 0)
        .sort(["booking_date", "bank_txn_id"], descending=[False, True])
        .to_dicts()
    ]
    for r in rows:
        t = r["bank_txn_id"]
        if st.claimed(t):
            continue
        ent = r["entity"]
        cur = r["currency"]
        d = r["booking_date"]
        amt = r["amount"]
        txt = bank_text(r)
        cids = cx.payers(r, ent)
        if not cids:
            continue
        # payer tiers: best-scoring name matches (ties included, IBAN first) are primary, the rest secondary.
        sc = cx.last_score
        top = max(sc.get(c, 0.0) for c in cids)
        primary = [c for c in cids if sc.get(c, 0.0) >= top - 0.02]
        secondary = [c for c in cids if c not in primary]
        # a parent may pay for its group: add the group of the best payer
        grp = cx.cust.get(primary[0], {}).get("group_id")
        group = [c for c in cx.group[grp] if c not in cids] if grp else []
        fam_p = list(primary) + group
        fam_a = fam_p + secondary
        cands = [
            i
            for i in _open_items(cx, st, fam_a)
            if (cx.info[i]["issue_date"] or "") <= d
        ]
        fam_p_set = set(fam_p)
        cands_p = [i for i in cands if cx.info[i]["party_id"] in fam_p_set]
        if not cands:
            continue
        fx = any(cx.info[i]["currency"] != cur for i in cands)
        refs = _ref_items(cx, r, cands)
        target = round(amt * 100)

        memo = {}

        def cents(iid):
            v = memo.get(iid)
            if v is None:
                v = memo[iid] = round(_val(cx, st, iid, cur, d) * 100)
            return v

        def close(a, b):
            return abs(a - b) <= max(1, (FX_TOL if fx else 0.0) * abs(b))

        # 1. referenced items explain the cash (plus open credit notes, or other open items of the payer)
        if refs:
            s = sum(cents(i) for i in refs)
            if close(s, target):
                _assign_set(
                    cx, st, t, r, refs, any(cx.info[i]["currency"] != cur for i in refs)
                )
                continue
            cns = [
                i for i in cands if cx.info[i]["item_type"] == "ARC" and i not in refs
            ]
            if cns:
                idx = _subset([cents(i) for i in cns], target - s, 12)
                if idx is not None:
                    _assign_set(cx, st, t, r, refs + [cns[k] for k in idx], False)
                    continue
            if s < target and not fx:
                # cash larger than the referenced items: the rest is paid for other open invoices of the payer
                others = [i for i in cands_p if i not in refs]
                if 0 < len(others) <= 60:
                    idx = _subset([cents(i) for i in others], target - s, 28)
                    if idx is not None:
                        _assign_set(
                            cx, st, t, r, refs + [others[k] for k in idx], False
                        )
                        continue
            if len(refs) == 1:
                iid = refs[0]
                ic = cx.info[iid]["currency"]
                cash = amt if ic == cur else cx.convert(amt, cur, ic, d)
                a, e = _single_exception(
                    cx, st, iid, cash, r, cx.cust.get(cx.info[iid]["party_id"])
                )
                if e == "PARTIAL" and _last_instalment(txt):
                    e = ""  # POLICIES §3: the last instalment of a plan carries no exception
                if ic != cur and e in (
                    "",
                    "SHORT_DISCOUNT",
                    "SHORT_WITHHOLDING",
                    "SHORT_BANK_CHARGES",
                    "OVERPAYMENT",
                ):
                    # within FX tolerance it is a full clearing with FX difference
                    if abs(cash - st.open.get(iid, 0)) <= FX_TOL * st.open.get(iid, 0):
                        a, e = st.open.get(iid, 0), "FX_DIFFERENCE"
                if e == "OVERPAYMENT" and (OVERPAY_STRICT or cash > OVERPAY_MAX_RATIO * st.open.get(iid, 0)):
                    # cash above the single referenced item and not explained by other open items: most often a
                    # multi-invoice payment with a stray reference -> do not claim here
                    pass
                else:
                    st.assign(t, [(iid, a, e)])
                    continue
        # 2. no usable reference: exact amount of one open invoice (primary payers first), else subset (FIFO first)
        sm = _service_month(txt)
        for pool in (cands_p, cands):
            inv = [i for i in pool if cx.info[i]["item_type"] == "ARI"]
            same = [i for i in inv if close(cents(i), target)]
            same.sort(
                key=lambda i: (
                    cx.info[i]["issue_date"] or "",
                    cx.info[i]["document_number"] or "",
                )
            )
            if sm is not None and len(same) > 1:
                # the text names the service month: pay the invoice of that month (issued the month after), if any
                by_month = sorted(
                    [i for i in same if _service_match(cx, i, sm)],
                    key=lambda i: (
                        0 if int(cx.info[i]["issue_date"][5:7]) == sm[0] % 12 + 1 else 1
                    ),
                )
                same = by_month or same
            if same:
                _assign_set(
                    cx, st, t, r, [same[0]], cx.info[same[0]]["currency"] != cur
                )
                break
        if st.claimed(t):
            continue
        if not fx and 1 < len(cands_p) <= 60:
            vals = [cents(i) for i in cands_p]
            idx = _subset(vals, target)
            if idx is not None:
                _assign_set(cx, st, t, r, [cands_p[k] for k in idx], False)
                continue
        # 3. a referenced item that is already settled -> duplicate payment
        idx_all = [
            i
            for c in cids[:2]
            for i in cx.by_party.get(c, ())
            if st.open.get(i, 0) <= 0.005
            and cx.info[i]["item_type"] == "ARI"
            and abs((cx.info[i]["total_amount"] or 0) - amt) < 0.01
        ]
        refs_all = _ref_items(cx, r, idx_all)
        if refs_all:
            st.assign(t, [(refs_all[0], amt, "DUPLICATE_PAYMENT")])
            continue
