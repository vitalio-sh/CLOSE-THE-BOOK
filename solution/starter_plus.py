"""starter_plus: the organizer starter + one-to-many matching (one bank line -> several documents).

    python solution/starter_plus.py --data data/raw/docs --phase sample --out outputs/sub_sample_plus.csv.gz
    python vendor/starter/score_train.py outputs/sub_sample_plus.csv.gz --data data/raw/docs --sample

Standard library only. Rules, in order, for every bank line:

1. One-to-one (unchanged from the starter): a token of the bank text is one of our document numbers and the document
   total equals the bank amount.
2. One-to-many (new): the bank text cites several document numbers (also written without prefix / leading zeros,
   e.g. "FRAS 4267 4276" or "FV25-0312562 0310057"). Among the cited documents we look for the set whose total equals
   the bank amount exactly, with these general safety constraints:
     - right side: receipts settle AR documents (ARI/ARC), payments settle AP documents (API/APC);
     - same currency as the bank line (FX lines are a different rule);
     - all documents belong to the same party (customer group or supplier);
     - invoices count +, credit notes count - (a payment net of a credit note), at least one invoice;
     - the solution must be unique; if two different sets fit, we do not guess.
3. Payer + amount (new, POLICIES 5.1/5.5/5.6), for receipts still unmatched after rules 1-2 on ALL lines:
     - identify the customer of the bank line's entity from the counterparty IBAN (also an IBAN written in the text),
       otherwise from its name or trade name in the counterparty field / bank text (accents ignored, truncated names
       accepted when at least MIN_NAME_CHARS characters match and the text stops mid-name);
     - among that customer's open invoices (same currency, issued on or before the booking date) take the one whose open
       amount equals the bank amount; several with the same amount -> the oldest (FIFO by invoice date);
     - if the customer itself has none, try the other customers of its group (a parent paying for subsidiaries);
     - if two different customers are equally plausible and both have a matching invoice, do not guess.
   Lines are processed in booking-date order so FIFO consumes invoices chronologically.
   Open items: in LIVE/FINAL only items listed in open_items_at_start (with their open amount) or issued in the phase.
4. Everything else: placeholder row as in the starter (receipts -> UNIDENTIFIED, payments -> BANK_FEES).
"""
import argparse
import csv
import gzip
import os
import re
import unicodedata
from collections import defaultdict

MAX_SUBSET_SEARCH = 18   # above this many candidates of one party we only test "all of them"
MIN_KEY_LEN = 3          # shorter numeric keys are too ambiguous
MIN_NAME_CHARS = 12      # a truncated name must match at least this many characters
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")


def rcsv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def cents(s):
    return int(round(float(s) * 100)) if s else 0


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def number_keys(number):
    """Ways a document number is commonly written in a bank text: full, without prefix, without leading zeros."""
    keys = {norm(number)}
    digits = re.sub(r"\D", "", number)
    if digits:
        keys.add(digits.lstrip("0"))                        # "FV25-0312562" -> "250312562"
    m = re.search(r"(\d+)$", number)
    if m:
        tail = m.group(1)
        keys.add(tail.lstrip("0"))                          # "A005762" -> "5762", "FV25-0312562" -> "312562"
        if len(tail) >= 8:
            keys.add(tail[-7:].lstrip("0"))                 # "FA250105095" -> "105095" (year prefix dropped)
    return {k for k in keys if len(k) >= MIN_KEY_LEN}


def text_keys(text):
    """Candidate keys from the bank text. Keeps '/' and '-' inside tokens (e.g. 'F25/02203')."""
    keys = set()
    for tok in re.split(r"[\s,;|]+", text):
        tok = tok.strip(".:()[]")
        if not tok:
            continue
        parts = [tok] + [p for p in re.split(r"[/]", tok) if p]
        for p in parts:
            n = norm(p)
            keys.add(n)
            if n.isdigit():
                keys.add(n.lstrip("0"))
    return {k for k in keys if len(k) >= MIN_KEY_LEN}


