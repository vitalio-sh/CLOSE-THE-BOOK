# Claude architecture review

Model confirmed by CLI usage metadata: claude-opus-5-5. Requested effort: xhigh.
This review started before recon/ existed; it is not verification of the finished implementation. Findings are reviewer claims and require checking. In particular sample does contain older invoices, and blank-golden validation provides format checks only, not accuracy or actual item-existence checks.

# Adversarial review: CLOSE-THE-BOOK (read-only)

**Scope.** `recon/` doesn't exist yet, so there's no module code to review. I reviewed the four docs, the official starter and scorer (`data/raw/docs/starter/`), `scripts/download_data.py`, `docs/source/config.json` and samples of the raw data. My tools can't open `.gz` files, so I have **no golden or segment statistics**. Each point is tagged **[E]** (I saw it in a file) or **[I]** (inference).

---

## P0: will block FINAL or silently lose points

**1. macOS `._*` junk files are mixed in with the real data.** [E]
- 19.6k `._*` files sit among the `.gz/.pdf/.xlsx/...` files in train and live, including `._bank_transactions.csv.gz`.
- `._FR01_202510.csv` is a binary "Mac OS X" header containing NUL bytes. Any `glob("*.csv")` will crash or read garbage rows. [I] The FINAL tarball probably has them too.
- **Fix:** one shared `list_files()` that skips names starting with `._`. For pdfs and remittances, drive discovery from `index.csv` instead of globbing.

**2. The FINAL download can block at 18:00.** [E]
- `config.json:60` has an empty `sha256` for final, and `download_data.py:28-29` raises when the checksum is missing.
- The runner assumes the layout `data/raw/final/final`. FINAL also adds `final/masters_final/`.
- **Fix:**
  - At 18:00, run `--refresh`; keep a manual `curl` path ready if the checksum is still empty.
  - The runner should take explicit `--phase-dir` and `--masters-dir` and auto-detect `bank_transactions.csv.gz`.
  - Rehearse this with LIVE placed in a fresh folder.

**3. The contract "modules return complete official row sets" is wrong for anything that depends on balances.** [I, from POLICIES §2–3]
- PARTIAL vs empty, DUPLICATE_PAYMENT, OVERPAYMENT, the RETURNED amount, FIFO, retention release and instalment remainders all depend on the open amount *at that booking date*.
- A module working without the ledger will reuse `total_amount`. The starter does exactly this at `starter.py:45`, and it also treats all train+live documents as open.
- **Fix:**
  - Modules emit proposals: `(txn, source, confidence, [(item_id, cash_in_item_ccy, reason_hint)] | category)`.
  - The root runs **one** `finalize(ledger_state, cash, reason, txn)` that implements POLICIES §2–3, so the 4 engineers don't each write their own version of PARTIAL/FX/OVERPAYMENT.
  - Batch modules can stay stateless.
  - The ordinary matcher **must run inside the root's chronological loop** as `match(txn, ledger_view)`, ordered by `(booking_date, bank_txn_id)`.

**4. The item universe is wrong.**
- Open items = `open_items_at_start.open_amount` plus the phase's own documents at their total.
- All earlier-phase documents should be loaded as **closed** items. They're needed to detect DUPLICATE_PAYMENT, RETURNED (a DD collected June 30 and returned July 3) and "anula y sustituye".
- [I] Returns early in a phase need the *previous* phase's DD and card files. Index them across phases.

