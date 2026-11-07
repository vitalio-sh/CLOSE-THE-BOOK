"""starter_plus: the organizer starter + one-to-many matching (one bank line -> several documents).

    python solution/starter_plus.py --data data/raw/docs --phase sample --out outputs/sub_sample_plus.csv.gz
    python vendor/starter/score_train.py outputs/sub_sample_plus.csv.gz --data data/raw/docs --sample

Standard library only. Rules, in order, for every bank line:

0a. Direct-debit batch (POLICIES 5.3): a receipt whose text carries the batch_id of a file in direct_debit_batches/
    and whose amount equals the batch total clears every receipt of that batch. Each batch row is resolved to an
    invoice of the debtor (mandate_id -> customer in customers.csv) by the document number in its remittance_info
    (also without prefix / leading zeros); failing that, the debtor's oldest open invoice with the row amount.
0b. Returned direct debit (POLICIES 3 RETURNED): a debit whose text carries the SEPA mandate of one of our customers.
    The item is that customer's invoice with the returned amount: preferably one collected under that mandate in a
    batch of the phase before the return (the latest), otherwise the oldest such invoice. Row: amount = returned
    amount, exception RETURNED; the invoice is re-opened (it can be re-presented or paid by transfer later).
0c. Payment run (POLICIES 5.3): a debit whose end_to_end_id (field or text) is a transfer of a payment_runs/ file
    clears the documents of that transfer; a debit carrying a run_id with exactly the run total clears the whole run.
    Each row -> supplier invoice / credit note by document number, of the supplier owning the beneficiary IBAN
    (suppliers.csv); if the beneficiary is not the supplier (factor, confirming bank), a unique document number.
    Amount = document amount (positive). Exception: PARTIAL if less than the item's open balance; FX_DIFFERENCE for
    invoices in a currency other than the bank line's; credit notes none.
0d. Card settlement (POLICIES 4, 5.3): a receipt carrying the merchant_id (or settlement_id) of a card_settlements/
    file whose net total equals the amount clears every order of that settlement, at GROSS amount (the acquirer fee
    is contractual, not an exception). Order number -> invoice by document number.
0e. Cheque deposit (POLICIES 5.3): a receipt carrying a deposit_id of cheque_deposits/ with exactly its total clears
    each cheque: customer = drawer name; invoice = stub_reference as document number (also without prefix) or as
    customer PO, else that customer's oldest open invoice with the cheque amount.
0f. Returned cheque / card chargeback (POLICIES 3 RETURNED): a debit carrying the number of a deposited cheque, or a
    masked card (****1234) of a card order with exactly the debited gross amount -> that invoice, RETURNED, re-opened.
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
import glob
import gzip
import os
import re
import unicodedata
from collections import defaultdict

MAX_SUBSET_SEARCH = 18   # above this many candidates of one party we only test "all of them"
MIN_KEY_LEN = 3          # shorter numeric keys are too ambiguous
MIN_NAME_CHARS = 12      # a truncated name must match at least this many characters
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
CARD_MASK_RE = re.compile(r"\*{2,}(\d{4})")  # masked card number in a bank text: ****6737
TOKEN_SPLIT = re.compile(r"[\s|,;+:/]+")  # 'MREF+VTM-DE-0001685' -> 'MREF', 'VTM-DE-0001685'


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
    ap.add_argument("--phase", default="live", help="sample, train, live, final, or a test phase folder (e.g. trainq2)")
    ap.add_argument("--out", default="submission.csv.gz")
    a = ap.parse_args()
    phases = {"sample": ["sample"], "train": ["train"], "live": ["train", "live"],
              "final": ["train", "live", "final"]}.get(a.phase, [a.phase])

    # party -> group (a parent company may pay for its subsidiaries)
    group_of, group_members = {}, defaultdict(list)
    cust_by_iban, cust_by_mandate, names = {}, {}, NameIndex()
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
                if r.get("sepa_mandate_id"):
                    cust_by_mandate[norm(r["sepa_mandate_id"])] = cid
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
    ar_all = defaultdict(list)                        # (customer, currency, total cents) -> [(issue date, item)]
    by_po = defaultdict(list)                         # customer PO number -> AR invoices
    for ph in phases:
        for name, numcol in (("ar_invoices", "document_number"), ("ap_invoices", "supplier_invoice_number"), ("credit_notes", "document_number")):
            p = os.path.join(a.data, ph, f"{name}.csv.gz")
            if not os.path.exists(p):
                continue
            carried = ph != a.phase and open_at_start is not None
            for r in rcsv(p):
                iid = r["item_id"]
                # documents of earlier phases: only those still open at the start of this phase, at their open
                # amount (the others are already settled; loading them all would also need ~10 GB of RAM)
                if carried and iid not in open_at_start:
                    continue
                # open amount when the item is listed as open at start, else the document total
                items[iid] = open_at_start[iid] if open_at_start and iid in open_at_start else cents(r["total_amount"])
                by_number[norm(r[numcol])].append(iid)
                if r.get("customer_po"):
                    by_po[norm(r["customer_po"])].append(iid)
                kind = iid[:3]                                           # ARI / ARC / API / APC
                party = r.get("customer_id") or r.get("supplier_id") or r.get("party_id") or ""
                info[iid] = {
                    "side": "AR" if kind.startswith("AR") else "AP",
                    "sign": 1 if kind in ("ARI", "API") else -1,
                    "currency": r["currency"],
                    "party": group_of.get(party, party),
                    "cust": party,
                    "kind": kind,
                }
                if kind == "ARI":
                    ar_all[(r["customer_id"], r["currency"], items[iid])].append((r["issue_date"], iid))
                    # candidate for the payer + amount rule, if it is open
                    open_amt = items[iid]
                    if open_amt:
                        ar_open[(r["entity"], r["customer_id"], r["currency"], open_amt)].append((r["issue_date"], iid))
                for k in number_keys(r[numcol]):
                    by_key[k].add(iid)

    for k in ar_all:
        ar_all[k].sort()

    # direct-debit batch files of the phase: batch_id -> rows
    dd_batches = defaultdict(list)
    for p in sorted(glob.glob(os.path.join(a.data, a.phase, "direct_debit_batches", "*.csv"))):
        with open(p, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                dd_batches[norm(r["batch_id"])].append(r)

    def read_dir(sub):
        out = []
        for p in sorted(glob.glob(os.path.join(a.data, a.phase, sub, "*.csv"))):
            with open(p, encoding="utf-8", newline="") as fh:
                out.extend(csv.DictReader(fh))
        return out

    # card settlements: merchant_id / settlement_id -> settlements (settlement_id -> rows); card mask -> rows
    card_sets, card_keys, card_by_mask = defaultdict(list), defaultdict(set), defaultdict(list)
    for r in read_dir("card_settlements"):
        card_sets[r["settlement_id"]].append(r)
        card_keys[norm(r["merchant_id"])].add(r["settlement_id"])
        card_keys[norm(r["settlement_id"])].add(r["settlement_id"])
        card_by_mask[norm(r["card"])].append(r)          # '****6737' -> '6737'
    # cheque deposits: deposit_id -> rows; cheque number -> row
    cheque_deps, cheque_by_number = defaultdict(list), {}
    for r in read_dir("cheque_deposits"):
        cheque_deps[norm(r["deposit_id"])].append(r)
        cheque_by_number[norm(r["cheque_number"])] = r

    def resolve_card_row(r):
        cands = [iid for iid in by_number.get(norm(r["order_number"]), ()) if info[iid]["kind"] == "ARI"]
        return cands[0] if len(cands) == 1 else None

    def resolve_cheque_row(r, entity):
        payers = names.find(entity, r["drawer"])
        amt_ = cents(r["amount"])
        ref = r["stub_reference"].upper()
        keyed = {iid for k in text_keys(ref) for iid in by_key.get(k, ())} | set(by_po.get(norm(ref), ())) if ref else set()
        cands = [iid for iid in keyed if info[iid]["kind"] == "ARI" and info[iid]["currency"] == r["currency"]
                 and (not payers or info[iid]["cust"] in payers)]
        if len(cands) > 1:
            cands = [iid for iid in cands if items[iid] == amt_]
        if len(cands) == 1:
            return cands[0]
        # no usable reference: the drawer's oldest open invoice with this amount; with homonym customers, only if
        # exactly one of them has such an invoice
        hits = []
        for cid in payers:
            hit = next((iid for issue, iid in ar_all.get((cid, r["currency"], amt_), ())
                        if iid not in used and issue <= r["deposit_date"]), None)
            if hit:
                hits.append(hit)
        return hits[0] if len(hits) == 1 else None

    cheque_item = {}                                  # cheque number -> invoice it paid

    # payment-run files of the phase: end_to_end_id -> rows, run_id -> rows; supplier by IBAN
    run_by_e2e, run_by_id = defaultdict(list), defaultdict(list)
    for p in sorted(glob.glob(os.path.join(a.data, a.phase, "payment_runs", "*.csv"))):
        with open(p, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                run_by_e2e[norm(r["end_to_end_id"])].append(r)
                run_by_id[norm(r["run_id"])].append(r)
    sup_by_iban = defaultdict(set)
    sup_path = os.path.join(a.data, "masters", "suppliers.csv")
    if os.path.exists(sup_path):
        with open(sup_path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                for acc in (r["iban"], r.get("account_number", "")):
                    if acc:
                        sup_by_iban[norm(acc)].add(r["supplier_id"])

    def run_total(rows):
        return sum({r["end_to_end_id"]: cents(r["transfer_amount"]) for r in rows}.values())

    def resolve_run_row(r):
        """Supplier invoice / credit note paid by one row of a payment-run file."""
        kind = "APC" if r["document_type"] == "CREDIT_NOTE" else "API"
        doc_amt = abs(cents(r["document_amount"]))
        cands = [iid for iid in by_number.get(norm(r["document_number"]), ())
                 if info[iid]["kind"] == kind and info[iid]["currency"] == r["currency"]]
        own = [iid for iid in cands if info[iid]["cust"] in sup_by_iban.get(norm(r["beneficiary_iban"]), ())]
        pool = own or cands                           # beneficiary not the supplier: factor / confirming bank
        if len(pool) > 1:
            pool = [iid for iid in pool if items[iid] == doc_amt]
        return pool[0] if len(pool) == 1 else None

    balance = {}                                      # item -> open balance in cents (payment runs)
    used = set()
    returned = set()
    dd_collected = defaultdict(list)                  # mandate -> [(collection date, item)] collected by our batches

    def resolve_dd_row(r, currency):
        """Invoice paid by one row of a direct-debit batch file."""
        cid = cust_by_mandate.get(norm(r["mandate_id"]))
        row_amt = cents(r["amount"])
        cands = {iid for k in text_keys(r["remittance_info"].upper()) for iid in by_key.get(k, ())
                 if info[iid]["kind"] == "ARI" and info[iid]["currency"] == currency
                 and (cid is None or info[iid]["cust"] == cid)}
        if len(cands) > 1:
            cands = {iid for iid in cands if items[iid] == row_amt}
        if len(cands) == 1:
            return cands.pop()
        if cid:                                       # no usable number: debtor's oldest open invoice with this amount
            for issue, iid in ar_all.get((cid, currency, row_amt), ()):
                if iid not in used and issue <= r["collection_date"]:
                    return iid
        return None

    txns = list(rcsv(os.path.join(a.data, a.phase, "bank_transactions.csv.gz")))
    result = {}                                       # bank_txn_id -> list of output rows
    pending = []                                      # receipts left for the payer + amount rule
    n_match = n_multi = n_multi_items = n_payer = n_batch = n_batch_items = n_returned = n_run = n_run_items = n_card = n_cheque = 0
    for t in txns:
        amt = cents(t["amount"])
        text = " ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]]).upper()
        tokens = [norm(x) for x in TOKEN_SPLIT.split(text) if x]
        tokens += [d for x in tokens for d in re.findall(r"\d{8,}", x)]   # 'DEV02250523133' -> '02250523133'

        # 0a) direct-debit batch credit
        if amt > 0:
            batch = next((dd_batches[x] for x in tokens if x in dd_batches
                          and cents(dd_batches[x][0]["batch_total"]) == amt), None)
            if batch:
                out = []
                for r in batch:
                    iid = resolve_dd_row(r, t["currency"])
                    if iid and iid not in {o[1] for o in out}:
                        used.add(iid)
                        returned.discard(iid)
                        dd_collected[norm(r["mandate_id"])].append((r["collection_date"], iid))
                        out.append([t["bank_txn_id"], iid, f"{cents(r['amount']) / 100:.2f}", "", ""])
                if out:
                    result[t["bank_txn_id"]] = out
                    n_batch += 1
                    n_batch_items += len(out)
                    continue

        # 0d) card settlement: merchant / settlement id in the text, net total = amount -> orders at gross
        if amt > 0:
            sids = {s for x in tokens for s in card_keys.get(x, ())}
            fits = [s for s in sids if sum(cents(r["net"]) for r in card_sets[s]) == amt
                    and card_sets[s][0]["currency"] == t["currency"]]
            if len(fits) == 1:
                out = []
                for r in card_sets[fits[0]]:
                    iid = resolve_card_row(r)
                    if iid and iid not in {o[1] for o in out}:
                        used.add(iid)
                        returned.discard(iid)
                        out.append([t["bank_txn_id"], iid, f"{cents(r['gross']) / 100:.2f}", "", ""])
                if out:
                    result[t["bank_txn_id"]] = out
                    n_card += 1
                    continue

        # 0e) cheque deposit: deposit id in the text with exactly its total
        if amt > 0:
            dep = next((cheque_deps[x] for x in tokens if x in cheque_deps
                        and cents(cheque_deps[x][0]["deposit_total"]) == amt), None)
            if dep:
                out = []
                for r in dep:
                    iid = resolve_cheque_row(r, t["entity"])
                    if iid and iid not in {o[1] for o in out}:
                        used.add(iid)
                        returned.discard(iid)
                        cheque_item[norm(r["cheque_number"])] = iid
                        out.append([t["bank_txn_id"], iid, f"{cents(r['amount']) / 100:.2f}", "", ""])
                if out:
                    result[t["bank_txn_id"]] = out
                    n_cheque += 1
                    continue

        # 0f) returned cheque (cheque number) or card chargeback (masked card + gross amount)
        if amt < 0:
            iid = None
            masks = {m for m in CARD_MASK_RE.findall(text)}   # only real masked cards, not any 4 digits
            for x in tokens + [m for m in masks if m not in tokens]:
                if x in cheque_by_number and cents(cheque_by_number[x]["amount"]) == -amt:
                    iid = cheque_item.get(x) or resolve_cheque_row(cheque_by_number[x], t["entity"])
                    break
                if x in masks and x in card_by_mask:
                    rows_ = [r for r in card_by_mask[x] if cents(r["gross"]) == -amt]
                    if len(rows_) == 1:
                        iid = resolve_card_row(rows_[0])
                        break
            if iid:
                used.discard(iid)
                returned.add(iid)
                n_returned += 1
                result[t["bank_txn_id"]] = [[t["bank_txn_id"], iid, f"{-amt / 100:.2f}", "", "RETURNED"]]
                continue

        # 0b) returned direct debit: a debit carrying one of our customers' SEPA mandates
        if amt < 0:
            mandate = next((x for x in tokens if x in cust_by_mandate), None)
            if mandate:
                cid, day = cust_by_mandate[mandate], t["booking_date"]
                collected = [iid for d, iid in sorted(dd_collected.get(mandate, ()), reverse=True)
                             if d <= day and items[iid] == -amt and iid not in returned]
                older = [iid for issue, iid in ar_all.get((cid, t["currency"], -amt), ())
                         if issue <= day and iid not in returned]
                iid = (collected or older or [None])[0]
                if iid:
                    used.discard(iid)                 # the invoice is open again
                    returned.add(iid)
                    n_returned += 1
                    result[t["bank_txn_id"]] = [[t["bank_txn_id"], iid, f"{-amt / 100:.2f}", "", "RETURNED"]]
                    continue

        # 0c) payment run: one transfer (end_to_end_id) or a whole run with exactly its total
        if amt < 0:
            run = None
            for x in [norm(t["end_to_end_id"])] + tokens:
                if x in run_by_e2e:
                    rows_ = run_by_e2e[x]
                    if rows_[0]["currency"] != t["currency"] or cents(rows_[0]["transfer_amount"]) == -amt:
                        run = rows_
                        break
                if x in run_by_id and run_total(run_by_id[x]) == -amt:
                    run = run_by_id[x]
                    break
            if run:
                out = []
                for r in run:
                    iid = resolve_run_row(r)
                    if not iid or iid in {o[1] for o in out}:
                        continue
                    paid = abs(cents(r["document_amount"]))
                    open_amt = balance.get(iid, items[iid])
                    exc = ""
                    if info[iid]["kind"] == "API":
                        if paid < open_amt - max(1, open_amt // 200):
                            exc = "PARTIAL"
                        elif r["currency"] != t["currency"]:
                            exc = "FX_DIFFERENCE"
                    balance[iid] = max(0, open_amt - paid)
                    used.add(iid)
                    out.append([t["bank_txn_id"], iid, f"{paid / 100:.2f}", "", exc])
                if out:
                    result[t["bank_txn_id"]] = out
                    n_run += 1
                    n_run_items += len(out)
                    continue

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
    print(f"{len(rows)} rows ({n_batch} DD batches with {n_batch_items} items, {n_card} card settlements, {n_cheque} cheque deposits, {n_returned} returned, {n_run} payment-run lines with {n_run_items} items, {n_match} one-to-one,{n_multi} one-to-many lines with {n_multi_items} items, "
          f"{n_payer} payer+amount) -> {a.out}")


if __name__ == "__main__":
    main()
