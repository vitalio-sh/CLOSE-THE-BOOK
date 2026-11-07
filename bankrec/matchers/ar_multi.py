"""AR multi-item / special-amount receipts left unclaimed by `receipts` (agent B).

Runs right after `receipts` and reuses its Ctx (payer resolution, reference keys). Per unclaimed credit line:
  R. references, including the customer's habit of quoting only the last 6 digits of our number ("FACT 200650"
     for FA250200650): referenced items of one customer (full or within-terms discount, POLICIES §3) + netted credit
     notes (§2) + other open items of that customer; a single referenced item takes instalment / partial / late
     discount amounts (§3, §5.8).
  S. no reference: one open item of the payer whose special amount equals the cash exactly and uniquely: early-payment
     discount within terms (SHORT_DISCOUNT), late discount (PARTIAL), instalment total/n (PARTIAL, last one empty),
     round-percentage partial payment (PARTIAL).
  D. no reference, several items of one payer (1-to-N, credit notes netted, N-to-M): FIFO prefix, then exact subset
     with full / discounted values, then subset + one item paid partially.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

import polars as pl

from ..ctx import bank_text
from ..refs import norm, tokens
from .receipts import Ctx, _INST, _single_exception

RATIOS = (0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7)
INST_N = (2, 3, 4, 6)
MAX_DP_ITEMS = 26
DP_CAP = 6000
_D6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_PAYREF = re.compile(r"PAY-\d{4}-\d+")


def _instalment(txt: str):
    """(k, n) when the text names instalment k of n."""
    for m in _INST.finditer((txt or "").upper()):
        k, n = int(m.group(1)), int(m.group(2))
        if 1 <= k <= n and n >= 2:
            return k, n
    m = re.search(
        r"(?:ECHEANCE|TEILZAHLUNG|RATA)\s*(\d{1,2})\s*/\s*(\d{1,2})",
        (txt or "").upper(),
    )
    if m:
        k, n = int(m.group(1)), int(m.group(2))
        if 1 <= k <= n and n >= 2:
            return k, n
    return None


class _Aux:
    def __init__(self, cx: Ctx, ph):
        self.by_amt = defaultdict(
            list
        )  # (entity, cents) -> booking dates of credit lines
        cr = ph.bank.filter(pl.col("amount") > 0)
        for e, a, d in cr.select("entity", "amount", "booking_date").iter_rows():
            self.by_amt[(e, round(a * 100))].append(d)
        # (entity, last 6 digits of our number) -> AR items
        self.suffix = defaultdict(set)
        for iid, r in cx.info.items():
            if r["item_type"] not in ("ARI", "ARC"):
                continue
            d = re.sub(r"\D", "", r["document_number"] or "")
            if len(d) >= 7:
                self.suffix[(r["entity"], d[-6:])].add(iid)


def _disc(cx, iid, d, op):
    """(cash, exception, amount) of an early-payment discount on item iid paid at booking date d, or None."""
    r = cx.info[iid]
    t = cx.terms.get(r["payment_terms"] or "")
    if not t or not t["early_payment_discount_pct"]:
        return None
    total = r["total_amount"] or 0.0
    if abs(op - total) > 0.01:
        return None
    pct = float(t["early_payment_discount_pct"])
    cash = round(total - round(total * pct / 100, 2), 2)
    limit = date.fromisoformat(r["issue_date"]) + timedelta(
        days=int(t["discount_days"] or 0) + 3
    )
    if date.fromisoformat(d) <= limit:
        return cash, "SHORT_DISCOUNT", op
    return cash, "PARTIAL", cash


_REM = re.compile(
    r"\b(RESTO|SOLDE|REST|RESTZAHLUNG|RESTBETRAG|LIQUIDACION|SALDO|BALANCE|REMAINDER|RESTANTE)\b"
)
_RET = re.compile(r"RETENTION|RETENCION|RETENUE|EINBEHALT|GARANT|RITENUTA")


class _Ctxt:
    """Per-line facts used to decide PARTIAL vs empty exception."""

    def __init__(self, aux, r, txt, inst):
        up = (txt or "").upper()
        self.inst = inst
        self.rem_kw = bool(_REM.search(up))
        self.ret_kw = bool(_RET.search(up))
        self.ent = r["entity"]
        self.d = r["booking_date"]
        self.aux = aux
        self.amt = r["amount"]

    def earlier(self, cents: int) -> int:
        """Credit lines of the entity booked before this one with this amount (+-1 cent)."""
        n = 0
        for c in (cents - 1, cents, cents + 1):
            n += sum(1 for x in self.aux.by_amt.get((self.ent, c), ()) if x < self.d)
        return n


def _specials(cx, iid, op, d, lc):
    """Special cash amounts for one invoice: [(cash cents, amount, exception, kind)]."""
    r = cx.info[iid]
    total = r["total_amount"] or op
    tc = round(total * 100)
    out = []
    ds = _disc(cx, iid, d, op)
    if ds:
        out.append((round(ds[0] * 100), ds[2], ds[1], "disc"))
    amt = lc.amt
    inst = lc.inst
    ns = (inst[1],) if inst else INST_N
    for n in ns:
        c = round(total / n, 2)
        last = round(total - (n - 1) * c, 2)
        if inst:
            e = "" if inst[0] == inst[1] else "PARTIAL"
        else:
            done = lc.rem_kw or lc.ret_kw or lc.earlier(round(c * 100)) >= n - 1
            e = "" if done else "PARTIAL"
        if c < op - 0.01:
            out.append((round(c * 100), amt, e, "inst"))
        if last != c and last <= op + 0.01 and (not inst or inst[0] == inst[1]):
            out.append((round(last * 100), amt, "", "inst"))
    if not inst:
        for q in RATIOS + (0.75, 0.05, 0.1):
            c = round(total * q, 2)
            if c >= op - 0.01:
                continue
            cc = round(c * 100)
            if q in (0.05, 0.1):
                if lc.ret_kw:
                    out.append((cc, amt, "", "ratio"))
                continue
            done = lc.rem_kw or lc.ret_kw or q == 0.75 or lc.earlier(tc - cc) > 0
            out.append((cc, amt, "" if done else "PARTIAL", "ratio"))
    return out


def _dp(vals: list, target: int, cap: int = DP_CAP):
    """reach dict sum -> tuple of indices (first found) over vals; stops early when target is reached."""
    reach = {0: ()}
    for i, v in enumerate(vals):
        new = {}
        for s, idx in reach.items():
            ns = s + v
            if ns not in reach and ns not in new:
                new[ns] = idx + (i,)
        reach.update(new)
        if target in reach or len(reach) > cap:
            break
    return reach


def match(ph, st):
    cx = ph.extra.get("_receipts_ctx") or Ctx(ph)
    aux = _Aux(cx, ph)
    rows = ph.bank.filter(pl.col("amount") > 0).sort("booking_date").to_dicts()
    for r in rows:
        if st.claimed(r["bank_txn_id"]):
            continue
        try:
            _line(cx, aux, st, r)
        except Exception:  # never let one line break the stage
            continue


def _line(cx, aux, st, r):
    t = r["bank_txn_id"]
    ent, cur, d, amt = r["entity"], r["currency"], r["booking_date"], r["amount"]
    txt = bank_text(r)
    target = round(amt * 100)
    inst = _instalment(txt)
    lc = _Ctxt(aux, r, txt, inst)
    cids = cx.payers(r, ent)
    sc = dict(cx.last_score) if cids else {}

    def items_of(p):
        return [
            i
            for i in cx.by_party.get(p, ())
            if st.open.get(i, 0) > 0.005
            and (cx.info[i]["issue_date"] or "") <= d
            and cx.info[i]["currency"] == cur
        ]

    def cents(i):
        v = round(st.open.get(i, 0.0) * 100)
        return -v if cx.info[i]["item_type"] == "ARC" else v

    # --- R: references (our keys + last 6 digits) -> customer
    toks = set(t_ for t_ in tokens(txt) if len(t_) >= 5)
    d6 = set(_D6.findall(_PAYREF.sub(" ", txt.upper())))
    fam = set(cids)
    for c in cids[:1]:
        g = cx.cust.get(c, {}).get("group_id")
        if g:
            fam |= cx.group[g]
    ref = []
    for k in toks | d6:
        hit = (
            {i for i in cx.keymap.get(k, ()) if cx.info[i]["entity"] == ent}
            if k in toks
            else set()
        )
        if k in d6:
            # last-6-digit quotes are weak: only among the identified payer's family
            hit |= {
                i
                for i in aux.suffix.get((ent, k), ())
                if not fam or cx.info[i]["party_id"] in fam
            }
        for i in hit:
            if (
                i not in ref
                and st.open.get(i, 0) > 0.005
                and cx.info[i]["currency"] == cur
                and (cx.info[i]["issue_date"] or "") <= d
            ):
                ref.append(i)
    if ref:
        byp = defaultdict(list)
        for i in ref:
            byp[cx.info[i]["party_id"]].append(i)
        ranked = sorted(byp, key=lambda p: (-len(byp[p]), -sc.get(p, 0.0)))
        if (
            len(ranked) == 1
            or len(byp[ranked[0]]) > len(byp[ranked[1]])
            or sc.get(ranked[0], 0) > sc.get(ranked[1], 0)
        ):
            p = ranked[0]
            if _refs(cx, st, t, r, p, byp[p], items_of(p), target, lc, cents):
                return
        return  # references point elsewhere / ambiguous: leave it (precision)
    if not cids:
        return
    top = max(sc.get(c, 0.0) for c in cids)
    primary = [c for c in cids if sc.get(c, 0.0) >= top - 0.02]
    grp = cx.cust.get(primary[0], {}).get("group_id")
    if grp:
        primary += [c for c in cx.group[grp] if c not in primary]
    pools = {p: items_of(p) for p in primary}

    # --- S: one item, special amount equal to the cash (unique among the primary payers)
    hits = []
    for p, its in pools.items():
        for i in its:
            if cx.info[i]["item_type"] != "ARI":
                continue
            op = st.open[i]
            if (
                abs(round(op * 100) - target) <= 1
                and op < (cx.info[i]["total_amount"] or 0) - 0.01
            ):
                hits.append(
                    (i, op, "", "rest")
                )  # remainder after a partial we allocated earlier
                continue
            for c, a, e, kind in _specials(cx, i, op, d, lc):
                if abs(c - target) <= 1:
                    hits.append((i, a, e, kind))
    if hits:
        prio = {"disc": 0, "rest": 1, "inst": 2, "ratio": 3}
        hits.sort(key=lambda h: prio[h[3]])
        best = [h for h in hits if h[3] == "disc"] or hits
        if len({h[0] for h in best}) == 1:
            h = best[0]
            st.assign(t, [(h[0], h[1], h[2])])
        return

    # --- D: several items of one payer
    sols = []
    for p, its in pools.items():
        if len(its) < 2:
            continue
        s = _multi(cx, st, its, target, d, cents)
        if s:
            sols.append(s)
    if len(sols) == 1:
        st.assign(t, sols[0])


def _cash_now(cx, i, d, op, cents_full):
    ds = _disc(cx, i, d, op) if cx.info[i]["item_type"] == "ARI" else None
    if ds:
        # discount taken within terms -> SHORT_DISCOUNT (full open); after the deadline -> PARTIAL (cash applied)
        return round(ds[0] * 100), (i, ds[2], ds[1])
    return cents_full, (i, op, "")


def _multi(cx, st, its, target, d, cents):
    """Allocation rows for several items of one customer summing to target, or None."""
    full = [cents(i) for i in its]
    # FIFO prefix (issue date order, credit notes included), then due-date order
    acc = 0
    for k, v in enumerate(full):
        acc += v
        if acc == target and k >= 1:
            return [(i, st.open[i], "") for i in its[: k + 1]]
    by_due = sorted(
        its, key=lambda i: cx.info[i]["due_date"] or cx.info[i]["issue_date"] or ""
    )
    acc = 0
    for k, i in enumerate(by_due):
        acc += cents(i)
        if acc == target and k >= 1:
            return [(j, st.open[j], "") for j in by_due[: k + 1]]
    now = [_cash_now(cx, i, d, st.open[i], full[k]) for k, i in enumerate(its)]
    has_disc = any(n[1][2] for n in now)
    if has_disc:
        acc = 0
        for k, n in enumerate(now):
            acc += n[0]
            if acc == target and k >= 1:
                return [x[1] for x in now[: k + 1]]
    sub = its[:MAX_DP_ITEMS]
    reach = _dp(full[: len(sub)], target)
    if target in reach and len(reach[target]) >= 2:
        return [(sub[k], st.open[sub[k]], "") for k in reach[target]]
    if has_disc:
        rn = _dp([n[0] for n in now[: len(sub)]], target)
        if target in rn and len(rn[target]) >= 2:
            return [now[k][1] for k in rn[target]]
    return None


def _one_partial(cx, st, refs, full, s, rest, target, cents):
    """N-to-M (POLICIES §3 PARTIAL): referenced invoices in full except one paid a round share of its total, plus
    possibly other open invoices of the customer in full. Accepted only when the solution is unique."""
    if len(refs) > 12 or (len(refs) == 1 and target <= s):
        return None
    sub = rest[:MAX_DP_ITEMS]
    sub_vals = [cents(i) for i in sub]
    sols = []
    for j, i in enumerate(refs):
        if cx.info[i]["item_type"] != "ARI":
            continue
        total = cx.info[i]["total_amount"] or 0.0
        for q in RATIOS:
            c = round(round(total * q, 2) * 100)
            if c >= full[j] - 1:
                continue
            base = s - full[j] + c
            rows = [(x, st.open[x], "") for x in refs if x != i] + [
                (i, c / 100, "PARTIAL")
            ]
            if abs(base - target) <= 1:
                sols.append(rows)
            elif base < target and sub and target > s - full[j]:
                reach = _dp(sub_vals, target - base)
                if reach.get(target - base):
                    sols.append(
                        rows
                        + [(sub[k], st.open[sub[k]], "") for k in reach[target - base]]
                    )
            if len(sols) > 1:
                return None
    return sols[0] if sols else None


def _refs(cx, st, t, r, p, R, pool, target, lc, cents):
    d, amt = r["booking_date"], r["amount"]
    inst = lc.inst
    R = [i for i in R if i in set(pool)] or R
    full = [cents(i) for i in R]
    s = sum(full)
    if abs(s - target) <= 1:
        return st.assign(t, [(i, st.open[i], "") for i in R])
    now = [_cash_now(cx, i, d, st.open[i], full[k]) for k, i in enumerate(R)]
    sn = sum(n[0] for n in now)
    if abs(sn - target) <= 1:
        return st.assign(t, [n[1] for n in now])
    rest = [i for i in pool if i not in R]
    cns = [i for i in rest if cx.info[i]["item_type"] == "ARC"]
    for base, rows in (
        (s, [(i, st.open[i], "") for i in R]),
        (sn, [n[1] for n in now]),
    ):
        if cns:
            reach = _dp([cents(i) for i in cns[:14]], target - base)
            if target - base in reach and reach[target - base]:
                return st.assign(
                    t,
                    rows
                    + [(cns[k], st.open[cns[k]], "") for k in reach[target - base]],
                )
        if base < target and rest:
            sub = rest[:MAX_DP_ITEMS]
            reach = _dp([cents(i) for i in sub], target - base)
            if target - base in reach and reach[target - base]:
                return st.assign(
                    t,
                    rows
                    + [(sub[k], st.open[sub[k]], "") for k in reach[target - base]],
                )
    hit = _one_partial(cx, st, R, full, s, rest, target, cents)
    if hit:
        return st.assign(t, hit)
    if len(R) == 1:
        i = R[0]
        op = st.open[i]
        if amt <= op + 0.01 and (
            (inst and inst[0] == inst[1]) or lc.rem_kw or lc.ret_kw
        ):
            return st.assign(t, [(i, min(amt, op), "")])
        for c, a, e, _k in _specials(cx, i, op, d, lc):
            if abs(c - target) <= 1:
                return st.assign(t, [(i, a, e)])
        a, e = _single_exception(cx, st, i, amt, r, cx.cust.get(p))
        if e == "PARTIAL" and inst:
            e = "" if inst[0] == inst[1] else "PARTIAL"
        return st.assign(t, [(i, a, e)])
    return False
