import unittest
from decimal import Decimal as D

from recon.credit_groups import reconcile
from tests.test_matching import context, invoice, txn


def credit(i, number, amount, related, reason='Devolución / rectificación', **kw):
    item = invoice(i, number, amount, related_document=related, reason=reason, **kw)
    item['kind'] = 'credit'
    return item


def ids(rows):
    return {r['item_id']: (r['amount'], r['exception']) for r in rows} if rows else None


class CreditGroupTests(unittest.TestCase):
    def test_single_invoice_net_of_its_credit(self):
        # POLICIES 2: the credit note is a positive row at its full amount, no exception.
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1'), invoice('B', 'F2', '55')]
        rows = reconcile(txn('70', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(ids(rows), {'A': ('100.00', ''), 'C': ('30.00', '')})

    def test_quoted_credit_brings_its_invoice(self):
        items = [invoice('A', 'FA240121967', '870.24'), credit('C', 'AV240100569', '130.54', 'FA240121967'),
                 invoice('B', 'FA240119469', '327.61')]
        rows = reconcile(txn('739.70', 'Alice Water Works FACTURE FA241021967 CN 100569'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'C'})

    def test_contiguous_run_with_credit(self):
        items = [invoice('A', 'F1', '100'), invoice('B', 'F2', '200'), credit('C', 'AB1', '50', 'F2'),
                 invoice('E', 'F3', '999')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        rows = reconcile(txn('250', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'B', 'C'})

    def test_run_never_carries_fully_credited_invoice(self):
        # Zero-net veto: an invoice fully offset by its credit is not swept into a run.
        items = [invoice('OLD', 'F0', '40'), credit('Z', 'AB0', '40', 'F0'), invoice('A', 'F1', '100'),
                 invoice('B', 'F2', '200'), credit('C', 'AB1', '50', 'F2')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        rows = reconcile(txn('250', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'B', 'C'})

    def test_cancelled_invoice_never_settled(self):
        # POLICIES 5.7: an invoice cancelled by a credit note is not settled by bank.
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1', reason='Anulación de factura')]
        self.assertIsNone(reconcile(txn('70', 'Alice Water Works PAYMENT'), context(items)))

    def test_ambiguous_single_nets_abstain(self):
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1'),
                 invoice('B', 'F2', '90'), credit('E', 'AB2', '20', 'F2')]
        self.assertIsNone(reconcile(txn('70', 'Alice Water Works PAYMENT'), context(items)))

    def test_unknown_payer_abstains(self):
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1')]
        self.assertIsNone(reconcile(txn('70', 'Mystery sender'), context(items)))

    def test_outgoing_lines_ignored(self):
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1')]
        self.assertIsNone(reconcile(txn('-70', 'Alice Water Works'), context(items)))


if __name__ == '__main__':
    unittest.main()
