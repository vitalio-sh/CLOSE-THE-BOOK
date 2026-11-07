import unittest
from tests.test_matching import context, invoice, txn
from recon.matching import fx_invoice_set


def usd_book():
    items = [invoice('U1', 'INV-1001', '1000.00'), invoice('U2', 'INV-1002', '2345.67'),
             invoice('U3', 'INV-1003', '999.99'), invoice('U4', 'INV-1004', '410.10')]
    for item in items:
        item['currency'] = 'USD'
    return items


def receipt(orig, amount='60000', ccy='USD'):
    t = txn(amount, 'SPEI RECIBIDO CONCEPTO Alice Water Works', orig_amount=orig, orig_currency=ccy)
    t['currency'] = 'MXN'
    return t


class FxInvoiceSetTests(unittest.TestCase):
    def test_unique_exact_set_in_instructed_currency(self):
        rows = fx_invoice_set(receipt('3345.67'), context(usd_book()))
        self.assertEqual({(r['item_id'], r['amount'], r['exception']) for r in rows},
                         {('U1', '1000.00', 'FX_DIFFERENCE'), ('U2', '2345.67', 'FX_DIFFERENCE')})

    def test_ambiguous_or_inexact_abstains(self):
        items = usd_book() + [invoice('U5', 'INV-1005', '1000.00')]
        items[-1]['currency'] = 'USD'
        self.assertIsNone(fx_invoice_set(receipt('3345.67'), context(items)))
        self.assertIsNone(fx_invoice_set(receipt('3345.65'), context(usd_book())))

    def test_requires_instructed_amount_in_other_currency(self):
        t = txn('3345.67', 'Alice Water Works')
        self.assertIsNone(fx_invoice_set(t, context(usd_book())))
        self.assertIsNone(fx_invoice_set(receipt('3345.67', ccy='MXN'), context(usd_book())))
