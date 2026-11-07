from collections import defaultdict
from decimal import Decimal
import unittest

from recon.context import Context
from recon.third_party import reconcile_confirming, reconcile_homonym_month


def context():
    ctx = object.__new__(Context)
    ctx.customers = {'C1': {'customer_id': 'C1', 'entity': 'ES01', 'name': 'Hoteles Navarra S.L.'},
                     'C2': {'customer_id': 'C2', 'entity': 'FR01', 'name': 'Hoteles Navarra SARL'}}
    ctx.items, ctx.by_party = {}, defaultdict(list)
    for iid, pid, entity, amount, due in (('ARI-1', 'C1', 'ES01', '1452.52', '2024-10-30'),
                                          ('ARI-2', 'C1', 'ES01', '300.00', '2024-10-01'),
                                          ('ARI-3', 'C2', 'FR01', '1452.52', '2024-09-01')):
        ctx.items[iid] = {'item_id': iid, 'party_id': pid, 'entity': entity, 'side': 'AR', 'kind': 'invoice',
                          'issue_date': '2024-09-01', 'due_date': due, 'currency': 'EUR',
                          'open_amount': Decimal(amount)}
        ctx.by_party[pid].append(iid)
    return ctx


def line(amount='1452.52', ref2='ORD. HOTELES NAV'):
    return {'bank_txn_id': 'B1', 'amount': amount, 'currency': 'EUR', 'entity': 'ES01', 'booking_date': '2024-10-14',
            'text': 'ABONO CONFIRMING CREDIFACIL COMERCIAL | EFC', 'ref2': ref2}


class ConfirmingTests(unittest.TestCase):
    def test_truncated_orderer_resolves_to_local_customer_invoice(self):
        rows = reconcile_confirming(line(), context())
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows], [('ARI-1', '1452.52', '')])

    def test_no_exact_amount_or_short_name_abstains(self):
        self.assertIsNone(reconcile_confirming(line(amount='1000.00'), context()))
        self.assertIsNone(reconcile_confirming(line(ref2='ORD. HOT'), context()))

    def test_other_receipts_untouched(self):
        txn = dict(line(), text='TRANSFERENCIA SEPA')
        self.assertIsNone(reconcile_confirming(txn, context()))


class HomonymMonthTests(unittest.TestCase):
    def ctx(self):
        ctx = object.__new__(Context)
        ctx.customers = {'U1': {'customer_id': 'U1', 'entity': 'US01', 'name': 'Ava Miller'},
                         'U2': {'customer_id': 'U2', 'entity': 'US01', 'name': 'Ava Miller'}}
        ctx.suppliers, ctx.previous_masters = {}, {}
        ctx.items, ctx.by_party = {}, defaultdict(list)
        for iid, pid, issued in (('ARI-7', 'U1', '2024-08-01'), ('ARI-8', 'U1', '2024-09-02'), ('ARI-9', 'U2', '2024-10-01')):
            ctx.items[iid] = {'item_id': iid, 'party_id': pid, 'entity': 'US01', 'side': 'AR', 'kind': 'invoice',
                              'issue_date': issued, 'due_date': issued, 'currency': 'USD', 'document_number': iid,
                              'open_amount': Decimal('1107.98')}
            ctx.by_party[pid].append(iid)
        return ctx

    def receipt(self, rem):
        return {'bank_txn_id': 'B9', 'amount': '1107.98', 'currency': 'USD', 'entity': 'US01', 'booking_date': '2024-10-04',
                'text': 'ACH CREDIT AVA MILLER CCD PAYMENT ID:8799273860', 'ref1': '', 'ref2': '',
                'remittance_info': rem, 'counterparty_name': 'Ava Miller'}

    def test_stated_month_picks_the_one_invoice_of_that_month(self):
        # POLICIES 5.6: monthly fees are invoiced at the start of the following month.
        rows = reconcile_homonym_month(self.receipt('SERVICE JULY 2024'), self.ctx())
        self.assertEqual([r['item_id'] for r in rows], ['ARI-7'])
        rows = reconcile_homonym_month(self.receipt('SERVICE SEPTEMBER 2024'), self.ctx())
        self.assertEqual([r['item_id'] for r in rows], ['ARI-9'])

    def test_no_month_abstains(self):
        self.assertIsNone(reconcile_homonym_month(self.receipt('PAYMENT'), self.ctx()))


if __name__ == '__main__':
    unittest.main()
