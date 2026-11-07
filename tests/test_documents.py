import csv
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recon.documents import DocumentMatcher, Advice, Line, money, parse_advice, xlsx_rows
from collections import defaultdict


class DocumentTests(unittest.TestCase):
    def test_localized_money(self):
        self.assertEqual(money('1.234,56'), D('1234.56'))
        self.assertEqual(money('1,234.56'), D('1234.56'))
        self.assertEqual(money('-12,34'), D('-12.34'))

    def make_context(self, root):
        return SimpleNamespace(phase_dir=root, items={
            'ARI-1': {'item_id':'ARI-1','entity':'ES01','total_amount':D('100'), 'open_amount':D('100'), 'kind':'invoice','currency':'EUR'},
            'ARC-2': {'item_id':'ARC-2','entity':'ES01','total_amount':D('10'), 'open_amount':D('10'), 'kind':'credit','currency':'EUR'},
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

    def matcher_without_files(self, ctx):
        matcher=DocumentMatcher.__new__(DocumentMatcher)
        matcher.ctx=ctx;matcher.document_aliases={}
        matcher.by_ref=defaultdict(list);matcher.by_amount=defaultdict(list)
        return matcher

    def test_missing_currency_requires_verified_original_cash(self):
        ctx=self.make_context(Path('.'));ctx.items['ARI-1']['currency']='USD'
        matcher=self.matcher_without_files(ctx)
        advice=Advice('PAY1234567','ES01','Acme Company','2025-06-03','',
                      [Line('FV25-001',D(100),D(100))],'xlsx')
        txn={'bank_txn_id':'BT1','entity':'ES01','booking_date':'2025-06-01',
             'amount':'100','currency':'EUR','orig_amount':'120','orig_currency':'USD'}
        self.assertIsNone(matcher._rows(txn,advice,True))
        txn.update(amount='85',orig_amount='100')
        rows=matcher._rows(txn,advice,True)
        self.assertEqual(rows[0]['exception'],'FX_DIFFERENCE')
        self.assertEqual(rows[0]['amount'],'100.00')
        advice.currency='EUR'
        self.assertIsNone(matcher._rows(txn,advice,True))

    def test_explicit_reference_conflict_blocks_amount_fallback(self):
        ctx=self.make_context(Path('.'))
        ctx.items['ARI-2']={**ctx.items['ARI-1'],'item_id':'ARI-2','total_amount':D(110),'open_amount':D(110)}
        ctx.by_number['FV25003']=['ARI-2']
        matcher=self.matcher_without_files(ctx)
        first=Advice('PAYAAAAA','ES01','Acme Company','2025-06-03','EUR',
                     [Line('FV25-003',D(110),D(110))],'a.csv')
        second=Advice('PAYBBBBB','ES01','Acme Company','2025-06-03','EUR',
                      [Line('FV25-001',D(100),D(100))],'b.csv')
        matcher.by_ref['PAYAAAAA']=[first];matcher.by_amount[D(100)]=[second]
        txn={'bank_txn_id':'BT1','entity':'ES01','booking_date':'2025-06-01',
             'amount':'100','currency':'EUR','text':'Acme Company PAYAAAAA'}
        self.assertIsNone(matcher.reconcile(txn))
        txn['text']='Acme Company'
        self.assertEqual(matcher.reconcile(txn)[0]['item_id'],'ARI-1')

    def test_missing_pdf_tool_is_observable(self):
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'remittance_advices';p.mkdir()
            (p/'index.csv').write_text('file,entity,payer_name,received_date,format\na.pdf,ES01,Acme,2025-06-01,pdf\n')
            with patch('recon.documents.shutil.which',return_value=None):
                matcher=DocumentMatcher(self.make_context(root))
            self.assertFalse(matcher.stats['pdf_tool_available'])
            self.assertEqual(matcher.stats['skipped_pdfs'],1)
            self.assertEqual(matcher.stats['parsed'],0)
            self.assertEqual(matcher.stats['parse_errors'],0)
            self.assertTrue(matcher.stats['warnings'])

if __name__=='__main__':unittest.main()
