import csv
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from recon.documents import DocumentMatcher, money, parse_advice, xlsx_rows


class DocumentTests(unittest.TestCase):
    def test_localized_money(self):
        self.assertEqual(money('1.234,56'), D('1234.56'))
        self.assertEqual(money('1,234.56'), D('1234.56'))
        self.assertEqual(money('-12,34'), D('-12.34'))

    def make_context(self, root):
        return SimpleNamespace(phase_dir=root, items={
            'ARI-1': {'item_id':'ARI-1','entity':'ES01','total_amount':D('100'), 'open_amount':D('100'), 'kind':'invoice'},
            'ARC-2': {'item_id':'ARC-2','entity':'ES01','total_amount':D('10'), 'open_amount':D('10'), 'kind':'credit'},
        }, by_number={'FV25001':['ARI-1'], 'AB25002':['ARC-2']}, payment_terms={})

    def test_group_dispute_credit_positive_and_no_state_mutation(self):
        with TemporaryDirectory() as d:
            root=Path(d); p=root/'remittance_advices';p.mkdir()
            (p/'index.csv').write_text('file,entity,payer_name,received_date,format\na.csv,ES01,Acme SA,2025-06-03,csv\n')
            (p/'a.csv').write_text('payment_ref;PAY-1234567;payment_date;2025-06-01;currency;EUR\ndocument;doc_date;gross;discount_or_deduction;reason;paid;your_order\nFV25-001;01/05/2025;100;-20;PRICE DIFF;80;\nAB25-002;01/05/2025;-10;;;-10;\nTOTAL;;;;;70;\n')
            ctx=self.make_context(root);matcher=DocumentMatcher(ctx)
            txn={'bank_txn_id':'BT1','entity':'ES01','booking_date':'2025-06-01','amount':'70','currency':'EUR','text':'AVISO PAY-1234567'}
            rows=matcher.reconcile(txn)
            self.assertEqual([(r['amount'],r['exception']) for r in rows], [('80.00','SHORT_DISPUTE'),('10.00','')])
            self.assertEqual(ctx.items['ARI-1']['open_amount'],D('100'))
            del ctx.by_number['AB25002']
            self.assertIsNone(matcher.reconcile(txn))
            ctx.by_number['AB25002']=['ARC-2']
            txn['amount']='71'
            self.assertIsNone(matcher.reconcile(txn))
            txn['amount']='70';txn['entity']='ES02'
            self.assertIsNone(matcher.reconcile(txn))

    def test_txt_deduction_is_applied(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'a.txt'
            p.write_text('Subject: Aviso de pago 123456789\nimporte de 80,00 EUR\n  FV25-001  01/05/25  100,00  -20,00 PRICE DIFF\n')
            a=parse_advice(p,{'entity':'ES01'})
            self.assertEqual(a.total,D('80'));self.assertEqual(a.reference,'123456789')

    def test_real_sample_formats(self):
        root=Path('data/raw/docs/sample/remittance_advices')
        if not root.exists():self.skipTest('event data unavailable')
        meta={'entity':'FR01'}
        x=next(root.glob('FR01/*.xlsx'))
        self.assertGreater(len(xlsx_rows(x)),3)
        self.assertGreater(len(parse_advice(x,meta).lines),0)

if __name__=='__main__':unittest.main()
