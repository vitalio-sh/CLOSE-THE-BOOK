"""Adversarial validation of a solver's direct-debit return rule against a holdout with golden labels.

    python solution/validation/dd_returns_report.py --data outputs/q2root --phase live \
        --sub outputs/runs/r6_q2.csv.gz [--label r6] [--examples 3] [--json out.json]

For every golden bank line of segment ar_dd_returned (returned SEPA/BACS/ACH direct debits):
  reason code  parsed from the bank text (SEPA AC04/AM04/MD06/MS02/MS03..., ACH R01..., none)
  outcome      correct   = same item set, amounts within tolerance, exception RETURNED
               wrong     = an item allocated but not the golden one (or not RETURNED)
               abstained = no item allocated (category / UNIDENTIFIED row)
Downstream damage on LATER bank lines (any segment), per item:
  missed reopen  golden-returned item whose return was not (correctly) predicted, later paid again with an empty
                 golden exception but submitted as PARTIAL / DUPLICATE_PAYMENT
  inflated       item wrongly returned by the solver, later submitted as PARTIAL / DUPLICATE_PAYMENT where the
                 golden exception is empty
Wrong picks are explained: same customer? same amount (recurring invoice)? collected again later?
"""
import argparse
import csv
import gzip
import json
import os
import re
from collections import Counter, defaultdict

REASON = re.compile(r"(?<![A-Z0-9])(AC0[1-9]|AC1[0-9]|AG0[1-2]|AM0[1-9]|BE0[1-9]|FF0[1-9]|MD0[1-9]|MS0[1-3]|RC01|RR0[1-4]|"
                    r"SL01|R0[1-9]|R[1-3][0-9])(?![A-Z0-9])")
DAMAGE = ("PARTIAL", "DUPLICATE_PAYMENT")


