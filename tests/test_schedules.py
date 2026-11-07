from decimal import Decimal as D
from types import SimpleNamespace
import unittest

from test_matching import invoice, txn, context
from recon.schedules import Schedules, instalment_plan, instalment_label, stated_index


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
        items = [invoice(amount='3440.78'), invoice('I2', 'FV25-00002', '500')]
        c, m = matcher(items, {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS FV25-00002')))

    def test_two_plans_with_same_share_abstain_unless_cited(self):
        items = [invoice(amount='3440.78'), invoice('I2', 'FV25-00002', '3440.78')]
        facts = {'I1': {'instalments_count': 3}, 'I2': {'instalments_count': 3}}
        c, m = matcher(items, facts)
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS')))
        r = m.reconcile(txn('1146.92', 'ALICE WATER WORKS FRA FV25-00002'))
        self.assertEqual(r[0]['item_id'], 'I2')

    def test_stated_plan_size_must_agree(self):
        c, m = matcher([invoice(amount='3440.78')], {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS PLAZO 1/4')))

    def test_dispute_and_outgoing_abstain(self):
        c, m = matcher([invoice(amount='3440.78')], {'I1': {'instalments_count': 3}})
        self.assertIsNone(m.reconcile(txn('1146.92', 'ALICE WATER WORKS DISPUTED PRICE')))
        self.assertIsNone(m.reconcile(txn('-1146.92', 'ALICE WATER WORKS')))


if __name__ == '__main__':
    unittest.main()