def name_norm(s):
    """'Villamar del Río Riegos S.L.' -> 'VILLAMAR DEL RIO RIEGOS S L'."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s).split())


class NameIndex:
    """Find customer names (full or truncated) inside a free bank text, per entity."""

    def __init__(self):
        self.by_first = defaultdict(list)          # (entity, first word) -> [(normalized name, customer_id)]

    def add(self, entity, name, cid):
        n = name_norm(name)
        if len(n) >= 4:
            self.by_first[(entity, n.split()[0])].append((n, cid))

    def find(self, entity, text):
        """Return the customers with the longest name match (set), or empty set."""
        t = name_norm(text)
        words = t.split()
        best, found, pos = 0, set(), 0
        for w in words:
            start = t.index(w, pos)
            pos = start + len(w)
            for n, cid in self.by_first.get((entity, w), ()):
                seg = t[start:start + len(n)]
                k = 0
                while k < len(seg) and seg[k] == n[k]:
                    k += 1
                if k == len(n):
                    ok = start + k == len(t) or t[start + k] == " "          # full name, whole word
                else:
                    end_of_word = start + k == len(t) or t[start + k] == " "
                    ok = k >= MIN_NAME_CHARS and end_of_word                  # truncated name
                if ok:
                    if k > best:
                        best, found = k, {cid}
                    elif k == best:
                        found.add(cid)
        return found


def unique_subset(cands, target):
    """cands: list of (item_id, signed_cents). Return the unique subset (>=2 items, >=1 invoice) summing to target."""
    if sum(c for _, c in cands) == target:
        return [i for i, _ in cands]
    if len(cands) > MAX_SUBSET_SEARCH:
        return None
    # dynamic programming over sums; keep at most 2 solutions per sum to detect ambiguity
    states = {0: [()]}
    for idx, (_, c) in enumerate(cands):
        new = {}
        for s, sols in states.items():
            for ns, sol in ((s, None), (s + c, idx)):
                bucket = new.setdefault(ns, [])
                for prev in sols:
                    if len(bucket) >= 2:
                        break
                    bucket.append(prev if sol is None else prev + (sol,))
        states = new
    sols = [s for s in states.get(target, []) if len(s) >= 2 and any(cands[i][1] > 0 for i in s)]
    if len(sols) != 1:
        return None
    return [cands[i][0] for i in sols[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "raw", "docs"))
    ap.add_argument("--phase", default="live", choices=["sample", "train", "live", "final"])
    ap.add_argument("--out", default="submission.csv.gz")
    a = ap.parse_args()
    phases = {"sample": ["sample"], "train": ["train"], "live": ["train", "live"], "final": ["train", "live", "final"]}[a.phase]

    # party -> group (a parent company may pay for its subsidiaries)
    group_of, group_members = {}, defaultdict(list)
    cust_by_iban, names = {}, NameIndex()
    cust_path = os.path.join(a.data, "masters", "customers.csv")
    if os.path.exists(cust_path):
        with open(cust_path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                cid = r["customer_id"]
                group_of[cid] = r["group_id"] or cid
                if r["group_id"]:
                    group_members[r["group_id"]].append(cid)
                if r["iban"]:
                    cust_by_iban[norm(r["iban"])] = cid
                names.add(r["entity"], r["name"], cid)
                if r["trade_name"]:
                    names.add(r["entity"], r["trade_name"], cid)

    # open items at the start of the phase (LIVE/FINAL); None = every loaded item counts as open (sample/train)
    open_at_start = None
    p_open = os.path.join(a.data, a.phase, "open_items_at_start.csv.gz")
    if os.path.exists(p_open):
        open_at_start = {r["item_id"]: cents(r["open_amount"]) for r in rcsv(p_open)}

    items, by_number = {}, defaultdict(list)          # starter structures (one-to-one)
    info, by_key = {}, defaultdict(set)               # one-to-many structures
    ar_open = defaultdict(list)                       # (entity, customer, currency, open cents) -> [(issue date, item)]
    for ph in phases:
        for name, numcol in (("ar_invoices", "document_number"), ("ap_invoices", "supplier_invoice_number"), ("credit_notes", "document_number")):
            p = os.path.join(a.data, ph, f"{name}.csv.gz")
            if not os.path.exists(p):
                continue
            for r in rcsv(p):
                iid = r["item_id"]
                items[iid] = cents(r["total_amount"])
                by_number[norm(r[numcol])].append(iid)
                kind = iid[:3]                                           # ARI / ARC / API / APC
                party = r.get("customer_id") or r.get("supplier_id") or r.get("party_id") or ""
                info[iid] = {
                    "side": "AR" if kind.startswith("AR") else "AP",
                    "sign": 1 if kind in ("ARI", "API") else -1,
                    "currency": r["currency"],
                    "party": group_of.get(party, party),
                }
                if kind == "ARI":
                    # candidate for the payer + amount rule, if it is open
                    if ph == a.phase or open_at_start is None:
                        open_amt = items[iid]
                    else:
                        open_amt = open_at_start.get(iid)
                    if open_amt:
                        ar_open[(r["entity"], r["customer_id"], r["currency"], open_amt)].append((r["issue_date"], iid))
                for k in number_keys(r[numcol]):
                    by_key[k].add(iid)

    used = set()
    txns = list(rcsv(os.path.join(a.data, a.phase, "bank_transactions.csv.gz")))
    result = {}                                       # bank_txn_id -> list of output rows
    pending = []                                      # receipts left for the payer + amount rule
    n_match = n_multi = n_multi_items = n_payer = 0
    for t in txns:
        amt = cents(t["amount"])
        text = " ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]]).upper()

        # 1) one-to-one, exactly as the starter
        hit = None
        for tok in re.split(r"[\s|,;/]+", text):
            for iid in by_number.get(norm(tok), []) if len(tok) >= 5 else []:
                if iid not in used and items[iid] == abs(amt):
                    hit = iid
                    break
            if hit:
                break
        if hit:
            used.add(hit)
            n_match += 1
            result[t["bank_txn_id"]] = [[t["bank_txn_id"], hit, f"{abs(amt) / 100:.2f}", "", ""]]
            continue

        # 2) one-to-many: several cited documents of one party whose net total equals the amount
        side = "AR" if amt > 0 else "AP"
        by_party = defaultdict(set)
        for k in text_keys(text):
            for iid in by_key.get(k, ()):
                d = info[iid]
                if iid not in used and d["side"] == side and d["currency"] == t["currency"]:
                    by_party[d["party"]].add(iid)
        solutions = []
        for party, ids in by_party.items():
            if len(ids) < 2:
                continue
            cands = sorted((iid, info[iid]["sign"] * items[iid]) for iid in ids)
            sol = unique_subset(cands, abs(amt))
            if sol:
                solutions.append(sol)
        if len(solutions) == 1:
            result[t["bank_txn_id"]] = []
            for iid in solutions[0]:
                used.add(iid)
                result[t["bank_txn_id"]].append([t["bank_txn_id"], iid, f"{items[iid] / 100:.2f}", "", ""])
            n_multi += 1
            n_multi_items += len(solutions[0])
            continue
        if amt > 0:
            pending.append(t)

    # 3) payer + amount + FIFO, after all reference matches, in booking-date order
    def open_invoice(entity, cid, currency, amount, booking):
        """Oldest open invoice of this customer with exactly this open amount, issued by the booking date."""
        for issue, iid in sorted(ar_open.get((entity, cid, currency, amount), ())):
            if iid not in used and issue <= booking:
                return issue, iid
        return None

    for t in sorted(pending, key=lambda x: x["booking_date"]):
        amt, ent, cur, day = cents(t["amount"]), t["entity"], t["currency"], t["booking_date"]
        text = " ".join([t["counterparty_name"], t["text"], t["ref1"], t["ref2"], t["remittance_info"]])
        payers = set()
        for iban in [t["counterparty_account"]] + IBAN_RE.findall(text.upper().replace(" ", "")):
            if norm(iban) in cust_by_iban:
                payers = {cust_by_iban[norm(iban)]}
                break
        if not payers:
            payers = names.find(ent, text)
        hits = {}                                       # customer -> (issue date, item)
        for cid in payers:
            h = open_invoice(ent, cid, cur, amt, day)
            if h:
                hits[cid] = h
        if not hits:                                    # parent paying for a subsidiary of its group
            for g in {group_of.get(c) for c in payers if group_of.get(c) in group_members}:
                for cid in group_members[g]:
                    h = open_invoice(ent, cid, cur, amt, day)
                    if h:
                        hits[cid] = h
        if len(hits) == 1:
            iid = next(iter(hits.values()))[1]
            used.add(iid)
            n_payer += 1
            result[t["bank_txn_id"]] = [[t["bank_txn_id"], iid, f"{amt / 100:.2f}", "", ""]]

    # 4) placeholder for everything else, output in the original line order
    rows = []
    for t in txns:
        if t["bank_txn_id"] in result:
            rows.extend(result[t["bank_txn_id"]])
        elif cents(t["amount"]) > 0:
            rows.append([t["bank_txn_id"], "", "", "UNIDENTIFIED", "UNIDENTIFIED"])
        else:
            rows.append([t["bank_txn_id"], "", "", "BANK_FEES", ""])

    op = gzip.open if a.out.endswith(".gz") else open
    with op(a.out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
        w.writerows(rows)
    print(f"{len(rows)} rows ({n_match} one-to-one, {n_multi} one-to-many lines with {n_multi_items} items, "
          f"{n_payer} payer+amount) -> {a.out}")


if __name__ == "__main__":
    main()