def rows(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def tol(g):
    return max(0.01, 0.005 * abs(g)) + 1e-9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--phase", required=True)
    ap.add_argument("--sub", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--json")
    a = ap.parse_args()
    P = os.path.join(a.data, a.phase)

    bank = {t["bank_txn_id"]: t for t in rows(os.path.join(P, "bank_transactions.csv.gz"))}
    gold, seg = defaultdict(list), {}
    for r in rows(os.path.join(P, "golden_allocations.csv.gz")):
        gold[r["bank_txn_id"]].append(r)
        seg[r["bank_txn_id"]] = r.get("segment", "")
    sub = defaultdict(list)
    for r in rows(a.sub):
        sub[r["bank_txn_id"]].append(r)
    party = {}
    for name, col in (("ar_invoices", "customer_id"),):
        for r in rows(os.path.join(P, name + ".csv.gz")):
            party[r["item_id"]] = (r[col], r["total_amount"], r["issue_date"])
    for r in rows(os.path.join(P, "open_items_at_start.csv.gz")):
        party.setdefault(r["item_id"], (r["party_id"], r["total_amount"], r["issue_date"]))

    def text(t):
        return " ".join(str(t.get(k) or "") for k in ("text", "ref1", "ref2", "remittance_info")).upper()

    # chronological item events in golden and submission
    order = sorted(bank, key=lambda k: (bank[k]["booking_date"], k))
    pos = {k: i for i, k in enumerate(order)}
    later_g, later_s = defaultdict(list), defaultdict(list)
    for k in order:
        for r in gold.get(k, ()):
            if r["item_id"]:
                later_g[r["item_id"]].append((pos[k], k, r["exception"]))
        for r in sub.get(k, ()):
            if r["item_id"]:
                later_s[r["item_id"]].append((pos[k], k, r["exception"]))

    stats = defaultdict(Counter)
    examples = defaultdict(list)
    missed_items, wrong_items = {}, {}
    for k in order:
        if seg.get(k) != "ar_dd_returned":
            continue
        t, g, s = bank[k], gold[k], sub.get(k, [])
        m = REASON.search(text(t))
        code = m.group(1) if m else "none"
        gi = {r["item_id"]: r for r in g if r["item_id"]}
        si = {r["item_id"]: r for r in s if r["item_id"]}
        if not si:
            outcome = "abstained"
        elif set(si) == set(gi) and all(si[i]["exception"] == "RETURNED" and
                                         abs(float(si[i]["amount"]) - float(gi[i]["amount"])) <= tol(float(gi[i]["amount"]))
                                         for i in gi):
            outcome = "correct"
        else:
            outcome = "wrong"
        stats[code][outcome] += 1
        stats["ALL"][outcome] += 1
        if outcome != "correct":
            for i in gi:
                missed_items[i] = (pos[k], code, outcome)
        if outcome == "wrong":
            why = []
            for j in set(si) - set(gi):
                wrong_items[j] = (pos[k], code)
                pj, pg = party.get(j), party.get(next(iter(gi), ""))
                if pj and pg:
                    why.append("same customer" if pj[0] == pg[0] else "other customer")
                    why.append("same amount (recurring)" if pj[1] == pg[1] else "different amount")
                    why.append(f"picked issued {pj[2]} vs golden {pg[2]}")
            stats["WHY"][", ".join(why[:2]) or "n/a"] += 1
            if len(examples["wrong"]) < a.examples:
                examples["wrong"].append({"bank_txn_id": k, "date": t["booking_date"], "amount": t["amount"],
                                          "code": code, "text": text(t)[:110], "golden": list(gi), "picked": list(si),
                                          "why": why})
        elif outcome == "abstained" and len(examples["abstained"]) < a.examples:
            examples["abstained"].append({"bank_txn_id": k, "date": t["booking_date"], "amount": t["amount"],
                                          "code": code, "text": text(t)[:110], "golden": list(gi),
                                          "submitted": [(r["category"], r["exception"]) for r in s]})

    # downstream damage
    damage = Counter()
    dmg_examples = []
    for kind, items in (("missed reopen", missed_items), ("inflated by wrong return", wrong_items)):
        for i, info in items.items():
            p0 = info[0]
            gold_exc = {kk: e for p, kk, e in later_g.get(i, ()) if p > p0}
            for p, kk, e in later_s.get(i, ()):
                if p > p0 and e in DAMAGE and gold_exc.get(kk, None) in ("", None) and kk in gold_exc:
                    damage[(kind, e)] += 1
                    if len(dmg_examples) < a.examples:
                        dmg_examples.append({"kind": kind, "item": i, "later_line": kk, "submitted": e,
                                             "segment": seg.get(kk), "date": bank[kk]["booking_date"]})
    # all false PARTIAL / DUPLICATE on later collections of DD segments (whatever the cause)
    false_dd = Counter()
    for k in order:
        if seg.get(k) not in ("ar_dd_batch", "ar_dd_represented_transfer"):
            continue
        gi = {r["item_id"]: r["exception"] for r in gold[k] if r["item_id"]}
        for r in sub.get(k, ()):
            if r["item_id"] in gi and r["exception"] in DAMAGE and gi[r["item_id"]] == "":
                false_dd[(seg[k], r["exception"])] += 1

    print(f"== {a.label}: returned direct debits (segment ar_dd_returned) by reason code")
    print(f"{'code':6s} {'lines':>6s} {'correct':>8s} {'wrong':>6s} {'abstain':>8s} {'precision':>9s} {'recall':>7s}")
    for code, c in sorted(stats.items(), key=lambda x: (x[0] in ("ALL", "WHY"), -sum(x[1].values()))):
        if code == "WHY":
            continue
        n = sum(c.values())
        prec = c["correct"] / (c["correct"] + c["wrong"]) if c["correct"] + c["wrong"] else 0
        print(f"{code:6s} {n:6d} {c['correct']:8d} {c['wrong']:6d} {c['abstained']:8d} {prec:9.1%} {c['correct'] / n:7.1%}")
    print("wrong picks explained:", dict(stats["WHY"]))
    print("downstream damage on later lines of the same item:", {f"{k[0]} -> {k[1]}": v for k, v in damage.items()})
    print("all false PARTIAL/DUPLICATE on later DD collections:", {f"{k[0]} {k[1]}": v for k, v in false_dd.items()})
    for kind in ("wrong", "abstained"):
        for e in examples[kind]:
            print(f"  [{kind}]", json.dumps(e, ensure_ascii=False))
    for e in dmg_examples:
        print("  [damage]", json.dumps(e, ensure_ascii=False))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"label": a.label, "by_code": {k: dict(v) for k, v in stats.items()},
                       "damage": {f"{k[0]} -> {k[1]}": v for k, v in damage.items()},
                       "false_dd": {f"{k[0]} {k[1]}": v for k, v in false_dd.items()},
                       "examples": examples, "damage_examples": dmg_examples}, fh, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
