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

    def test_card_chargeback_masked_card_and_merchant(self):
        # POLICIES 3 RETURNED: a card chargeback re-opens the order behind card + merchant + gross.
        self.item('ARI-1','WEB249002496','189.79')
        self.ctx.items['ARI-1']['open_amount']=D('0')
        self.rows('card_settlements',[dict(settlement_id='0427240813',acquirer='PayNexo',merchant_id='993110427',settlement_date='2025-05-13',account_id='ES01-A',currency='EUR',order_number='WEB249002496',card='****1159',auth_code='1',operation_date='2025-05-12',gross='189.79',fee='1.71',net='188.08'),
                                      dict(settlement_id='0427240814',acquirer='PayNexo',merchant_id='993110427',settlement_date='2025-05-14',account_id='ES01-A',currency='EUR',order_number='WEB249002497',card='****7006',auth_code='2',operation_date='2025-05-13',gross='189.79',fee='1.71',net='188.08')])
        result=BatchMatcher(self.ctx).reconcile(dict(self.txn('-189.79','CHARGEBACK ****1159 MID 993110427'),entity='ES01'))
        self.assertEqual([(r['item_id'],r['amount'],r['exception']) for r in result],[('ARI-1','189.79','RETURNED')])
    def test_card_chargeback_needs_matching_gross(self):
        self.item('ARI-1','WEB249002496','189.79')
        self.rows('card_settlements',[dict(settlement_id='0427240813',acquirer='PayNexo',merchant_id='993110427',settlement_date='2025-05-13',account_id='ES01-A',currency='EUR',order_number='WEB249002496',card='****1159',auth_code='1',operation_date='2025-05-12',gross='189.79',fee='1.71',net='188.08')])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(dict(self.txn('-50.00','CHARGEBACK ****1159 MID 993110427'),entity='ES01')))
    def test_supplier_transfer_return_named_beneficiary(self):
        # A rejected transfer that quotes only the beneficiary still re-opens the paid documents.
        self.item('API-1','INV1','100',party='S1');self.item('APC-1','CN1','20',party='S1')
        base=dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-05-10',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Franco Logistica S.A.',currency='EUR',transfer_amount='80')
        self.rows('payment_runs',[dict(base,document_number='INV1',document_type='INVOICE',document_amount='100'),dict(base,document_number='CN1',document_type='CREDIT_NOTE',document_amount='-20')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('80','DEVOLUCION TRANSFERENCIA AC04 | FRANCO LOGISTICA S.A.'))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in result},{('API-1','100.00','RETURNED'),('APC-1','20.00','RETURNED')})
    def test_named_return_without_return_words_ignored(self):
        self.item('API-1','INV1','100',party='S1')
        self.rows('payment_runs',[dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-05-10',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Franco Logistica S.A.',currency='EUR',transfer_amount='100',document_number='INV1',document_type='INVOICE',document_amount='100')])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('100','TRANSFERENCIA FRANCO LOGISTICA S.A.')))
    def test_md06_refund_skips_collections_of_last_three_weeks(self):
        # MD06 = debtor refund request, weeks after the debit: the latest collection is the next one.
        self.item('ARI-1','INV1','50');self.item('ARI-2','INV2','50')
        self.ctx.items['ARI-1']['open_amount']=D('0');self.ctx.items['ARI-2']['open_amount']=D('0')
        base=dict(account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='VTM-DE-0001477',amount='50')
        self.rows('direct_debit_batches',[dict(base,batch_id='B20250401',collection_date='2025-04-01',end_to_end_id='E1',remittance_info='INV1'),
                                          dict(base,batch_id='B20250520',collection_date='2025-05-20',end_to_end_id='E2',remittance_info='INV2')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT MD06 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-1','RETURNED')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT AM04 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-2','RETURNED')])
    def test_supplier_refund_is_not_a_named_transfer_return(self):
        self.item('API-1','INV1','100',party='S1')
        self.rows('payment_runs',[dict(run_id='RUN12345678',run_type='transfers',execution_date='2025-05-10',debtor_account_id='ES01-A',end_to_end_id='RUN12345678-1',beneficiary='Valves Parker Co., Ltd.',currency='EUR',transfer_amount='100',document_number='INV1',document_type='INVOICE',document_amount='100')])
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('100','TT IN VALVES PARKER CO., LTD. DEVOLUCION SALDO A FAVOR')))
    def _dd_three_collections(self, extra=()):
        for iid, number in (('ARI-1','INV1'),('ARI-2','INV2'),('ARI-3','INV3')):
            self.item(iid,number,'50');self.ctx.items[iid]['open_amount']=D('0')
        base=dict(account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='VTM-DE-0001477',amount='50',re_presentation='N')
        self.rows('direct_debit_batches',[dict(base,batch_id='B20250401',collection_date='2025-04-01',end_to_end_id='E1',remittance_info='INV1'),
                                          dict(base,batch_id='B20250502',collection_date='2025-05-02',end_to_end_id='E2',remittance_info='INV2'),
                                          dict(base,batch_id='B20250527',collection_date='2025-05-27',end_to_end_id='E3',remittance_info='INV3')]
                                         + [dict(base, **row) for row in extra])
    def test_md06_refund_returns_collection_of_its_delay_window(self):
        # POLICIES 3 RETURNED: MD06 is a refund request weeks after the debit (TRAIN 21-75 days);
        # the debit of the last three weeks is the next collection of the mandate.
        self._dd_three_collections()
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT MD06 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-2','RETURNED')])
    def test_short_reason_code_returns_latest_collection(self):
        self._dd_three_collections()
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT AM04 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-3','RETURNED')])
    def test_re_presentation_names_the_returned_collection(self):
        # POLICIES 5.10: the later batch row flagged re_presentation=Y names the returned invoice.
        self._dd_three_collections(extra=[dict(batch_id='B20250620',collection_date='2025-06-20',end_to_end_id='E4',
                                               remittance_info='INV1',re_presentation='Y')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT MD06 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-1','RETURNED')])
    def test_re_presentation_printed_without_prefix(self):
        # The re-presented row may print 'RECIBO 0000001' for the collected 'INV0000001'.
        for iid, number in (('ARI-1','INV0000001'),('ARI-2','INV0000002')):
            self.item(iid,number,'50');self.ctx.items[iid]['open_amount']=D('0')
        base=dict(account_id='ES01-A',currency='EUR',debtor_name='Buyer',mandate_id='VTM-DE-0001477',amount='50',re_presentation='N')
        self.rows('direct_debit_batches',[dict(base,batch_id='B20250401',collection_date='2025-04-10',end_to_end_id='E1',remittance_info='INV0000001'),
                                          dict(base,batch_id='B20250502',collection_date='2025-05-02',end_to_end_id='E2',remittance_info='INV0000002'),
                                          dict(base,batch_id='B20250620',collection_date='2025-06-20',end_to_end_id='E3',remittance_info='RECIBO 0000001',re_presentation='Y')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT MD06 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-1','RETURNED')])
    def test_re_presentation_outside_horizon_ignored(self):
        self._dd_three_collections(extra=[dict(batch_id='B20250901',collection_date='2025-09-01',end_to_end_id='E4',
                                               remittance_info='INV1',re_presentation='Y')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-50','RUECKLASTSCHRIFT MD06 MREF+VTM-DE-0001477'))
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('ARI-2','RETURNED')])

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

    def test_confirming_without_run_single_invoice_due_today(self):
        self.item('API-1','INV1','364.68',party='S1'); self.item('API-2','INV2','364.68',party='S2')
        self.ctx.items['API-1'].update(due_date='2025-05-10',issue_date='2025-03-01',side='AP',kind='invoice')
        self.ctx.items['API-2'].update(due_date='2025-05-20',issue_date='2025-03-01',side='AP',kind='invoice')
        txn=self.maturity_txn('-364.68','PAGO CONFIRMING | REMESA CF000024112560 1 PROVEEDORES'); txn['entity']='ES01'
        self.assertEqual([r['item_id'] for r in BatchMatcher(self.ctx).reconcile(txn)],['API-1'])
        # A second invoice with the same amount and maturity abstains; so do several beneficiaries.
        self.ctx.items['API-2']['due_date']='2025-05-12'
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))
        self.ctx.items['API-2']['due_date']='2025-05-20'
        txn['text']='PAGO CONFIRMING | REMESA CF000024112560 2 PROVEEDORES'
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))

    def test_returned_supplier_transfer_reverses_original_allocation(self):
        self.item('API-1','FAC1','6627.11',party='S1'); self.item('API-2','FAC2','7003.77',party='S1')
        self.item('API-3','FAC3','13630.88',party='S2')
        self.ctx.suppliers={'S1':{'name':'Franco Logística S.A.'},'S2':{'name':'Otro Proveedor S.L.'}}
        for iid in ('API-1','API-2'): self.ctx.items[iid]['open_amount']=D(0)
        self.ctx.items['API-3']['open_amount']=D(0)
        self.ctx.history={
            'API-1':[dict(bank_txn_id='BT0',item_id='API-1',amount='6627.11',exception='',booking_date='2025-05-20',account_id='ES01-A')],
            'API-2':[dict(bank_txn_id='BT0',item_id='API-2',amount='7003.77',exception='',booking_date='2025-05-20',account_id='ES01-A')],
            'API-3':[dict(bank_txn_id='BT9',item_id='API-3',amount='13630.88',exception='',booking_date='2025-05-21',account_id='ES01-A')]}
        txn=self.txn('13630.88','DEVOLUCION TRANSFERENCIA AC04 | FRANCO LOGISTICA S.A.')
        result=BatchMatcher(self.ctx).reconcile(txn)
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in result},
                         {('API-1','6627.11','RETURNED'),('API-2','7003.77','RETURNED')})
        # Unnamed supplier, other account or already returned: abstain.
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('13630.88','DEVOLUCION TRANSFERENCIA AC04 | DESCONOCIDO')))
        other=dict(txn,account_id='ES01-B'); self.assertIsNone(BatchMatcher(self.ctx).reconcile(other))
        self.ctx.history['API-1'].append(dict(bank_txn_id='BT5',item_id='API-1',amount='6627.11',exception='RETURNED',booking_date='2025-05-25',account_id='ES01-A'))
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))

    def test_single_factor_row_prefers_the_open_document_over_settled_namesakes(self):
        # 'FV-24-0061' was printed by three MX01 suppliers; two of those documents are settled.
        self.item('API-1','FV-24-0061','20190.30',party='S1'); self.item('API-2','FV-24-0061','19440.94',party='S2')
        self.item('API-3','FV-24-0061','51002.52',party='S3')
        for iid in ('API-2','API-3'): self.ctx.items[iid].update(open_amount=D(0),historical_closed=True)
        base=dict(run_id='PR010024101009',run_type='transfers',execution_date='2025-06-01',debtor_account_id='ES01-A',end_to_end_id='PR010024101009-0001',beneficiary='Credifacil Comercial EFC',beneficiary_iban='',currency='EUR',transfer_amount='6057.09',document_type='INVOICE',remittance_info='')
        self.rows('payment_runs',[dict(base,document_number='FV-24-0061',document_amount='6057.09')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-6057.09','SPEI ENVIADO CREDIFACIL COMERCIAL EFC CONCEPTO FRA FV-24-0061'))
        self.assertEqual([(r['item_id'],r['amount'],r['exception']) for r in result],[('API-1','6057.09','PARTIAL')])
        # Two open namesakes that both cover the row stay ambiguous.
        self.ctx.items['API-2'].update(open_amount=D('19440.94'),historical_closed=False)
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('-6057.09','SPEI ENVIADO CREDIFACIL COMERCIAL EFC CONCEPTO FRA FV-24-0061')))

    def test_factor_transfer_partial_row_resolved_within_supplier(self):
        # Factor-named transfer: INV0003340 is printed by two suppliers and paid partially.
        self.item('API-1','INV0003366','4404.19',party='S1'); self.item('API-2','INV0003340','3000.00',party='S1')
        self.item('API-3','INV0003340','2500.00',party='S2')
        base=dict(run_id='PR010024101002',run_type='transfers',execution_date='2025-06-01',debtor_account_id='ES01-A',end_to_end_id='PR010024101002-0310',beneficiary='Norte Pagos Confirming EFC',beneficiary_iban='',currency='EUR',transfer_amount='5531.44',document_type='INVOICE',remittance_info='')
        self.rows('payment_runs',[dict(base,document_number='INV0003366',document_amount='4404.19'),
                                  dict(base,document_number='INV0003340',document_amount='1127.25')])
        result=BatchMatcher(self.ctx).reconcile(self.txn('-5531.44','ACH DEBIT NORTE PAGOS CONF VENDOR PMT'))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in result},
                         {('API-1','4404.19',''),('API-2','1127.25','PARTIAL')})

    def test_returned_transfer_named_beneficiary_beyond_three_days(self):
        self.item('API-1','FV-24-0296','29259.78',party='S1'); self.item('API-2','FV-24-0307','3013.53',party='S1')
        for iid in ('API-1','API-2'): self.ctx.items[iid].update(open_amount=D(0),historical_closed=True)
        base=dict(run_id='PR010024122645',run_type='transfers',execution_date='2025-05-20',debtor_account_id='ES01-A',end_to_end_id='PR010024122645-0396',beneficiary='Motors Coral Bay North Pty Ltd',beneficiary_iban='',currency='EUR',transfer_amount='32273.31',document_type='INVOICE',remittance_info='')
        self.rows('payment_runs',[dict(base,document_number='FV-24-0296',document_amount='29259.78'),
                                  dict(base,document_number='FV-24-0307',document_amount='3013.53')])
        matcher=BatchMatcher(self.ctx)
        result=matcher.reconcile(self.txn('32273.31','RETURN AC01 MOTORS CORAL BAY NORTH PTY LTD'))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in result},
                         {('API-1','29259.78','RETURNED'),('API-2','3013.53','RETURNED')})
        self.assertIsNone(matcher.reconcile(self.txn('32273.31','RETURN AC01 OTHER SUPPLIER LTD')))
        # A supplier refund naming the beneficiary is not a returned transfer.
        self.assertIsNone(matcher.reconcile(self.txn('32273.31','TT IN MOTORS CORAL BAY NORTH PTY LTD DEVOLUCION SALDO A FAVOR')))

    def test_transfer_run_dated_after_the_booking_is_not_this_line(self):
        self.item('API-1','F24/03225','216.97',party='S1')
        base=dict(run_id='PR010024102542',run_type='transfers',execution_date='2025-06-03',debtor_account_id='ES01-A',end_to_end_id='PR010024102542-0077',beneficiary='MR3 Recambios S.Coop.',beneficiary_iban='',currency='EUR',transfer_amount='216.97',document_type='INVOICE',remittance_info='',document_number='F24/03225',document_amount='216.97')
        self.rows('payment_runs',[base])
        # A supplier direct debit two days before the run executes is not that transfer.
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(self.txn('-216.97','RECAMBIOS FUENTE | RECIBO 2024/00073')))
        txn=self.txn('-216.97','TRANSFERENCIA A MR3 RECAMBIOS S.COOP. | FRA F24/03225'); txn['booking_date']='2025-06-03'
        self.assertEqual([r['item_id'] for r in BatchMatcher(self.ctx).reconcile(txn)],['API-1'])

    def test_cut_run_reference_flags_duplicate_payment(self):
        self.item('API-1','FV-24-0100','2844.12',party='S1'); self.item('API-2','A9','500.00',party='S2')
        self.ctx.items['API-1'].update(open_amount=D(0),historical_closed=True)
        base=dict(run_id='PR010024091060',run_type='transfers',execution_date='2025-05-01',debtor_account_id='ES01-A',beneficiary='Proveedor Uno S.L.',beneficiary_iban='',currency='EUR',document_type='INVOICE',remittance_info='')
        self.rows('payment_runs',[dict(base,end_to_end_id='PR010024091060-0109',transfer_amount='2844.12',document_number='FV-24-0100',document_amount='2844.12'),
                                  dict(base,end_to_end_id='PR010024091060-0001',transfer_amount='500.00',document_number='A9',document_amount='500.00')])
        # The trailing '000001' of the text is not the transfer sequence.
        txn=self.txn('-2844.12','PAGO TRANSFERENCIAS | 4799-56-1122872-73 051775 000001'); txn['ref2']='PR010024091060-0'
        result=BatchMatcher(self.ctx).reconcile(txn)
        self.assertEqual([(r['item_id'],r['exception']) for r in result],[('API-1','DUPLICATE_PAYMENT')])
        # Without the cut run reference the 31-day-old transfer is not an amount candidate.
        txn['ref2']=''
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))

    def test_cut_run_reference_tie_abstains(self):
        self.item('API-1','FV-24-0100','2844.12',party='S1'); self.item('API-2','FV-24-0101','2844.12',party='S2')
        for iid in ('API-1','API-2'): self.ctx.items[iid].update(open_amount=D(0),historical_closed=True)
        base=dict(run_id='PR010024091060',run_type='transfers',execution_date='2025-05-01',debtor_account_id='ES01-A',beneficiary='Proveedor',beneficiary_iban='',currency='EUR',transfer_amount='2844.12',document_type='INVOICE',remittance_info='',document_amount='2844.12')
        self.rows('payment_runs',[dict(base,end_to_end_id='PR010024091060-0109',document_number='FV-24-0100'),
                                  dict(base,end_to_end_id='PR010024091060-0110',document_number='FV-24-0101')])
        txn=self.txn('-2844.12','PAGO TRANSFERENCIAS | 4799 051775 000001'); txn['ref2']='PR010024091060-0'
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))

    def test_transfer_run_prefix(self):
        from recon.history import transfer_run_prefix
        self.assertEqual(transfer_run_prefix({'ref2':'PR010024101092-0'}),('PR010024101092','PR0100241010920'))
        self.assertEqual(transfer_run_prefix({'ref1':'PR010024101009-0','ref2':'ME242841120091'}),('PR010024101009','PR0100241010090'))
        self.assertEqual(transfer_run_prefix({'ref2':'PR010024101092-0118'}),('',''))
        self.assertEqual(transfer_run_prefix({'ref2':'NO242842873273'}),('',''))

    def test_supplier_direct_debit_truncated_name_and_factory(self):
        self.item('API-1','INV0001012','822.95',party='S1'); self.item('API-2','INV9','822.95',party='S2')
        for iid in ('API-1','API-2'): self.ctx.items[iid].update(payment_method='dd',entity='ES02')
        self.ctx.suppliers={'S1':{'name':'PM8 Mantenimiento S.A.'},'S2':{'name':'Otro Mantenimiento S.L.'}}
        txn=self.txn('-822.95','PM8 MANTENIMIENT | Recibos varios'); txn.update(ref2='248084923622',entity='ES00')
        self.assertEqual([r['item_id'] for r in BatchMatcher(self.ctx).reconcile(txn)],['API-1'])
        # Transfer invoices, other entities and ambiguous names abstain.
        self.ctx.items['API-1']['payment_method']='transfer'
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))
        self.ctx.items['API-1']['payment_method']='dd'; txn['entity']='FR01'
        self.assertIsNone(BatchMatcher(self.ctx).reconcile(txn))
        # An opening-snapshot invoice has no payment method; the supplier master's 'dd' applies.
        txn['entity']='ES00'
        for iid in ('API-1','API-2'): self.ctx.items[iid].pop('payment_method')
        self.ctx.suppliers['S1']['payment_method']='dd'; self.ctx.suppliers['S2']['payment_method']='transfer'
        self.assertEqual([r['item_id'] for r in BatchMatcher(self.ctx).reconcile(txn)],['API-1'])

    def test_unique_subset_three_four_and_budget(self):
        from recon.batches import unique_small_subset
        self.assertEqual(unique_small_subset([100,210,440,900],750,3),(0,1,2))
        self.assertEqual(unique_small_subset([100,210,440,900,2000],1650,4),(0,1,2,3))
        self.assertIsNone(unique_small_subset([100,210,440,900],750,3,max_checks=0))
        self.assertIsNone(unique_small_subset([100,210,440,900],750,3,max_candidates=3))
        self.assertIsNone(unique_small_subset([1,2,3,4,5],9,3))

if __name__=='__main__':unittest.main()
