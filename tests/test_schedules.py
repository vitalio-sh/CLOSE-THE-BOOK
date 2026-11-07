from decimal import Decimal as D
from types import SimpleNamespace
import unittest

from test_matching import invoice, txn, context
from recon.schedules import Schedules, instalment_plan, instalment_label, stated_index, grouped_amount, ADVANCE_RE


def matcher(items, facts):
    c = context(items)
    m = Schedules(c)
    m.facts = SimpleNamespace(enabled=True, get=lambda i: facts.get(i, {}))
    return c, m


class PlanTests(unittest.TestCase):
    def test_share_rounds_down_and_last_takes_remainder(self):
        # PDF: 3 vencimientos (1.146,92 / 1.146,92 / 1.146,94 EUR)
        self.assertEqual(instalment_plan({'total_amount': '3440.78'}, {'instalments_count': 3}),
                         (3, D('1146.92'), D('1146.94')))
        self.assertEqual(instalment_plan({'total_amount': '35954.02'}, {'instalments_count': 4}),
                         (4, D('8988.50'), D('8988.52')))
        self.assertIsNone(instalment_plan({'total_amount': '100'}, {}))

    def test_label_by_plan_index_not_by_balance(self):
        plan = (4, D('7466.30'), D('7466.33'))
        # Last instalment paid before the third: no exception although a balance remains.
        self.assertEqual(instalment_label(plan, D('7466.33'), D('14932.63')), '')
        self.assertEqual(instalment_label(plan, D('7466.30'), D('7466.30')), 'PARTIAL')
        equal = (3, D('976.18'), D('976.18'))
        self.assertEqual(instalment_label(equal, D('976.18'), D('976.18'), (2, 3)), 'PARTIAL')
        self.assertEqual(instalment_label(equal, D('976.18'), D('1952.36'), (3, 3)), '')
        self.assertEqual(instalment_label(equal, D('976.18'), D('976.18')), '')
        # Share and remainder one cent apart: 864.47 / 864.48.
        self.assertEqual(instalment_label((2, D('864.47'), D('864.48')), D('864.47'), D('1728.95')), 'PARTIAL')
        self.assertEqual(instalment_label((2, D('864.47'), D('864.48')), D('864.48'), D('1728.95')), '')

    def test_stated_index(self):
        self.assertEqual(stated_index({'text': 'TRANSFERENCIAS X | X PLAZO 1/3'}), (1, 3))
        self.assertEqual(stated_index({'remittance_info': 'INSTALMENT 2/3 INV# 1102415516'}), (2, 3))
        self.assertIsNone(stated_index({'text': 'PAGO 12/10/2024'}))


