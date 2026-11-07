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
        self.rows('direct_debit_batches',[dict(base,remittance_info='INV1',amount='100'),dict(base,remittance_info='MISSING',amount='50'),
                                          dict(base,remittance_info='MISSING2',amount='25')])
        # Most rows unknown: the exact batch total is not enough evidence.
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('175','BATCH12345')))
    def test_batch_keeps_resolvable_rows(self):
        self.item('ARI-1','INV1','100')
        base=dict(batch_id='BATCH12345',collection_date='2025-06-01',account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='M1',end_to_end_id='E1')
        self.rows('direct_debit_batches',[dict(base,remittance_info='INV1',amount='100'),dict(base,remittance_info='MISSING',amount='50')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('150','BATCH12345'))
        self.assertEqual([(r['item_id'],r['amount']) for r in result],[('ARI-1','100.00')])
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

    def test_return_cannot_precede_source_payment(self):
        self.item('API-1','INV1','100')
        self.rows('payment_runs',[dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-06-02',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Supplier',currency='EUR',transfer_amount='100',document_number='INV1',document_type='INVOICE',document_amount='100')])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('100','RETURN RUN12345678-1')))
        self.assertEqual(self.ctx.items['API-1']['open_amount'],D('100'))
    def test_final_cent_clears_balance(self):
        from collections import defaultdict
        from recon.context import Context
        self.item('API-1','INV1','100')
        self.ctx.items['API-1']['open_amount']=D('0.01')
        self.ctx.history=defaultdict(list)
        self.rows('payment_runs',[dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-06-01',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Supplier',currency='EUR',transfer_amount='0.01',document_number='INV1',document_type='INVOICE',document_amount='0.01')])
        txn=self.txn('-0.01','RUN12345678-1')
        rows=BatchMatcher(self.ctx).reconcile(txn)
        self.assertEqual(rows[0]['exception'],'')
        self.assertTrue(Context.proposal_valid(self.ctx,txn,rows))
        Context.apply(self.ctx,txn,rows)
        self.assertEqual(self.ctx.items['API-1']['open_amount'],D('0.00'))

    def test_group_reserves_fifo_balance_without_mutation(self):
        self.item('ARI-1','INV1','100')
        self.item('ARI-2','INV2','100')
        self.ctx.items['ARI-2']['issue_date']='2025-02-01'
        self.ctx.customers['C1']={'name':'Buyer'}
        base=dict(batch_id='BATCH12345',collection_date='2025-06-01',account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='M1',remittance_info='',amount='100')
        self.rows('direct_debit_batches',[dict(base,end_to_end_id='E1'),dict(base,end_to_end_id='E2')])
        rows=BatchMatcher(self.ctx).reconcile(self.txn('200','BATCH12345'))
        self.assertEqual({r['item_id']:r['amount'] for r in rows},{'ARI-1':'100.00','ARI-2':'100.00'})
        self.assertTrue(all(i['open_amount']==D('100') for i in self.ctx.items.values()))
    def test_explicit_fragments_can_aggregate_to_full_payment(self):
        self.item('API-1','INV1','100')
        base=dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-06-01',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Supplier',currency='EUR',transfer_amount='100',document_number='INV1',document_type='INVOICE')
        self.rows('payment_runs',[dict(base,document_amount='40'),dict(base,document_amount='60')])
        rows=BatchMatcher(self.ctx).reconcile(self.txn('-100','RUN12345678-1'))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['amount'],'100.00')
        self.assertEqual(rows[0]['exception'],'')
        self.assertEqual(self.ctx.items['API-1']['open_amount'],D('100'))

    def test_historical_dedup_preserves_original_ordered_row_identity(self):
        import shutil
        row=dict(settlement_id='SET12345',settlement_date='2025-06-01',account_id='ES01-A',currency='EUR',order_number='ORDER1',gross='100',net='98')
        self.rows('card_settlements',[row])
        history=self.root/'history';(history/'card_settlements').mkdir(parents=True)
        path=history/'card_settlements/ES01_202506.csv'
        shutil.copyfile(self.root/'card_settlements/ES01_202506.csv',path)
        self.ctx.history_dirs=[history]
        self.assertEqual(len(BatchMatcher(self.ctx).groups[0]['rows']),1)
        # The previous tuple(row.items()) key intentionally included CSV column
        # order; the memory optimization must not silently change dedup semantics.
        with path.open('w') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(reversed(row)))
            writer.writeheader();writer.writerow(row)
        self.assertEqual(len(BatchMatcher(self.ctx).groups[0]['rows']),2)

    def confirming_groups(self, amounts):
        rows=[]
        for n, amount in enumerate(amounts):
            self.item(f'API-{n}', f'INV{n}', str(amount), party=f'S{n}')
            rows.append(dict(run_id='CF12345678',run_type='confirming',execution_date='2025-05-01',debtor_account_id='ES01-A',end_to_end_id=f'CF12345678-{n}',beneficiary=f'Supplier {n}',currency='EUR',transfer_amount=str(amount),document_number=f'INV{n}',document_type='INVOICE',document_amount=str(amount)))
        self.rows('payment_runs',rows)

    def test_confirming_multi_beneficiary_unique_sum(self):
        self.confirming_groups([100, 210, 440, 900])
        # Different invoice due dates do not prevent the documented bank group.
        self.ctx.items['API-1']['due_date']='2025-08-01'
        result=BatchMatcher(self.ctx).reconcile(self.txn('-310','PAGO CONFIRMING REMESA CF12345678 2 PROVEEDORES'))
        self.assertEqual({r['item_id'] for r in result},{'API-0','API-1'})
        self.assertEqual(self.ctx.items['API-0']['open_amount'],D(100))

    def test_confirming_subset_ambiguous_rejected(self):
        self.confirming_groups([100, 200, 120, 180])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('-300','CF12345678 2 PROVEEDORES')))

    def test_confirming_subset_requires_reference_and_count(self):
        self.confirming_groups([100,210,440])
        matcher=BatchMatcher(self.ctx)
        for text in ('2 PROVEEDORES','CF12345678','CF12345678 3 PROVEEDORES'):
            self.assertIsNone(matcher.reconcile(self.txn('-310',text)))

    def test_confirming_subset_rejects_paid_or_unresolved_member(self):
        self.confirming_groups([100,210,440])
        self.ctx.items['API-1']['open_amount']=D(0)
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('-310','CF12345678 2 PROVEEDORES')))
        del self.ctx.items['API-1']
        self.ctx.by_number[norm('INV1')]=[]
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('-310','CF12345678 2 PROVEEDORES')))

    def test_confirming_subset_wrong_account_or_currency(self):
        self.confirming_groups([100,210,440])
        matcher=BatchMatcher(self.ctx)
        txn=self.txn('-310','CF12345678 2 PROVEEDORES')
        txn['account_id']='OTHER'
        self.assertIsNone(matcher.reconcile(txn))
        txn['account_id']='ES01-A';txn['currency']='USD'
        self.assertIsNone(matcher.reconcile(txn))

    def test_confirming_subset_uses_net_group_but_positive_credit_rows(self):
        self.item('API-1','INV1','100',party='S1')
        self.item('APC-1','CN1','20',party='S1')
        self.item('API-2','INV2','210',party='S2')
        base=dict(run_id='CF12345678',run_type='confirming',execution_date='2025-05-01',debtor_account_id='ES01-A',currency='EUR')
        self.rows('payment_runs',[
            dict(base,end_to_end_id='CF12345678-1',beneficiary='Supplier One',transfer_amount='80',document_number='INV1',document_type='INVOICE',document_amount='100'),
            dict(base,end_to_end_id='CF12345678-1',beneficiary='Supplier One',transfer_amount='80',document_number='CN1',document_type='CREDIT_NOTE',document_amount='-20'),
            dict(base,end_to_end_id='CF12345678-2',beneficiary='Supplier Two',transfer_amount='210',document_number='INV2',document_type='INVOICE',document_amount='210')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-290','CF12345678 2 PROVEEDORES'))
        self.assertEqual({r['item_id']:r['amount'] for r in result},{'API-1':'100.00','APC-1':'20.00','API-2':'210.00'})

    def maturity_txn(self, amount, text, day='2025-05-12'):
        txn=self.txn(amount,text); txn['booking_date']=day
        return txn

    def test_confirming_maturity_selects_transfers_due_today(self):
        # Nine-beneficiary style debit: transfers mature on the booking date (weekend rolls to Monday).
        self.confirming_groups([100, 210, 440, 900, 75])
        for iid, due in [('API-0','2025-05-10'),('API-1','2025-05-11'),('API-2','2025-05-12'),('API-3','2025-05-09'),('API-4','2025-05-26')]:
            self.ctx.items[iid]['due_date']=due; self.ctx.items[iid]['issue_date']='2025-03-01'
        result=BatchMatcher(self.ctx).reconcile(self.maturity_txn('-750','PAGO CONFIRMING | REMESA CF12345678 3 PROVEEDORES'))
        self.assertEqual({r['item_id'] for r in result},{'API-0','API-1','API-2'})
        self.assertTrue(all(r['exception']=='' for r in result))

    def test_confirming_maturity_drops_surplus_only_when_unique(self):
        self.confirming_groups([100, 210, 440, 900])
        for n in range(4):
            self.ctx.items[f'API-{n}']['due_date']='2025-05-12'; self.ctx.items[f'API-{n}']['issue_date']='2025-03-01'
        matcher=BatchMatcher(self.ctx)
        result=matcher.reconcile(self.maturity_txn('-1550','REMESA CF12345678 3 PROVEEDORES'))
        self.assertEqual({r['item_id'] for r in result},{'API-1','API-2','API-3'})
        # Count mismatch or wrong cash abstains.
        self.assertIsNone(matcher.reconcile(self.maturity_txn('-1549','REMESA CF12345678 3 PROVEEDORES')))

    def test_confirming_maturity_adds_unique_overdue_transfer(self):
        self.confirming_groups([100, 210, 440])
        for iid, due in [('API-0','2025-05-12'),('API-1','2025-04-01'),('API-2','2025-05-30')]:
            self.ctx.items[iid]['due_date']=due; self.ctx.items[iid]['issue_date']='2025-03-01'
        result=BatchMatcher(self.ctx).reconcile(self.maturity_txn('-310','REMESA CF12345678 2 PROVEEDORES'))
        self.assertEqual({r['item_id'] for r in result},{'API-0','API-1'})

    def test_confirming_maturity_skips_settled_transfer(self):
        self.confirming_groups([100, 210, 440])
        for n in range(3):
            self.ctx.items[f'API-{n}']['due_date']='2025-05-12'; self.ctx.items[f'API-{n}']['issue_date']='2025-03-01'
        self.ctx.items['API-2']['open_amount']=D(0)
        result=BatchMatcher(self.ctx).reconcile(self.maturity_txn('-310','REMESA CF12345678 2 PROVEEDORES'))
        self.assertEqual({r['item_id'] for r in result},{'API-0','API-1'})

    def test_next_business_day_target_calendar(self):
        from recon.batches import next_business_day
        self.assertEqual(next_business_day('2025-01-01'),'2025-01-02')
        self.assertEqual(next_business_day('2024-10-05'),'2024-10-07')
        self.assertEqual(next_business_day('2025-04-18'),'2025-04-22')
        self.assertEqual(next_business_day('2025-12-25'),'2025-12-29')

    def test_transfer_ids_repeating_across_accounts_stay_separate(self):
        # PR...-0118 exists for PT01 and UK01 runs: one transfer per paying account.
        self.item('API-1','F24/01582','6139.95',party='S1')
        self.item('API-2','24006741','623.80',party='S2',currency='GBP')
        base=dict(run_id='PR010024101092',run_type='transfers',execution_date='2025-06-01',end_to_end_id='PR010024101092-0118',beneficiary_iban='',document_type='INVOICE',remittance_info='')
        self.rows('payment_runs',[
            dict(base,debtor_account_id='ES01-A',beneficiary='Software Gil S.A.',currency='EUR',transfer_amount='6139.95',document_number='F24/01582',document_amount='6139.95'),
            dict(base,debtor_account_id='UK01-B',beneficiary='WC4 Consulting Limited',currency='GBP',transfer_amount='623.80',document_number='24006741',document_amount='623.80')])
        matcher=BatchMatcher(self.ctx)
        result=matcher.reconcile(self.txn('-6139.95','PAGO TRANSFERENCIAS | 0971 317899 000118'))
        self.assertEqual([(r['item_id'],r['amount']) for r in result],[('API-1','6139.95')])
        uk=self.txn('-623.80','FP WC4 CONSULTING',currency='GBP'); uk['account_id']='UK01-B'
        self.assertEqual([r['item_id'] for r in matcher.reconcile(uk)],['API-2'])

    def test_unique_subset_three_four_and_budget(self):
        from recon.batches import unique_small_subset
        self.assertEqual(unique_small_subset([100,210,440,900],750,3),(0,1,2))
        self.assertEqual(unique_small_subset([100,210,440,900,2000],1650,4),(0,1,2,3))
        self.assertIsNone(unique_small_subset([100,210,440,900],750,3,max_checks=0))
        self.assertIsNone(unique_small_subset([100,210,440,900],750,3,max_candidates=3))
        self.assertIsNone(unique_small_subset([1,2,3,4,5],9,3))

if __name__=='__main__':unittest.main()
