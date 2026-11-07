import unittest
from types import SimpleNamespace
from decimal import Decimal as D

from recon.documents import DocumentMatcher, Advice, Line


class DeductionTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(item_id='ARI-X', side='AR', kind='invoice', entity='ES01',
                         currency='EUR', issue_date='2025-01-01', payment_terms='D2',
                         total_amount='100.00', open_amount=D('100'), document_number='INV1')
        self.txn = dict(bank_txn_id='BT-X', booking_date='2025-01-14', entity='ES01',
                        amount='98.00', currency='EUR')
        self.line = Line('INV1', D('100'), D('98'))
        self.matcher = object.__new__(DocumentMatcher)
        self.matcher.ctx = SimpleNamespace(
            payment_terms={'D2': {'early_payment_discount_pct': '2', 'discount_days': '10'}},
            items={'ARI-X': self.item}, by_number={'INV1': ['ARI-X']})
        self.matcher.document_aliases = {}

    def matches(self):
        return self.matcher._unlabelled_discount(self.txn, self.line, self.item, self.item['open_amount'])

    def advice(self):
        return Advice('REF', 'ES01', 'PAYER', '2025-01-14', 'EUR', [self.line], 'fixture')

    def test_grace_boundary_and_full_clear(self):
        self.assertTrue(self.matches())
        row = self.matcher._rows(self.txn, self.advice(), True)[0]
        self.assertEqual((row['amount'], row['exception']), ('100.00', 'SHORT_DISCOUNT'))

    def test_late_discount_stays_partial(self):
        self.txn['booking_date'] = '2025-01-15'
        self.assertFalse(self.matches())
        self.assertEqual(self.matcher._rows(self.txn, self.advice(), True)[0]['exception'], 'PARTIAL')

    def test_preissue_rejected(self):
        self.txn['booking_date'] = '2024-12-31'
        self.assertFalse(self.matches())
        self.assertIsNone(self.matcher._rows(self.txn, self.advice(), True))

    def test_explicit_dispute_not_overridden(self):
        for reason in ['PRICE DIFF', 'DAMAGED GOODS', 'LOGISTIC FEE', 'RET', 'OTHER']:
            with self.subTest(reason=reason):
                self.line.reason = reason
                self.assertFalse(self.matches())
                self.assertEqual(self.matcher._rows(self.txn, self.advice(), True)[0]['exception'], 'SHORT_DISPUTE')

    def test_prior_partial_not_cleared(self):
        self.item['open_amount'] = D('99')
        self.assertFalse(self.matches())

    def test_wrong_gross(self):
        self.line.gross = D('101')
        self.assertFalse(self.matches())

    def test_wrong_paid(self):
        self.line.paid = D('97.98')
        self.assertFalse(self.matches())

    def test_missing_or_invalid_terms(self):
        for pct in ['', 'bad', 'NaN', 'Infinity', '-2', '100']:
            with self.subTest(pct=pct):
                self.matcher.ctx.payment_terms['D2']['early_payment_discount_pct'] = pct
                self.assertFalse(self.matches())

    def test_missing_discount_days(self):
        for days in ['', None, '-1', 'invalid']:
            with self.subTest(days=days):
                self.matcher.ctx.payment_terms['D2']['discount_days'] = days
                self.txn['booking_date'] = '2025-01-01'
                self.assertFalse(self.matches())

    def test_no_credit_discount(self):
        self.item.update(kind='credit', item_id='ARC-X')
        self.assertFalse(self.matches())

    def test_cash_mismatch_rejects_whole_advice(self):
        self.txn['amount'] = '97'
        self.assertIsNone(self.matcher._rows(self.txn, self.advice(), True))

    def test_unresolved_line_rejects_whole_advice(self):
        a = self.advice()
        a.lines.append(Line('MISSING', D('100'), D('100')))
        self.txn['amount'] = '198'
        self.assertIsNone(self.matcher._rows(self.txn, a, True))

    def test_fx_precedence_requires_original_currency(self):
        self.txn.update(currency='USD', amount='107.80', orig_amount='98', orig_currency='EUR')
        row = self.matcher._rows(self.txn, self.advice(), True)[0]
        self.assertEqual((row['amount'], row['exception']), ('100.00', 'FX_DIFFERENCE'))
        self.txn['orig_currency'] = 'GBP'
        self.assertIsNone(self.matcher._rows(self.txn, self.advice(), True))

    def test_half_up_rounding(self):
        self.item.update(total_amount='10.75', open_amount=D('10.75'))
        self.line.gross, self.line.paid = D('10.75'), D('10.54')
        self.assertTrue(self.matches())


if __name__ == '__main__':
    unittest.main()