class InstalmentTests(unittest.TestCase):
    def test_unreferenced_instalment_by_party_and_plan(self):
        c, m = matcher([invoice(amount='3440.78')], {'I1': {'instalments_count': 3}})
        r = m.reconcile(txn('1146.92', 'TRANSFERENCIAS ALICE WATER WORKS PLAZO 1/3'))
        self.assertEqual((r[0]['item_id'], r[0]['amount'], r[0]['exception']), ('I1', '1146.92', 'PARTIAL'))
        self.assertEqual(c.items['I1']['open_amount'], D('3440.78'))

    def test_referenced_last_instalment_out_of_order(self):
        a = invoice(amount='29865.23')
        a['open_amount'] = D('14932.63')
        c, m = matcher([a], {'I1': {'instalments_count': 4}})
        r = m.reconcile(txn('7466.33', 'REGLEMENT FACT FV25-00001'))
        self.assertEqual((r[0]['amount'], r[0]['exception']), ('7466.33', ''))

    def test_no_plan_or_no_pdf_abstains(self):
        c, m = matcher([invoice(amount='3440.78')], {})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS PLAZO 1/3')))

    def test_other_exact_open_item_or_cited_document_abstains(self):
        items = [invoice(amount='3440.78'), invoice('I2', 'FV25-00002', '1146.92')]
        c, m = matcher(items, {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS')))
        items = [invoice(amount='3440.78'), invoice('I2', 'FV25-00002', '5000')]
        c, m = matcher(items, {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS FV25-00002')))
        items = [invoice(amount='3440.78'), invoice('I2', 'FV25-00777', '500')]
        c, m = matcher(items, {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS FV25-00777')))

    def test_two_plans_with_same_share_abstain_unless_cited(self):
        items = [invoice(amount='3440.78'), invoice('I2', 'FV25-00002', '3440.78')]
        facts = {'I1': {'instalments_count': 3}, 'I2': {'instalments_count': 3}}
        c, m = matcher(items, facts)
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS')))
        r = m.reconcile(txn('1146.92', 'ALICE WATER WORKS FRA FV25-00002'))
        self.assertEqual(r[0]['item_id'], 'I2')

    def test_mistyped_reference_of_the_only_fitting_plan(self):
        items = [invoice(number='FV24-0105411', amount='2289.70'), invoice('I2', 'FV24-0105412', '500')]
        c, m = matcher(items, {'I1': {'instalments_count': 3}})
        r = m.reconcile(txn('763.23', 'ALICE WATER WORKS PLAZO 1/3 FACTURA FV24-0105412'))
        self.assertEqual((r[0]['item_id'], r[0]['exception']), ('I1', 'PARTIAL'))
        items = [invoice(number='FV24-0105411', amount='2289.70'), invoice('I2', 'FV24-0105412', '763.23')]
        c, m = matcher(items, {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('763.23', 'ALICE WATER WORKS FACTURA FV24-0105412')))

    def test_fx_instalment(self):
        a = invoice(amount='11856.57')
        a['currency'] = 'USD'
        c, m = matcher([a], {'I1': {'instalments_count': 4}})
        r = m.reconcile(txn('20852.34', 'ALICE WATER WORKS INSTALMENT 1/4', orig_amount='2964.14', orig_currency='USD'))
        self.assertEqual((r[0]['amount'], r[0]['exception']), ('2964.14', 'PARTIAL'))

    def test_stated_instalment_of_unrecognised_payer(self):
        items = [invoice(amount='4257.72'), invoice('I2', 'FV25-00002', '3000')]
        items[0]['party_id'] = 'C9'
        c, m = matcher(items, {'I1': {'instalments_count': 3}, 'I2': {'instalments_count': 2}})
        r = m.reconcile(txn('1419.24', 'FP KINGSMERE POOL INSTALMENT 1/3 PAY'))
        self.assertEqual((r[0]['item_id'], r[0]['exception']), ('I1', 'PARTIAL'))
        self.assertIsNone(m.reconcile(txn('1419.24', 'FP KINGSMERE POOL PAY')))
        self.assertIsNone(m.reconcile(txn('1419.24', 'FP KINGSMERE POOL INSTALMENT 1/2 PAY')))

    def test_last_paid_out_of_order_makes_equal_remainder_partial(self):
        a = invoice(amount='2928.54')
        a['open_amount'] = D('976.18')
        c, m = matcher([a], {'I1': {'instalments_count': 3}})
        c.history = {'I1': [{'amount': '976.18', 'exception': ''}]}
        r = m.reconcile(txn('976.18', 'PAGO FV25-00001'))
        self.assertEqual(r[0]['exception'], 'PARTIAL')

    def test_stated_plan_size_must_agree(self):
        c, m = matcher([invoice(amount='3440.78')], {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS PLAZO 1/4')))

    def test_dispute_and_outgoing_abstain(self):
        c, m = matcher([invoice(amount='3440.78')], {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS DISPUTED PRICE')))
        self.assertIsNone(m.reconcile(txn('-1146.92', 'ALICE WATER WORKS')))


class AdvanceAndRetentionTests(unittest.TestCase):
    def test_advance_deduction_is_partial_for_net_due(self):
        facts = {'I1': {'advance_proforma': 'PF24-0300010', 'advance_amount': '718.53', 'net_due': '1676.57'}}
        c, m = matcher([invoice(amount='2395.10')], facts)
        r = m.reconcile(txn('1676.57', 'DIRECT CREDIT ALICE WATER WORKS'))
        self.assertEqual((r[0]['item_id'], r[0]['amount'], r[0]['exception']), ('I1', '1676.57', 'PARTIAL'))

    def test_advance_on_snapshot_open_at_net_due_is_still_partial(self):
        # POLICIES §3: the payment of an invoice whose PDF deducts an advance is PARTIAL,
        # also when the opening snapshot already carries the invoice at its net due.
        facts = {'I1': {'advance_proforma': 'PF24-0300010', 'advance_amount': '718.53', 'net_due': '1676.57'}}
        a = invoice(amount='2395.10')
        a['open_amount'] = D('1676.57')
        c, m = matcher([a], facts)
        r = m.reconcile(txn('1676.57', 'DIRECT CREDIT ALICE WATER WORKS'))
        self.assertEqual((r[0]['item_id'], r[0]['amount'], r[0]['exception']), ('I1', '1676.57', 'PARTIAL'))
        a['open_amount'] = D('1000.00')
        c, m = matcher([a], facts)
        self.assertIsNone(m.reconcile(txn('1676.57', 'DIRECT CREDIT ALICE WATER WORKS')))

    def test_space_grouped_advance_clause_verified_from_text(self):
        flat = 'LESS ADVANCE RECEIVED AGAINST PROFORMA PF24-0100016: 1 629,09 EUR. NET AMOUNT DUE 3 801,23 EUR.'
        self.assertEqual(grouped_amount(ADVANCE_RE.search(flat)[1]), D('1629.09'))
        self.assertEqual(grouped_amount('1.018,76'), D('1018.76'))
        c, m = matcher([invoice(amount='5430.32')], {})
        m.advance_from_text = lambda item: D('1629.09')
        r = m.reconcile(txn('3801.23', 'VIR SEPA RECU ALICE WATER WORKS'))
        self.assertEqual((r[0]['amount'], r[0]['exception']), ('3801.23', 'PARTIAL'))
        m.advance_from_text = lambda item: None
        self.assertIsNone(m.reconcile(txn('3801.23', 'VIR SEPA RECU ALICE WATER WORKS')))

    def test_advance_fx_keeps_partial(self):
        a = invoice(amount='3053.71')
        a['currency'] = 'USD'
        facts = {'I1': {'advance_amount': '916.11', 'net_due': '2137.60'}}
        c, m = matcher([a], facts)
        r = m.reconcile(txn('40966.45', 'ALICE WATER WORKS', orig_amount='2137.60', orig_currency='USD'))
        self.assertEqual((r[0]['amount'], r[0]['exception']), ('2137.60', 'PARTIAL'))

    def test_retention_with_unparsed_net_uses_percentage(self):
        # 23,047.13 x 0.95 = 21,894.7735 -> 21,894.77 (the PDF prints 21 894,78).
        c, m = matcher([invoice(amount='23047.13')], {'I1': {'retention_pct': '5'}})
        r = m.reconcile(txn('21894.77', 'ALICE WATER WORKS VIREMENT'))
        self.assertEqual((r[0]['amount'], r[0]['exception']), ('21894.77', 'PARTIAL'))

    def test_retention_outranks_withholding_lookalike(self):
        c, m = matcher([invoice(amount='382109.54')], {'I1': {'retention_pct': '10', 'net_due': '343898.59'}})
        r = m.reconcile(txn('343898.59', 'LIQUIDACION ALICE WATER WORKS'))
        self.assertEqual(r[0]['exception'], 'PARTIAL')

    def test_retention_requires_untouched_invoice_and_documented_pct(self):
        a = invoice(amount='23047.13')
        a['open_amount'] = D('20000')
        c, m = matcher([a], {'I1': {'retention_pct': '5'}})
        self.assertIsNone(m.reconcile(txn('21894.77', 'ALICE WATER WORKS')))
        c, m = matcher([invoice(amount='23047.13')], {'I1': {'retention_pct': '20'}})
        self.assertIsNone(m.reconcile(txn('18437.70', 'ALICE WATER WORKS')))


if __name__ == '__main__':
    unittest.main()
