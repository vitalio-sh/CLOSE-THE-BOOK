"""Adversarial unit tests for the direct-debit return rule of the team integration candidate (review/team-integ-r6).

Run against a read-only checkout of r6 (never modified):
    R6_ROOT=outputs/worktrees/r6 python -m unittest solution/validation/test_r6_dd_returns.py -v

The tests encode POLICIES behaviour (3 RETURNED, 5.6 FIFO, 5.10 re-presentation), not the current r6 output:
a failure is a finding to report, not something to "fix" in the test.
"""
import csv
import os
import sys
import unittest
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

R6 = Path(os.environ.get("R6_ROOT", Path(__file__).resolve().parents[2] / "outputs" / "worktrees" / "r6")).resolve()
if not (R6 / "recon" / "batches.py").exists():
    raise unittest.SkipTest(f"r6 checkout not found at {R6} (set R6_ROOT)")
sys.path.insert(0, str(R6))
from recon.batches import BatchMatcher, norm  # noqa: E402
from recon.context import Context  # noqa: E402

MANDATE = "VTM-ES-0004471"
DD_FIELDS = ["batch_id", "collection_date", "account_id", "currency", "debtor_name", "mandate_id", "end_to_end_id",
             "remittance_info", "amount", "re_presentation"]


class DDReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = SimpleNamespace(phase_dir=self.root, items={}, by_number={}, by_party={}, customers={},
                                   suppliers={}, accounts={}, history=defaultdict(list))
        self.batches = []

    # ---------- fixtures ----------
    def invoice(self, iid, number, amount, issue, open_amount=None):
        item = dict(item_id=iid, document_number=number, total_amount=D(amount),
                    open_amount=D(amount if open_amount is None else open_amount), party_id="C1", entity="ES01",
                    currency="EUR", side="AR", kind="invoice", issue_date=issue, due_date=issue)
        self.ctx.items[iid] = item
        self.ctx.by_number.setdefault(norm(number), []).append(iid)
        self.ctx.by_party.setdefault("C1", []).append(iid)

    def collect(self, batch, day, number, amount, repr_="N"):
        self.batches.append(dict(batch_id=batch, collection_date=day, account_id="ES01-A", currency="EUR",
                                 debtor_name="Buyer", mandate_id=MANDATE, end_to_end_id=f"{batch}-0001",
                                 remittance_info=number, amount=amount, re_presentation=repr_))

    def matcher(self):
        dest = self.root / "direct_debit_batches"
        dest.mkdir(exist_ok=True)
        with (dest / "ES01_MAD_2025.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=DD_FIELDS)
            w.writeheader()
            w.writerows(self.batches)
        return BatchMatcher(self.ctx)

    def ret(self, day, amount, code="AM04", batch=None, bank_id="BT-R"):
        text = f"DEVOLUCION RECIBO {code} REM {batch or ''} | MANDATO {MANDATE}"
        return dict(bank_txn_id=bank_id, account_id="ES01-A", amount=f"-{amount}", currency="EUR",
                    booking_date=day, text=text)

    def apply(self, txn, rows):
        Context.apply(self.ctx, txn, rows)       # the real balance update of r6

    def picked(self, result):
        return [(r["item_id"], r["exception"]) for r in result] if result else None

    # ---------- 1) recurring invoices with the same amount ----------
    def test_recurring_same_amount_am04_reverses_latest_collection(self):
        for m, inv in (("01", "FV-JAN"), ("02", "FV-FEB"), ("03", "FV-MAR")):
            self.invoice(f"ARI-{inv}", inv, "50", f"2025-{m}-01", open_amount="0")
            self.collect(f"0125{m}03001", f"2025-{m}-03", inv, "50")
        result = self.matcher().reconcile(self.ret("2025-03-06", "50", "AM04"))
        self.assertEqual(self.picked(result), [("ARI-FV-MAR", "RETURNED")])

    def test_recurring_same_amount_md06_skips_last_three_weeks(self):
        # MD06 = refund request weeks after the debit: the collection 3 days ago is the next one, not the refunded one.
        # Edge case: single-collection batches, so a batch total equals the returned amount.
        for m, inv in (("01", "FV-JAN"), ("02", "FV-FEB"), ("03", "FV-MAR")):
            self.invoice(f"ARI-{inv}", inv, "50", f"2025-{m}-01", open_amount="0")
            self.collect(f"0125{m}03001", f"2025-{m}-03", inv, "50")
        result = self.matcher().reconcile(self.ret("2025-03-06", "50", "MD06"))
        self.assertEqual(self.picked(result), [("ARI-FV-FEB", "RETURNED")])

    def test_recurring_same_amount_md06_multi_row_batches(self):
        # Same as above, but each batch also collects another customer, so no batch total equals the returned amount.
        for m, inv in (("01", "FV-JAN"), ("02", "FV-FEB"), ("03", "FV-MAR")):
            self.invoice(f"ARI-{inv}", inv, "50", f"2025-{m}-01", open_amount="0")
            self.collect(f"0125{m}03001", f"2025-{m}-03", inv, "50")
            self.batches.append(dict(self.batches[-1], mandate_id="VTM-ES-0009999", end_to_end_id=f"0125{m}03001-0002",
                                     remittance_info=f"OTHER-{m}", amount="80"))
        result = self.matcher().reconcile(self.ret("2025-03-06", "50", "MD06"))
        self.assertEqual(self.picked(result), [("ARI-FV-FEB", "RETURNED")])

    def test_recurring_same_amount_named_batch_wins(self):
        for m, inv in (("01", "FV-JAN"), ("02", "FV-FEB"), ("03", "FV-MAR")):
            self.invoice(f"ARI-{inv}", inv, "50", f"2025-{m}-01", open_amount="0")
            self.collect(f"0125{m}03001", f"2025-{m}-03", inv, "50")
        result = self.matcher().reconcile(self.ret("2025-03-06", "50", "MS02", batch="01250103001"))
        self.assertEqual(self.picked(result), [("ARI-FV-JAN", "RETURNED")])

    def test_same_day_two_collections_same_amount_abstains(self):
        # Two invoices of the same amount collected in the same batch: nothing tells which one bounced.
        self.invoice("ARI-A", "FV-A", "50", "2025-03-01", open_amount="0")
        self.invoice("ARI-B", "FV-B", "50", "2025-03-01", open_amount="0")
        self.collect("01250303001", "2025-03-03", "FV-A", "50")
        self.collect("01250303001", "2025-03-03", "FV-B", "50")
        self.assertIsNone(self.matcher().reconcile(self.ret("2025-03-06", "50", "AM04")))

    # ---------- 2) repeated returns on the same invoice ----------
    def test_repeated_returns_same_invoice_never_inflate_balance(self):
        self.invoice("ARI-1", "FV-1", "100", "2025-02-01")
        self.collect("01250303001", "2025-03-03", "FV-1", "100")
        self.collect("01250403001", "2025-04-03", "FV-1", "100", repr_="Y")      # re-presented after 1st return
        m = self.matcher()
        steps = [("2025-03-03", "+", None), ("2025-03-06", "-", "AM04"), ("2025-04-03", "+", None),
                 ("2025-04-07", "-", "AM04")]
        for i, (day, sign, code) in enumerate(steps):
            if sign == "+":
                rows = [dict(item_id="ARI-1", amount="100.00", exception="")]
                self.apply(dict(bank_txn_id=f"BT{i}", booking_date=day, account_id="ES01-A"), rows)
            else:
                txn = self.ret(day, "100", code, bank_id=f"BT{i}")
                result = m.reconcile(txn)
                self.assertEqual(self.picked(result), [("ARI-1", "RETURNED")], f"return on {day}")
                self.apply(txn, result)
            self.assertLessEqual(self.ctx.items["ARI-1"]["open_amount"], D("100"), f"balance after {day}")
        self.assertEqual(self.ctx.items["ARI-1"]["open_amount"], D("100"))

    def test_second_return_targets_re_presented_collection(self):
        # After a re-presentation (re_presentation=Y) the next return of the same amount reverses that re-presentation,
        # not an older unrelated invoice of the same amount.
        self.invoice("ARI-OLD", "FV-OLD", "100", "2025-01-01", open_amount="0")
        self.invoice("ARI-1", "FV-1", "100", "2025-02-01", open_amount="0")
        self.collect("01250203001", "2025-02-03", "FV-OLD", "100")
        self.collect("01250303001", "2025-03-03", "FV-1", "100")
        self.collect("01250403001", "2025-04-03", "FV-1", "100", repr_="Y")
        result = self.matcher().reconcile(self.ret("2025-04-07", "100", "AM04"))
        self.assertEqual(self.picked(result), [("ARI-1", "RETURNED")])

    # ---------- 3) invoice paid by transfer after the return ----------
    def test_transfer_after_return_is_a_normal_payment(self):
        self.invoice("ARI-1", "FV-1", "100", "2025-02-01")
        self.collect("01250303001", "2025-03-03", "FV-1", "100")
        m = self.matcher()
        self.apply(dict(bank_txn_id="BT0", booking_date="2025-03-03", account_id="ES01-A"),
                   [dict(item_id="ARI-1", amount="100.00", exception="")])
        txn = self.ret("2025-03-06", "100", "AM04")
        result = m.reconcile(txn)
        self.assertEqual(self.picked(result), [("ARI-1", "RETURNED")])
        self.apply(txn, result)
        # re-opened at its full amount: a later transfer of 100 settles it with no exception (not DUPLICATE/PARTIAL)
        self.assertEqual(self.ctx.items["ARI-1"]["open_amount"], D("100"))
        self.apply(dict(bank_txn_id="BT2", booking_date="2025-03-20", account_id="ES01-A"),
                   [dict(item_id="ARI-1", amount="100.00", exception="")])
        self.assertEqual(self.ctx.items["ARI-1"]["open_amount"], D("0"))

    def test_return_without_original_collection_abstains(self):
        # The original collection is not in any batch file: do not guess another invoice of the same amount.
        self.invoice("ARI-X", "FV-X", "100", "2025-02-01", open_amount="0")
        self.collect("01250303001", "2025-03-03", "FV-X", "100")
        result = self.matcher().reconcile(self.ret("2025-03-06", "75", "AM04"))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
