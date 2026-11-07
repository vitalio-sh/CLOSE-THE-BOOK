import unittest
from decimal import Decimal as D
from recon.residual_fees import ResidualFees
from tests.test_matching import context, invoice, txn


class ResidualFeeTests(unittest.TestCase):
    def setUp(self):
        a = invoice('I1', 'FV24-00001', '3156.40')
        b = invoice('I2', 'FV24-00002', '949.36')
        self.c = context([a, b])
        self.c.customers['C1']['country'] = 'IT'

    def line(self, amount, text='TRANSFER. EN DIV. | BJFHGHXX -ALICE WATER WORKS | VIREMENT', **kw):
        return txn(amount, text, **kw)

    def test_unreferenced_cross_border_receipt_short_by_scheduled_fee(self):
        rows = ResidualFees(self.c).reconcile(self.line('3121.40'))
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows],
                         [('I1', '3156.40', 'SHORT_BANK_CHARGES')])
        self.assertEqual(self.c.items['I1']['open_amount'], D('3156.40'))

    def test_requires_cross_border_marker_schedule_fee_and_unique_invoice(self):
        handler = ResidualFees(self.c)
        self.assertIsNone(handler.reconcile(self.line('3121.40', 'TRANSFERENCIA ALICE WATER WORKS')))
        self.assertIsNone(handler.reconcile(self.line('3126.40')))   # 30.00 is not a scheduled fee
        self.assertIsNone(handler.reconcile(self.line('-3121.40')))
        self.c.items['I3'] = invoice('I3', 'FV24-00003', '3156.40')
        self.c.by_party['C1'].append('I3')
        self.assertIsNone(handler.reconcile(self.line('3121.40')))  # two candidate invoices

    def test_foreign_currency_schedule_and_unknown_payer(self):
        d = invoice('I4', 'INV-1', '1425.15', )
        d.update(currency='CAD', entity='US01')
        c = context([d])
        t = txn('1410.35', 'WIRE IN /ORG/ALICE WATER WORKS /RFB/NOTPROVIDED', bank_code='PMNT/RCDT/XBCT')
        t.update(currency='CAD', entity='US01')
        self.assertEqual(ResidualFees(c).reconcile(t)[0]['exception'], 'SHORT_BANK_CHARGES')
        t['text'] = 'WIRE IN /ORG/SOMEONE ELSE /RFB/NOTPROVIDED'
        self.assertIsNone(ResidualFees(c).reconcile(t))
        t['text'] = 'WIRE IN /ORG/ALICE WATER WORKS /RFB/NOTPROVIDED'
        t['entity'] = 'CA01'
        self.assertIsNone(ResidualFees(c).reconcile(t))

    def test_short_payer_name(self):
        a = invoice('I1', 'FV24-00001', '1429.05')
        a['entity'] = 'ES02'
        c = context([a])
        c.customers['C1'].update(name='LQS Spa SA', country='MA', entity='ES02')
        t = txn('1409.05', 'TRANSFER. EN DIV. | BJFHGHXX -LQS SPA SA | PAYMENT | GASTOS')
        t['entity'] = 'ES02'
        rows = ResidualFees(c).reconcile(t)
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows],
                         [('I1', '1429.05', 'SHORT_BANK_CHARGES')])


if __name__ == '__main__':
    unittest.main()
