import csv
import io
import json
from collections import defaultdict
from decimal import Decimal
import unittest

from recon.context import Context
from recon.engine import execute, build_handlers
from recon.validate import FIELDS


class EngineTests(unittest.TestCase):
    def context(self):
        ctx = object.__new__(Context)
        ctx.items = {'ARI-2024-0000001': {'open_amount': Decimal('100'), 'issue_date': '2024-01-01'}}
        ctx.history = defaultdict(list)
        return ctx

    def test_order_rejection_fallback_and_single_application(self):
        ctx = self.context()
        txns = [dict(bank_txn_id='b', booking_date='2024-02-01', account_id='a'),
                dict(bank_txn_id='a', booking_date='2024-02-01', account_id='a')]
        observed = []
        def invalid(txn):
            return [dict(bank_txn_id=txn['bank_txn_id'], item_id='missing', amount='100')]
        def valid(txn):
            balance = ctx.items['ARI-2024-0000001']['open_amount']
            observed.append((txn['bank_txn_id'], balance))
            amount = '60' if txn['bank_txn_id'] == 'a' else str(balance)
            return [dict(bank_txn_id=txn['bank_txn_id'], item_id='ARI-2024-0000001', amount=amount,
                         category='', exception='PARTIAL' if txn['bank_txn_id'] == 'a' else '')]
        output, audit = io.StringIO(), io.StringIO()
        writer = csv.DictWriter(output, fieldnames=FIELDS); writer.writeheader()
        counts, rejected = execute(ctx, txns, [('bad', invalid), ('good', valid)], writer, audit)
        self.assertEqual(observed, [('a', Decimal(100)), ('b', Decimal(40))])
        self.assertEqual(ctx.items['ARI-2024-0000001']['open_amount'], 0)
        self.assertEqual(len(ctx.history['ARI-2024-0000001']), 2)
        self.assertEqual(rejected, {'bad': 2})
        self.assertEqual(counts, {'good': 2})
        self.assertEqual([json.loads(line)['bank_txn_id'] for line in audit.getvalue().splitlines()], ['a', 'b'])

    def test_empty_handlers_emit_unidentified(self):
        ctx = self.context()
        self.assertEqual(build_handlers(ctx, []), ([], None))
        output, audit = io.StringIO(), io.StringIO()
        writer = csv.DictWriter(output, fieldnames=FIELDS); writer.writeheader()
        counts, rejected = execute(ctx, [dict(bank_txn_id='t', booking_date='2024-01-01', account_id='a', amount='100')], [], writer, audit)
        self.assertEqual(list(csv.DictReader(io.StringIO(output.getvalue())))[0]['category'], 'UNIDENTIFIED')
        self.assertEqual(counts, {'unidentified': 1})
        self.assertFalse(rejected)


if __name__ == '__main__':
    unittest.main()
