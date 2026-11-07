import unittest
from decimal import Decimal as D
from types import SimpleNamespace

from recon import namesakes
from recon.matching import norm, party_candidates


def context(items, customers):
    by_party, by_number = {}, {}
    for i in items:
        by_party.setdefault(i['party_id'], []).append(i['item_id'])
        by_number.setdefault(norm(i['document_number']), []).append(i['item_id'])
    return SimpleNamespace(items={i['item_id']: i for i in items}, by_party=by_party, by_number=by_number,
                           customers=customers, suppliers={}, accounts={}, history={}, payment_terms={})


def invoice(i, party, amount, issue):
    return dict(item_id=i, document_number='INV-' + i, party_id=party, side='AR', kind='invoice',
                open_amount=D(amount), total_amount=D(amount), currency='USD', entity='US01', issue_date=issue)


def txn(amount, text='ACH CREDIT NOAH EVANS CCD PAYMENT ID:111', **kw):
    return dict(bank_txn_id='T1', amount=amount, text=text, counterparty_name='Noah Evans', currency='USD',
                entity='US01', booking_date='2024-10-02', **kw)


NAMESAKES = {pid: {'name': 'Noah Evans', 'entity': 'US01', 'country': 'US'} for pid in ('C1', 'C2', 'C3')}


class NamesakeTests(unittest.TestCase):
    def test_short_namesakes_get_no_party_candidate(self):
        c = context([invoice('A', 'C1', '1202.78', '2024-08-01')], dict(NAMESAKES))
        self.assertEqual(party_candidates(txn('1202.78'), c), [])

    def test_single_namesake_with_exact_amount_pays_oldest(self):
        items = [invoice('A2', 'C2', '1202.78', '2024-09-02'), invoice('A1', 'C2', '1202.78', '2024-08-01'),
                 invoice('B1', 'C1', '500.00', '2024-08-01'), invoice('F1', 'C2', '1202.78', '2024-10-05')]
        r = namesakes.reconcile(txn('1202.78'), context(items, dict(NAMESAKES)))
        self.assertEqual(r, [dict(bank_txn_id='T1', item_id='A1', amount='1202.78', category='', exception='')])

    def test_two_namesakes_with_exact_amount_abstain(self):
        items = [invoice('A1', 'C2', '1202.78', '2024-08-01'), invoice('B1', 'C3', '1202.78', '2024-09-02')]
        self.assertIsNone(namesakes.reconcile(txn('1202.78'), context(items, dict(NAMESAKES))))

    def test_no_exact_amount_or_outgoing_or_unique_name_abstain(self):
        items = [invoice('A1', 'C2', '1202.78', '2024-08-01')]
        self.assertIsNone(namesakes.reconcile(txn('1200.00'), context(items, dict(NAMESAKES))))
        self.assertIsNone(namesakes.reconcile(txn('-1202.78'), context(items, dict(NAMESAKES))))
        unique = {'C2': NAMESAKES['C2']}
        self.assertIsNone(namesakes.reconcile(txn('1202.78'), context(items, unique)))


if __name__ == '__main__':
    unittest.main()
