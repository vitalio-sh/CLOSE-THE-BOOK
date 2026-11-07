import unittest,tempfile,gzip,csv
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from recon.context import Context
from recon.validate import validate,FIELDS
from collections import defaultdict

class PipelineTests(unittest.TestCase):
    def test_balance_partial_return_duplicate(self):
        ctx=object.__new__(Context);ctx.items={'x':{'open_amount':Decimal('100')}};ctx.history=defaultdict(list)
        txn={'booking_date':'2026-01-01','account_id':'a'}
        def apply(amount,exc):ctx.apply(txn,[dict(item_id='x',amount=amount,exception=exc)])
        apply('40','PARTIAL');self.assertEqual(ctx.items['x']['open_amount'],Decimal('60'))
        apply('40','RETURNED');self.assertEqual(ctx.items['x']['open_amount'],Decimal('100'))
        apply('100','');apply('100','DUPLICATE_PAYMENT');self.assertEqual(ctx.items['x']['open_amount'],0)
    def test_validator_detects_coverage_and_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            with gzip.open(d/'bank_transactions.csv.gz','wt') as f:f.write('bank_txn_id\nt1\nt2\n')
            p=d/'answer.csv'
            with p.open('w') as f:
                w=csv.writer(f);w.writerow(FIELDS);w.writerow(['t1','','','BANK_FEES','']);w.writerow(['t1','','','BANK_FEES',''])
            result=validate(p,SimpleNamespace(phase_dir=d,items={},categories={'BANK_FEES'}))
            self.assertFalse(result['ok']);self.assertEqual(result['missing'],1)
            self.assertTrue(any('duplicate' in x for x in result['errors']))
if __name__=='__main__':unittest.main()
