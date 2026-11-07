import unittest
from collections import defaultdict
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

from recon.return_groups import reconcile_return_group


def ctx_with_group(amounts=('60.00', '40.00')):
    items, history = {}, defaultdict(list)
    for n, amount in enumerate(amounts, 1):
        iid = f'ARI-{n}'
        items[iid] = dict(item_id=iid, document_number=f'FV-{n}', party_id='C1', side='AR', kind='invoice',
                          currency='EUR', entity='ES01', issue_date='2025-01-01',
                          total_amount=D(amount), open_amount=D(0))
        history[iid].append(dict(bank_txn_id='BT-PAY', item_id=iid, amount=amount, exception='',
                                 booking_date='2025-01-10', account_id='A1'))
    return SimpleNamespace(items=items, history=history, by_number={}, by_party={'C1': list(items)})


def ret(amount='-100.00', text='DEVOLUCION RECIBO CUSTOMER', account='A1'):
    return dict(bank_txn_id='BT-RET', amount=amount, text=text, currency='EUR', entity='ES01',
                account_id=account, booking_date='2025-01-15')


@patch('recon.return_groups.party_candidates', lambda txn, ctx: ['C1'])
class ReturnGroupTests(unittest.TestCase):
    def test_whole_prior_group_returned(self):
        rows = reconcile_return_group(ret(), ctx_with_group())
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows],
                         [('ARI-1', '60.00', 'RETURNED'), ('ARI-2', '40.00', 'RETURNED')])

    def test_requires_return_text_cash_and_account(self):
        self.assertIsNone(reconcile_return_group(ret(text='TRANSFER CUSTOMER'), ctx_with_group()))
        self.assertIsNone(reconcile_return_group(ret(amount='-99.00'), ctx_with_group()))
        self.assertIsNone(reconcile_return_group(ret(account='A2'), ctx_with_group()))

    def test_single_item_left_to_matching(self):
        self.assertIsNone(reconcile_return_group(ret(amount='-60.00'), ctx_with_group(('60.00',))))

    def test_already_returned_group_is_ambiguous(self):
        ctx = ctx_with_group()
        ctx.history['ARI-1'].append(dict(bank_txn_id='BT-R0', item_id='ARI-1', amount='60.00',
                                         exception='RETURNED', booking_date='2025-01-12', account_id='A1'))
        self.assertIsNone(reconcile_return_group(ret(), ctx))


if __name__ == '__main__':
    unittest.main()
