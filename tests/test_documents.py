import csv
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from xml.sax.saxutils import escape

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

    def test_split_reference_and_trade_name_payer(self):
        ctx=self.make_context(Path('.'))
        ctx.customers={'C1':{'entity':'ES01','name':'Bem-Estar Otero SA','trade_name':'Pedra Alta Jardins'}}
        matcher=self.matcher_without_files(ctx)
        matcher.payer_ids={('ES01','BEMESTAROTEROSA'):{'C1'}}
        advice=Advice('REM/2025/1591','ES01','Bem-Estar Otero SA','2025-01-13','EUR',
                      [Line('FV25-001',D(100),D(100))],'a.csv')
        matcher.by_ref['REM20251591']=[advice];matcher.by_amount[D(100)]=[advice]
        txn={'bank_txn_id':'BT1','entity':'ES01','booking_date':'2025-01-10','amount':'100','currency':'EUR',
             'text':'TRANSFERENCIA DE PEDRA ALTA JARDINS | CONCEPTO: VENDOR PMT 011025 REM/2025/1 | 591 | ORDENANTE X'}
        self.assertEqual(matcher.reconcile(txn)[0]['item_id'],'ARI-1')
        # Without the split reference, the trade name still confirms the payer.
        txn['text']='TRANSFERENCIA DE PEDRA ALTA JARDINS | CONCEPTO: VENDOR PMT'
        self.assertEqual(matcher.reconcile(txn)[0]['item_id'],'ARI-1')
        txn['text']='TRANSFERENCIA DE OTRA EMPRESA DISTINTA | CONCEPTO: VENDOR PMT'
        self.assertIsNone(matcher.reconcile(txn))

    def test_reasonless_deduction_classified_by_policy(self):
        ctx=self.make_context(Path('.'))
        ctx.payment_terms={'D2N30':{'early_payment_discount_pct':'2.00','discount_days':'10'}}
        ctx.customers={'CBR':{'country':'BR'},'CES':{'country':'ES'}}
        ctx.items['ARI-1'].update(payment_terms='D2N30',issue_date='2025-05-25',party_id='CES',side='AR')
        matcher=self.matcher_without_files(ctx)
        advice=Advice('PAY1234567','ES01','Acme Company','2025-06-03','EUR',[Line('FV25-001',D(100),D(98))],'a.csv')
        txn={'bank_txn_id':'BT1','entity':'ES01','booking_date':'2025-06-01','amount':'98','currency':'EUR'}
        rows=matcher._rows(txn,advice,True)
        self.assertEqual((rows[0]['amount'],rows[0]['exception']),('100.00','SHORT_DISCOUNT'))
        txn['booking_date']='2025-06-15'  # beyond 10 days + 3 grace: discount not accepted
        rows=matcher._rows(txn,advice,True)
        self.assertEqual((rows[0]['amount'],rows[0]['exception']),('98.00','PARTIAL'))
        # Withholding: only for MX/BR/MA customers at a policy rate.
        ctx.items['ARI-1'].update(payment_terms='T30',party_id='CBR')
        advice.lines=[Line('FV25-001',D(100),D('98.50'))];txn['amount']='98.50'
        rows=matcher._rows(txn,advice,True)
        self.assertEqual((rows[0]['amount'],rows[0]['exception']),('100.00','SHORT_WITHHOLDING'))
        ctx.items['ARI-1']['party_id']='CES'
        rows=matcher._rows(txn,advice,True)
        self.assertEqual((rows[0]['amount'],rows[0]['exception']),('98.50','PARTIAL'))

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

    def write_xlsx(self, path, rows):
        data=[]
        for n,row in enumerate(rows,1):
            cells=''.join(f'<c r="{chr(65+i)}{n}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>' for i,v in enumerate(row))
            data.append(f'<row r="{n}">{cells}</row>')
        xml='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(data)+'</sheetData></worksheet>'
        with zipfile.ZipFile(path,'w') as z:z.writestr('xl/worksheets/sheet1.xml',xml)

    def test_drift_layout_scoped_short_references_and_ambiguity(self):
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'remittance_advices';p.mkdir()
            (p/'index.csv').write_text('file,entity,payer_name,received_date,format\na.xlsx,ES01,Example Customer,2025-06-03,xlsx\n')
            self.write_xlsx(p/'a.xlsx',[
                ['Example Customer'],['Payment advice','PAY1234567','2025-06-01'],
                ['Voucher','Invoice','PO','Inv. date','Amount','Deduction','Net paid'],
                ['VCHR123','123','PO1','2025-05-01','100','-20','80'],['TOTAL','','','','','','80']])
            ctx=self.make_context(root)
            ctx.items={'ARI-1':{'item_id':'ARI-1','entity':'ES01','party_id':'C1','document_number':'FV25-0000123','issue_date':'2025-05-01','total_amount':D(100),'open_amount':D(100),'currency':'EUR','kind':'invoice'}}
            ctx.items['ARI-other']={**ctx.items['ARI-1'],'item_id':'ARI-other','party_id':'C2'}
            ctx.by_number={'FV250000123':['ARI-1','ARI-other']}
            ctx.customers={'C1':{'entity':'ES01','name':'Example Customer'}}
            matcher=DocumentMatcher(ctx)
            self.assertEqual(matcher.stats['changed_layout_files'],1)
            self.assertEqual(matcher.stats['short_reference_unresolved_rows'],0)
            txn={'bank_txn_id':'BT1','entity':'ES01','booking_date':'2025-06-01','amount':'80','currency':'EUR','text':'PAY1234567'}
            result=matcher.reconcile(txn)
            self.assertEqual(result[0]['item_id'],'ARI-1')
            self.assertEqual(result[0]['amount'],'80.00')
            self.assertEqual(result[0]['exception'],'PARTIAL') # No deduction reason supplied.
            ctx.items['ARI-ambiguous']={**ctx.items['ARI-1'],'item_id':'ARI-ambiguous','document_number':'FV25-0100123'}
            ctx.by_number['FV250100123']=['ARI-ambiguous']
            ambiguous=DocumentMatcher(ctx)
            self.assertEqual(ambiguous.stats['short_reference_ambiguous_rows'],1)
            self.assertIsNone(ambiguous.reconcile(txn))
            ctx.customers={}
            unidentified=DocumentMatcher(ctx)
            self.assertEqual(unidentified.stats['short_reference_unresolved_rows'],1)
            self.assertIsNone(unidentified.reconcile(txn))

    def test_empty_csv_is_skipped_without_crash(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'empty.csv';p.write_text('')
            self.assertIsNone(parse_advice(p,{}))

if __name__=='__main__':unittest.main()
