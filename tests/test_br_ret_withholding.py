from decimal import Decimal as D
from types import SimpleNamespace
import unittest

from recon.documents import Advice, DocumentMatcher, Line


class BrazilRetTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(item_id='ARI-1', entity='BR01', side='AR', kind='invoice',
                         party_id='C1', currency='BRL', total_amount=D('100'),
                         open_amount=D('100'), issue_date='2025-01-01')
        self.ctx = SimpleNamespace(items={'ARI-1': self.item},
                                   by_number={'INV1': ['ARI-1']},
                                   customers={'C1': {'country': 'BR'}}, payment_terms={})
        self.matcher = DocumentMatcher.__new__(DocumentMatcher)
        self.matcher.ctx = self.ctx
        self.matcher.document_aliases = {}
        self.txn = dict(bank_txn_id='BT1', entity='BR01', booking_date='2025-01-10',
                        amount='90', currency='BRL')

    def advice(self, paid='90', reason='RET', gross='100'):
        return Advice('PAY12345678', 'BR01', 'Customer', '2025-01-11', 'BRL',
                      [Line('INV1', D(gross), D(paid), reason)], 'fixture.csv')

    def rows(self, paid='90', reason='RET'):
        self.txn['amount'] = paid
        return self.matcher._rows(self.txn, self.advice(paid, reason), True)

    def test_all_three_policy_rates_clear_full_open(self):
        for paid in ('98.75', '98.50', '90'):
            with self.subTest(paid=paid):
                row = self.rows(paid)[0]
                self.assertEqual((row['amount'], row['exception']), ('100.00', 'SHORT_WITHHOLDING'))
                self.assertEqual(self.item['open_amount'], D('100'))

    def test_cent_rounding_half_up(self):
        self.item.update(total_amount=D('100.50'), open_amount=D('100.50'))
        self.assertTrue(self.matcher._br_ret_withholding(
            Line('INV1', D('100.50'), D('99.24'), 'RET'), self.item, D('100.50')))

    def test_unrecognised_rate_stays_dispute(self):
        row = self.rows('95')[0]
        self.assertEqual((row['amount'], row['exception']), ('95.00', 'SHORT_DISPUTE'))

    def test_stated_other_deductions_are_not_tax(self):
        # A blank reason is no longer "not tax": on full TRAIN history a blank-reason advice deduction at an
        # exact MX/BR/MA withholding rate is SHORT_WITHHOLDING in golden every time (remit lane, POLICIES §3).
        for reason in ('PRICE DIFF', 'RET GARANTIA', 'RETENTION', 'RET TAX UNKNOWN'):
            with self.subTest(reason=reason):
                row = self.rows(reason=reason)[0]
                self.assertEqual(row['amount'], '90.00')
                self.assertNotEqual(row['exception'], 'SHORT_WITHHOLDING')

    def test_country_is_required_and_narrowly_brazilian(self):
        for country in ('ES', 'MX', 'MA', ''):
            with self.subTest(country=country):
                self.ctx.customers['C1']['country'] = country
                self.assertEqual(self.rows()[0]['exception'], 'SHORT_DISPUTE')
        self.ctx.customers.clear()
        self.assertEqual(self.rows()[0]['exception'], 'SHORT_DISPUTE')

    def test_part_paid_invoice_not_cleared(self):
        self.item['open_amount'] = D('95')
        row = self.rows()[0]
        self.assertEqual((row['amount'], row['exception']), ('90.00', 'SHORT_DISPUTE'))
        self.item['open_amount'] = D('10')
        row = self.rows('9')[0]
        self.assertEqual((row['amount'], row['exception']), ('9.00', 'SHORT_DISPUTE'))

    def test_wrong_item_type_entity_sign_or_gross_rejected_by_tax_rule(self):
        for changes in ({'side': 'AP'}, {'kind': 'credit'}, {'entity': 'MX01'},
                        {'item_id': 'API-1'}, {'total_amount': D('0')}):
            with self.subTest(changes=changes):
                item = {**self.item, **changes}
                self.assertFalse(self.matcher._br_ret_withholding(
                    self.advice().lines[0], item, D('100')))
        for gross, paid in (('-100', '-90'), ('99.99', '90'), ('100', '0'), ('100', '100')):
            self.assertFalse(self.matcher._br_ret_withholding(
                self.advice(paid=paid, gross=gross).lines[0], self.item, D('100')))

    def test_fx_precedence_and_original_cash_validation(self):
        self.txn.update(currency='EUR', amount='15', orig_currency='BRL', orig_amount='90')
        row = self.matcher._rows(self.txn, self.advice(), True)[0]
        self.assertEqual((row['amount'], row['exception']), ('100.00', 'FX_DIFFERENCE'))
        self.txn['orig_amount'] = '91'
        self.assertIsNone(self.matcher._rows(self.txn, self.advice(), True))

    def test_cash_mismatch_and_ambiguous_invoice_still_rejected(self):
        self.txn['amount'] = '91'
        self.assertIsNone(self.matcher._rows(self.txn, self.advice(), True))
        self.txn['amount'] = '90'
        self.ctx.items['ARI-2'] = {**self.item, 'item_id': 'ARI-2'}
        self.ctx.by_number['INV1'].append('ARI-2')
        self.assertIsNone(self.matcher._rows(self.txn, self.advice(), True))

    def test_group_credit_note_preserved_and_unresolved_line_rejected(self):
        self.ctx.items['ARC-1'] = {**self.item, 'item_id': 'ARC-1', 'kind': 'credit',
                                  'total_amount': D('10'), 'open_amount': D('10')}
        self.ctx.by_number['CR1'] = ['ARC-1']
        advice = self.advice()
        advice.lines.append(Line('CR1', D('-10'), D('-10')))
        self.txn['amount'] = '80'
        rows = self.matcher._rows(self.txn, advice, True)
        self.assertEqual([(r['amount'], r['exception']) for r in rows],
                         [('100.00', 'SHORT_WITHHOLDING'), ('10.00', '')])
        del self.ctx.by_number['CR1']
        self.assertIsNone(self.matcher._rows(self.txn, advice, True))

    def test_later_payment_cannot_reclear_closed_invoice(self):
        self.assertEqual(self.rows()[0]['amount'], '100.00')
        self.item['open_amount'] = D('0')  # Chronological engine applies first allocation.
        self.assertIsNone(self.rows())

    def test_explicit_tax_and_plain_full_payment_unchanged(self):
        self.assertEqual(self.rows(reason='WITHHOLDING')[0]['exception'], 'SHORT_WITHHOLDING')
        self.assertEqual(self.rows('100')[0]['exception'], '')


if __name__ == '__main__':
    unittest.main()