**5. Use the official code as the validator.** [E: `bankrec_score.py` is the server's code]
- A golden file containing only a header, plus the phase's `bank_transactions` file, passed to `score_submission()`, reproduces server validation exactly.
- The writer must:
  - merge duplicate `(txn, item)` pairs (advices can list a document twice);
  - drop zero amounts;
  - enforce that a line has either item rows or one category row, never both;
  - use `\n` line endings (the stdlib `csv` default is `\r\n`);
  - format amounts from integer cents.
- FINAL has about 220k lines [E, config], so roughly 0.8–1M rows, around 40–50 MB [I]. The limit is 62.9 MB, so assert the byte size.

---

## P1: score-driven priority (the plan's ordering is off)

**6. Allocation F1 (50 %) counts pairs; transaction accuracy (30 %) counts lines.**
- [E, README] LIVE has about 210k lines but about 750k allocations. One DD batch is about 400 pairs; one advice payment can be about 300.
- [I] So C (batches) and D's advice parsing probably control most of the 50 %, and B's single-invoice transfers plus category lines control most of the 30 %.
- **A's first job:** a table of lines, pairs and maximum points per segment, computed from the TRAIN golden. Assign effort from that table, not from intuition.

**7. Category lines are cheap points but are buried in B's scope after 1:1 matching.**
- Build a lookup or Naive Bayes on `(bank, bank_subcode, bank_code, sign, account purpose, normalised text tokens)` learned from the TRAIN golden. That's about an hour of work.
- Add a **whole-phase pre-pass before matching**:
  - BANK_REVERSAL pairs (the bad original line comes *before* "ANULACION…", so a chronological matcher will match it to an invoice);
  - CASH_POOL / INTERNAL / INTERCOMPANY / FX_DEAL legs (counterparty IBAN is one of our accounts).
- Move this to 11:00.

**8. Exception macro-F1 (20 %): 10 classes, about 2 points each.** A class with zero true positives still counts as 0 in the average.
- [E, `bankrec_score.py:253-263`] Exception credit only needs the right pair; the amount doesn't have to be within tolerance.
- Several classes are **deterministic once the pair is right**:
  - FX_DIFFERENCE: account currency ≠ item currency, item fully cleared, invoice rows only;
  - SHORT_DISCOUNT: terms from the *invoice row*, booking date ≤ issue + discount days + 3, and cash = total − `ROUND_HALF_UP(pct × total)` (Python's `round` uses banker's rounding);
  - SHORT_WITHHOLDING: 1.25 / 1.5 / 10 %;
  - RETURNED and UNIDENTIFIED.
- The plan has these at 14:30–16:00. Put them in `finalize()` by 13:00.

**9. Most PDF clauses don't affect exception codes; the ledger produces them.** [I]
- Instalment k<n, the first payment of a retention invoice and an advance-deducted payment all end up PARTIAL simply because the balance stays open.
- The last instalment and the retention release come out empty because the balance reaches 0.
- So D should use PDFs for **matching**, in this order:
  1. proformas → CUSTOMER_ADVANCE (297 in LIVE [E]);
  2. "anula y sustituye" redirect;
  3. service month, which overrides FIFO;
  4. credit note offset vs paid out / refunded;
  5. new-IBAN notices;
  6. instalment amounts as match hints.
- [E] The PDFs are one-page, templated text ("…fee for the month of June 2025."), so regex is enough. Keep LLMs off the FINAL critical path (shared IP, 70-minute window).

**10. Decision threshold.** Emit a pair when the estimated P(correct) is above about F1/2 (the F1-optimal threshold), calibrated on the dev slice. Amount-only matches with no evidence about the party are deliberate traps (POLICIES §3).

---

## P2: correctness traps that cascade

- **FIFO cascade.** One wrong pick on a monthly fixed fee shifts every later payment from that customer. Parse the service month ("MANT. MAYO 25") before FIFO, and apply FIFO only to items that are *currently* open.
- **Duplicates against open-only search.** If the matcher only searches open items, it pushes a duplicate payment onto the next same-amount invoice. That's two wrong lines. Check closed items with a strong reference first.
- **Reservation.** Items with `payment_method` dd/card/cheque, or listed in a payment run, DD or card file, should be reserved so the fuzzy matcher can't take them first.
- **Advices** [E]:
  - They arrive after the cash (Clearwater: paid 2025-07-31, received 2025-08-08). Match on `payment_ref` plus payer, amount and a date window.
  - The CSV advices have a two-line header, so `DictReader` misreads them.
  - Filename stems collide: `CLEARWATERBA` covers 7+ different payers. Use the payer name from `index.csv`.
  - Clearwater Bay Pool Builders Co. (ES02) switched from CSV to XLSX on 2025-09-03. That is probably the documented layout change, and TRAIN doesn't contain it.
- **Drift that TRAIN can't test:**
  - Moncayo is camt from 2025-10 (key on `bank_subcode`, where it keeps the N43 code).
  - ZA01 uses `KWI…` numbers and ZAR: derive number normalisation from `document_number` values at runtime, not hard-coded prefixes.
  - The new IT01 account from 2026-02-01: unknown `account_id`s must not crash; use `bank_transactions.entity`.
  - Payments are slower in 2026: don't use tight age windows.
- **Payment factory.** `supplier_id` is per entity (`SES00…` vs `SES01…`), so resolve ES00 runs to subsidiary invoices by IBAN, tax ID or name.
- **Money and FX.** Use integer cents throughout. FX rows output the item's open amount, never a converted bank amount. For a PARTIAL in a different currency, use `orig_amount` when it's present.

---

## P3: evaluation leakage

- **`sample/` is a smoke test, not a metric.** It is June 2025 and part of TRAIN, and it has no `open_items_at_start`, so invoices issued before June can't be matched.
- **Dev slice:** score 2025-01…06 (about the size of LIVE). Anything learned — alias tables, category model, thresholds — must come only from months before the slice. Name/IBAN→party aliases from the full TRAIN set are fine for LIVE/FINAL, but evaluating them on TRAIN inflates the score.
- **No teacher forcing.** Take the opening balance from the golden, which mirrors how the organisers built `open_items_at_start`. Inside the slice, update the ledger from *your own predictions*.
- **Highest-value ledger test:** rebuild `live/open_items_at_start` from the TRAIN documents and golden, then diff `open_amount` per item. It's the only ground truth for how RETURNED, DUPLICATE, SHORT_* and credit notes move balances, and it also produces the dev slice's opening balances.
- Use `segment` and `difficulty` for diagnostics only.
- Drift components can only be measured through LIVE's per-segment accuracy. Spend LIVE submissions on those.

---

## P4: scalability and FINAL operations

- TRAIN has 3.96M AR invoices, 1.53M AP invoices, 1.49M lines and 131k PDFs [E]. Loading it as dict rows costs several GB; the starter README itself notes ~8 GB. Do one columnar load (polars/pandas → parquet cache, int cents, only the needed columns). For LIVE/FINAL, keep in memory only the open items plus a document-number index of roughly the last 18 months.
- Bound subset-sum to the party's open items (n ≤ ~20, with a timeout). Large payers send advices.
- **Before 18:00:** precompute everything that doesn't depend on FINAL (TRAIN/LIVE indexes, aliases, parsed PDFs and advices, cached by hash). The FINAL run should only parse FINAL inputs; time the PDF stage on LIVE's 16.7k PDFs with multiprocessing.
- **FINAL sequence:**
  1. Run the fast fallback first (flags `--no-pdf --no-subsetsum`) and validate it.
  2. Run the full pipeline and validate it.
  3. Diff the two (coverage, row count, category mix).
  4. Submit exactly one.
- **Team ops:**
  - Moving D to the video at 16:00 takes the advice owner off the critical path. Assign the video to whoever has the lowest *measured* marginal gain at 16:00.
  - Use one branch with per-module files and a frozen `contract.py` by 10:30, rather than four long-lived branches.

## Revised order
1. **10:00–10:30:** loader with the `._` filter, contract, header-only-golden validator, first LIVE submission.
2. **By 11:00:** segment points table.
3. **11:00–13:00:**
   - A: ledger, `finalize()` and the open-items cross-check.
   - B: category model and pair pre-pass, then party resolution.
   - C: payment runs, DD batches and returns, then card and cheque.
   - D: CSV/TXT advices, then proformas, "anula y sustituye" and service month, then XLSX/PDF advices.
4. **13:00:** first integrated LIVE submission.

**Measure first** (needs code; I couldn't decompress the files): pairs and lines per segment; precision of each proposal source on the dev slice; how far the rebuilt open items differ from `live/open_items_at_start`.
