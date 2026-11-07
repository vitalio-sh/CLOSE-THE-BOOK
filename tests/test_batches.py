import csv
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from recon.batches import BatchMatcher, norm

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.ctx=SimpleNamespace(phase_dir=self.root, items={},by_number={},by_party={},customers={},suppliers={},accounts={})
    def item(self,iid,number,amount,party='C1',currency='EUR'):
        item=dict(item_id=iid,document_number=number,total_amount=D(amount),open_amount=D(amount),party_id=party,entity='ES01',currency=currency,side=iid[:2],issue_date='2025-01-01',due_date='2025-06-01')
        self.ctx.items[iid]=item;self.ctx.by_number.setdefault(norm(number),[]).append(iid)
        self.ctx.by_party.setdefault(party,[]).append(iid)
    def rows(self,folder,rows):
        dest=self.root/folder;dest.mkdir();p=dest/'ES01_202506.csv'
        with p.open('w') as f:
            w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    def txn(self,amt,text='',currency='EUR'):
        return dict(bank_txn_id='BT1',account_id='ES01-A',amount=amt,currency=currency,booking_date='2025-06-01',text=text)
    def test_card_gross_no_fee_exception(self):
        self.item('ARI-1','ORDER1','100')
        self.rows('card_settlements',[dict(settlement_id='SET12345',settlement_date='2025-06-01',account_id='ES01-A',currency='EUR',order_number='ORDER1',gross='100',net='98')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('98','SET12345'))
        self.assertEqual(result[0]['amount'],'100.00');self.assertEqual(result[0]['exception'],'')
        self.assertEqual(self.ctx.items['ARI-1']['open_amount'],D(100))
    def test_credit_note_positive(self):
        self.item('API-1','INV1','100');self.item('APC-1','CN1','20')
        base=dict(run_id='RUN12345',run_type='transfers',execution_date='2025-06-01',debtor_account_id='ES01-A',end_to_end_id='RUN12345-1',beneficiary='Supplier',currency='EUR',transfer_amount='80')
        self.rows('payment_runs',[dict(base,document_number='INV1',document_type='INVOICE',document_amount='100'),dict(base,document_number='CN1',document_type='CREDIT_NOTE',document_amount='-20')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-80','RUN12345-1'))
        self.assertEqual({r['item_id']:r['amount'] for r in result},{'API-1':'100.00','APC-1':'20.00'})
    def test_incomplete_group_rejected(self):
        self.item('ARI-1','INV1','100')
        base=dict(batch_id='BATCH12345',collection_date='2025-06-01',account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='M1',end_to_end_id='E1')
        self.rows('direct_debit_batches',[dict(base,remittance_info='INV1',amount='100'),dict(base,remittance_info='MISSING',amount='50')])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('150','BATCH12345')))
    def test_wrong_direction_rejected(self):
        self.item('ARI-1','ORDER1','100')
        self.rows('card_settlements',[dict(settlement_id='SET12345',settlement_date='2025-06-01',account_id='ES01-A',currency='EUR',order_number='ORDER1',gross='100',net='98')])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('-98','SET12345')))
    def test_partial_amount(self):
        self.item('API-1','INV1','100')
        self.rows('payment_runs',[dict(run_id='RUN12345',run_type='transfers',execution_date='2025-06-01',debtor_account_id='ES01-A',end_to_end_id='RUN12345-1',beneficiary='Supplier',currency='EUR',transfer_amount='60',document_number='INV1',document_type='INVOICE',document_amount='60')])
        self.assertEqual(BatchMatcher(self.ctx).reconcile(self.txn('-60','RUN12345-1'))[0]['exception'],'PARTIAL')

    def test_direct_debit_suffix_reference(self):
        self.item('ARI-1','RE250207788','100')
        self.item('ARI-2','RE250210182','100')
        self.ctx.customers['C1']={'name':'Buyer'}
        self.rows('direct_debit_batches',[dict(batch_id='BATCH12345',collection_date='2025-06-01',account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='M1',end_to_end_id='E1',remittance_info='RECIBO 207788',amount='100')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('100','BATCH12345'))
        self.assertEqual(result[0]['item_id'],'ARI-1')
    def test_confirming_delayed_single_transfer(self):
        self.item('API-1','INV1','100')
        self.rows('payment_runs',[dict(run_id='CF12345678',run_type='confirming',execution_date='2025-05-01',debtor_account_id='ES01-A',end_to_end_id='CF12345678-1',beneficiary='Supplier',currency='EUR',transfer_amount='100',document_number='INV1',document_type='INVOICE',document_amount='100')])
        self.assertEqual(BatchMatcher(self.ctx).reconcile(self.txn('-100','CF12345678'))[0]['item_id'],'API-1')
    def test_supplier_transfer_return(self):
        self.item('API-1','INV1','100')
        self.rows('payment_runs',[dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-05-15',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Supplier',currency='EUR',transfer_amount='100',document_number='INV1',document_type='INVOICE',document_amount='100')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('100','DEVOLUCION TRANSFERENCIA RUN12345678-1'))
        self.assertEqual(result[0]['exception'],'RETURNED')

if __name__=='__main__':unittest.main()
