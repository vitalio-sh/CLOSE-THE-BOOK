import unittest
from types import SimpleNamespace
from decimal import Decimal as D

from recon import refnoise
from recon.matching import norm


def context(items, customers):
    by_number, by_party = {}, {}
    for i in items:
        by_number.setdefault(norm(i['document_number']), []).append(i['item_id'])
        by_party.setdefault(i['party_id'], []).append(i['item_id'])
    return SimpleNamespace(items={i['item_id']: i for i in items}, by_number=by_number, by_party=by_party,
                           customers=customers, suppliers={}, accounts={}, history={}, payment_terms={})


def invoice(i, number, amount, party='C1', day='01'):
    return dict(item_id=i, document_number=number, party_id=party, side='AR', kind='invoice',
                open_amount=D(amount), total_amount=D(amount), currency='EUR', entity='ES01',
                issue_date=f'2024-09-{day}')


def txn(amount, text, **kw):
    return dict(bank_txn_id='T1', amount=amount, text=text, currency='EUR', entity='ES01',
                booking_date='2024-10-15', **kw)


CUSTOMERS = {'C1': {'name': 'Rocaverde Piscinas S.L.', 'entity': 'ES01'},
             'C2': {'name': 'Rocaverde Piscinas S.L.', 'entity': 'ES01'}}


class RefNoiseTests(unittest.TestCase):
    def test_typo_and_truncation_anchor_the_remaining_set(self):
        # POLICIES 5.5: transposed digits (FV42 for FV24) and a truncated number.
        items = [invoice('A', 'FV24-0214535', '1674.64'), invoice('B', 'FV24-0210155', '250.99'),
                 invoice('C', 'FV24-0216588', '2391.52'), invoice('D', 'FV24-0216870', '702.79')]
        rows = refnoise.reconcile(txn('1925.63', 'TRANSFERENCIA ROCAVERDE PISCINAS PAGO FRA. FV42-0214535 FV24-02101'),
                                  context(items, CUSTOMERS))
        self.assertEqual({r['item_id']: r['amount'] for r in rows}, {'A': '1674.64', 'B': '250.99'})

    def test_same_name_customers_need_one_payer_with_an_exact_set(self):
        items = [invoice('A', 'FV24-0000001', '100.00', 'C1'), invoice('B', 'FV24-0000002', '250.00', 'C2'),
                 invoice('C', 'FV24-0000003', '75.25', 'C2')]
        rows = refnoise.reconcile(txn('325.25', 'TRANSFERENCIA ROCAVERDE PISCINAS S.L.'), context(items, CUSTOMERS))
        self.assertEqual({r['item_id'] for r in rows}, {'B', 'C'})

    def test_two_payers_with_exact_sets_abstain(self):
        items = [invoice('A', 'FV24-0000001', '100.00', 'C1'), invoice('B', 'FV24-0000002', '100.00', 'C2')]
        self.assertIsNone(refnoise.reconcile(txn('100.00', 'TRANSFERENCIA ROCAVERDE PISCINAS S.L.'),
                                             context(items, CUSTOMERS)))

    def test_ambiguous_amount_only_set_abstains(self):
        items = [invoice('A', 'FV24-0000001', '60.00'), invoice('B', 'FV24-0000002', '40.00'),
                 invoice('C', 'FV24-0000003', '30.00'), invoice('D', 'FV24-0000004', '70.00')]
        self.assertIsNone(refnoise.reconcile(txn('100.00', 'TRANSFERENCIA ROCAVERDE PISCINAS S.L.'),
                                             context(items, {'C1': CUSTOMERS['C1']})))

    def test_unnamed_payer_is_never_guessed(self):
        items = [invoice('A', 'FV24-0000001', '123.45')]
        self.assertIsNone(refnoise.reconcile(txn('123.45', 'TRANSFERENCIA RECIBIDA'), context(items, CUSTOMERS)))

    def test_money_amount_is_not_a_reference(self):
        self.assertNotIn('2798', ''.join(refnoise.reference_tokens(
            {'text': 'ACH CREDIT RMR*IV*324132*PI*2798.79\\', 'remittance_info': 'INV 324132'})))

    def test_split_concept_line_is_rejoined(self):
        tokens = refnoise.reference_tokens({'text': 'FACTURA FV24-0103585 0103691 0 | 103042 0103385'})
        self.assertIn('0103042', tokens)

    def test_fragment_list_names_one_invoice_per_token(self):
        # POLICIES 5.5: each paid invoice is quoted by a fragment (head 'RE2401', middle
        # '240105', tail '105621'); the list is repeated in the same field.
        import random
        rnd = random.Random(5)
        items = [invoice(f'X{k}', f'RE2401{k:05d}', f'{rnd.randint(10000, 170000) / 100:.2f}') for k in range(1000, 1080)]
        paid = [invoice('A', 'RE240105633', '1543.94'), invoice('B', 'RE240105621', '699.58'),
                invoice('C', 'RE240105475', '128.81'), invoice('D', 'RE240105515', '461.41'),
                invoice('E', 'RE240105155', '272.60'), invoice('F', 'RE240105262', '933.17')]
        text = 'SEPA-UEBERWEISUNG SVWZ+RE-NR RE2401 105621 105515 240105 105155  RE-NR RE2401 105621 105515 240105 105155'
        rows = refnoise.reconcile(txn('3106.34', 'ROCAVERDE PISCINAS S.L. ' + text), context(items + paid, CUSTOMERS))
        self.assertEqual({r['item_id'] for r in rows}, {'A', 'B', 'C', 'D', 'E'})
        refnoise.ASSIGN = False
        try:  # the open search alone cannot single the set out in this book
            self.assertIsNone(refnoise.reconcile(txn('3106.34', 'ROCAVERDE PISCINAS S.L. ' + text),
                                                 context(items + paid, CUSTOMERS)))
        finally:
            refnoise.ASSIGN = True
        self.assertEqual(refnoise.reference_sequences({'text': text}),
                         [('RE2401', '105621', '105515', '240105', '105155')])

    def test_transposed_number_of_another_customer_goes_to_named_payer(self):
        customers = {'C1': {'name': 'Wellness Jimenez S.Coop.', 'entity': 'ES01'},
                     'C9': {'name': 'Hoteles Prime S.L.', 'entity': 'ES01'}}
        items = [invoice('X', 'FV24-0115039', '880.00', 'C9'), invoice('Y', 'FV24-0115309', '541.60', 'C1'),
                 invoice('Z', 'FV24-0115310', '120.00', 'C1')]
        ctx = context(items, customers)
        t = txn('541.60', 'TRANSFERENCIAS WELLNESS JIMENEZ S.COO | WELLNESS J FRA. FV24-0115039')
        self.assertEqual([r['item_id'] for r in refnoise.typo_owner(t, ctx)], ['Y'])
        # The quoted invoice's own customer named, or its amount equal to the cash: no override.
        self.assertIsNone(refnoise.typo_owner(txn('541.60', 'TRANSF HOTELES PRIME S.L. FV24-0115039'), ctx))
        self.assertIsNone(refnoise.typo_owner(txn('880.00', 'WELLNESS JIMENEZ S.COO FV24-0115039'), ctx))

    def test_osa_distance_one(self):
        self.assertTrue(refnoise._osa1('1003740', '0103740'))
        self.assertTrue(refnoise._osa1('0133714', '0103714'))
        self.assertFalse(refnoise._osa1('0133715', '0103714'))


if __name__ == '__main__':
    unittest.main()
