import unittest
from types import SimpleNamespace
from decimal import Decimal as D
from recon.duplicates import ClosedDuplicates
from recon.matching import norm


def ctx_with(open_items=(), closed=()):
    items = {i['item_id']: i for i in open_items}
    by_party, by_number = {}, {}
    for i in open_items:
        by_party.setdefault(i['party_id'], []).append(i['item_id'])
        by_number.setdefault(norm(i['document_number']), []).append(i['item_id'])
    c = SimpleNamespace(items=items, by_party=by_party, by_number=by_number, history={},
                        customers={'C1': {'name': 'Alice Water Works', 'country': 'ES'}}, suppliers={},
                        recent_closed={'C1': list(closed)})

    def add_item(r):
        v = dict(r)
        v.update(party_id=r.get('customer_id') or r.get('supplier_id'), side='AR' if r['item_id'].startswith('AR') else 'AP',
                 kind='invoice', total_amount=D(r['total_amount']), open_amount=D(r['open_amount']))
        c.items[r['item_id']] = v
    c.add_item = add_item
    from collections import defaultdict
    c.by_party = defaultdict(list, c.by_party)
    c.by_number = defaultdict(list, c.by_number)
    return c


def closed(iid='ARI-2024-1', total='515.71', issue='2024-09-23', pm='transfer', entity='ES01'):
    return dict(item_id=iid, entity=entity, document_number='FV24-1', issue_date=issue, customer_id='C1',
                currency='EUR', total_amount=total, payment_method=pm)


def txn(amount='515.71', text='TRANSFERENCIA ALICE WATER WORKS PAGO'):
    return dict(bank_txn_id='T1', amount=amount, text=text, currency='EUR', entity='ES01', booking_date='2024-10-01')


class ClosedDuplicateTests(unittest.TestCase):
    def test_unique_recent_closed_total_is_duplicate(self):
        c = ctx_with(closed=[closed()])
        rows = ClosedDuplicates(c).reconcile(txn())
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows],
                         [('ARI-2024-1', '515.71', 'DUPLICATE_PAYMENT')])
        self.assertEqual(c.items['ARI-2024-1']['open_amount'], D(0))

    def test_item_settled_earlier_in_phase_paid_again(self):
        settled = dict(item_id='ARI-2024-7', document_number='FV24-7', party_id='C1', side='AR', kind='invoice',
                       open_amount=D('0'), total_amount=D('515.71'), currency='EUR', entity='ES01',
                       issue_date='2024-07-26', payment_method='transfer')
        rows = ClosedDuplicates(ctx_with([settled])).reconcile(txn())
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows],
                         [('ARI-2024-7', '515.71', 'DUPLICATE_PAYMENT')])
        # Ambiguous with a closed historical candidate, collected by direct debit, or too old.
        self.assertIsNone(ClosedDuplicates(ctx_with([settled], [closed()])).reconcile(txn()))
        self.assertIsNone(ClosedDuplicates(ctx_with([dict(settled, payment_method='dd')])).reconcile(txn()))
        self.assertIsNone(ClosedDuplicates(ctx_with([dict(settled, issue_date='2024-05-01')])).reconcile(txn()))
        # Still partly open: not settled, so not a duplicate.
        self.assertIsNone(ClosedDuplicates(ctx_with([dict(settled, open_amount=D('15.71'))])).reconcile(txn()))

    def test_guards(self):
        # Direct-debit receipt paid again by transfer: re-payment after a return, not a duplicate.
        self.assertIsNone(ClosedDuplicates(ctx_with(closed=[closed(pm='dd')])).reconcile(txn()))
        # Two closed candidates: ambiguous.
        self.assertIsNone(ClosedDuplicates(ctx_with(closed=[closed(), closed('ARI-2024-2')])).reconcile(txn()))
        # Too old.
        self.assertIsNone(ClosedDuplicates(ctx_with(closed=[closed(issue='2024-04-01')])).reconcile(txn()))
        # An open item of the payer has the same amount (monthly fee family).
        open_item = dict(item_id='ARI-2024-9', document_number='FV24-9', party_id='C1', side='AR', kind='invoice',
                         open_amount=D('515.71'), total_amount=D('515.71'), currency='EUR', entity='ES01')
        self.assertIsNone(ClosedDuplicates(ctx_with([open_item], [closed()])).reconcile(txn()))
        # Unknown payer, outgoing line for an AR invoice, other entity.
        self.assertIsNone(ClosedDuplicates(ctx_with(closed=[closed()])).reconcile(txn(text='PAYMENT')))
        self.assertIsNone(ClosedDuplicates(ctx_with(closed=[closed()])).reconcile(txn('-515.71')))
        self.assertIsNone(ClosedDuplicates(ctx_with(closed=[closed(entity='FR01')])).reconcile(txn()))


if __name__ == '__main__':
    unittest.main()
