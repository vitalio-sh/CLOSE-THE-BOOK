import csv
import gzip
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from recon.history import enrich_referenced_closed, reference_tokens

class HistoryTests(unittest.TestCase):
    def context(self, root, phase='live'):
        p=Path(root);(p/phase).mkdir();(p/'train').mkdir(exist_ok=True)
        (p/phase/'open_items_at_start.csv.gz').touch()
        ctx=SimpleNamespace(phase_dir=p/phase,history_dirs=[p/'train'],items={},by_number=defaultdict(list),by_party=defaultdict(list))
        def add(r):
            r=dict(r);r['party_id']=r.get('customer_id') or r.get('supplier_id') or r.get('party_id');ctx.items[r['item_id']]=r
        ctx.add_item=add
        return ctx
    def write(self,ctx,rows):
        with gzip.open(ctx.history_dirs[0]/'ar_invoices.csv.gz','wt',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    def record(self,iid='ARI-2024-1',num='FV24-00001',date='2024-06-01',entity='ES01'):
        return dict(item_id=iid,document_number=num,issue_date=date,entity=entity,customer_id='C1',total_amount='100')
    def txn(self,text='PAGO FV24-00001',entity='ES01'):
        return dict(text=text,booking_date='2025-07-01',entity=entity)
    def test_closed_restoration_and_existing_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d);self.write(c,[self.record(),self.record('ARI-2024-2','FV24-00002')]);c.items['ARI-2024-2']={'open_amount':Decimal('25')}
            r=enrich_referenced_closed(c,[self.txn('FV24-00001 FV24-00002')])
            self.assertEqual(r['added_count'],1);self.assertEqual(c.items['ARI-2024-1']['open_amount'],Decimal(0));self.assertEqual(c.items['ARI-2024-2']['open_amount'],Decimal(25))
            self.assertEqual(c.by_number['FV2400001'],['ARI-2024-1'])
            self.assertEqual(enrich_referenced_closed(c,[self.txn()])['added_count'],0)
    def test_repeated_transfer_documents_restored(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d)
            with gzip.open(c.history_dirs[0]/'ap_invoices.csv.gz','wt',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['item_id','entity','supplier_id','supplier_invoice_number','issue_date','total_amount'])
                w.writeheader();w.writerows([dict(item_id='API-1',entity='ES01',supplier_id='S1',supplier_invoice_number='FV-24-0100',issue_date='2024-09-01',total_amount='2844.12'),
                                             dict(item_id='API-2',entity='ES01',supplier_id='S2',supplier_invoice_number='A003252',issue_date='2024-09-01',total_amount='606.23')])
            runs=c.history_dirs[0]/'payment_runs';runs.mkdir()
            fields=['run_id','run_type','execution_date','debtor_account_id','end_to_end_id','beneficiary','currency','transfer_amount','document_number','document_amount']
            with (runs/'ES01_202409_0.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
                w.writerow(dict(run_id='PR010024091060',run_type='transfers',execution_date='2024-09-10',debtor_account_id='ES01-A',end_to_end_id='PR010024091060-0109',beneficiary='Proveedor Uno',currency='EUR',transfer_amount='2844.12',document_number='FV-24-0100',document_amount='2844.12'))
                w.writerow(dict(run_id='PR010024091060',run_type='transfers',execution_date='2024-09-10',debtor_account_id='ES01-A',end_to_end_id='PR010024091060-0002',beneficiary='Proveedor Dos',currency='EUR',transfer_amount='606.23',document_number='A003252',document_amount='606.23'))
            txns=[dict(text='PAGO TRANSFERENCIAS | 4799-56-1122872-73 051775 000001',ref2='PR010024091060-0',amount='-2844.12',account_id='ES01-A',booking_date='2024-10-20',entity='ES01'),
                  dict(text='PAGO TRANSFERENCIAS | 4799-56-1122872-73 051775 000002',ref2='PR010024091060-0',amount='-606.23',account_id='ES01-B',booking_date='2024-10-20',entity='ES01')]
            # A transfer executed inside the phase is never restored.
            txns.append(dict(text='PAGO TRANSFERENCIAS | 1 000003',ref2='PR010024101099-0',amount='-77.00',account_id='ES01-A',booking_date='2024-10-20',entity='ES01'))
            with (runs/'ES01_202410_0.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
                w.writerow(dict(run_id='PR010024101099',run_type='transfers',execution_date='2024-10-20',debtor_account_id='ES01-A',end_to_end_id='PR010024101099-0003',beneficiary='Proveedor Tres',currency='EUR',transfer_amount='77.00',document_number='A003252',document_amount='77.00'))
            r=enrich_referenced_closed(c,txns)
            # Only the transfer of the same debtor account is restored, at zero balance.
            self.assertEqual(r['repeated_transfer_tokens'],1);self.assertEqual(set(c.items),{'API-1'})
            self.assertEqual(c.items['API-1']['open_amount'],Decimal(0))
    def test_skip_sample(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d,'sample');self.write(c,[self.record()]);self.assertEqual(enrich_referenced_closed(c,[self.txn()])['added_count'],0)
    def test_no_future_no_other_entity_no_partial_reference(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d);self.write(c,[self.record(date='2025-07-02'),self.record('I2','FV24-00002',entity='FR01'),self.record('I3','FV24-00003')])
            r=enrich_referenced_closed(c,[self.txn('FV24-00001 FV24-00002 FV24-000')]);self.assertEqual(r['added_count'],0)
    def test_numeric_requires_cue_when_short(self):
        self.assertIn('2814',reference_tokens(self.txn('FRA 2814')))
        self.assertNotIn('2814',reference_tokens(self.txn('PAYMENT 2814')))
    def test_swift_tags_preserve_document_slashes(self):
        self.assertIn('FV250001',reference_tokens(self.txn('/REMI/FV25/0001/EREF/NOTPROVIDED')))
    def test_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d);(c.phase_dir/'open_items_at_start.csv.gz').unlink()
            self.assertIn('Missing',enrich_referenced_closed(c,[self.txn()])['skipped_reason'])
    def batch(self,ctx,folder,name,rows):
        (ctx.phase_dir/folder).mkdir(exist_ok=True)
        with open(ctx.phase_dir/folder/name,'w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    def test_reversal_restores_cheque_card_and_transfer_documents(self):
        # POLICIES 3 RETURNED: the returned operation's documents are restored (closed, zero balance).
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d)
            self.write(c,[self.record('ARI-2025-1','1072420363','2025-05-01','US01'),self.record('ARI-2025-2','WEB259002496','2025-05-01','FR01'),
                          self.record('ARI-2025-3','FV25-77777','2025-05-01','US01')])
            self.batch(c,'cheque_deposits','US01_202506.csv',[dict(deposit_id='LBX1',deposit_date='2025-06-01',account_id='A',currency='USD',deposit_total='159.79',cheque_number='5127738',drawer='X',drawer_bank_routing='1',amount='159.79',stub_reference='1072420363'),
                                                             dict(deposit_id='LBX1',deposit_date='2025-06-01',account_id='A',currency='USD',deposit_total='159.79',cheque_number='5127799',drawer='X',drawer_bank_routing='1',amount='10',stub_reference='FV25-77777')])
            self.batch(c,'card_settlements','FR01_202506.csv',[dict(settlement_id='S1',acquirer='P',merchant_id='993110427',settlement_date='2025-06-02',account_id='B',currency='EUR',order_number='WEB259002496',card='****1159',auth_code='1',operation_date='2025-06-01',gross='189.79',fee='1',net='188.79')])
            txns=[dict(text='RETURNED ITEM CHK 5127738 STOP PAYMENT',amount='-159.79',booking_date='2025-07-01',entity='US01'),
                  dict(text='CHARGEBACK ****1159 MID 993110427',amount='-189.79',booking_date='2025-07-02',entity='FR01')]
            r=enrich_referenced_closed(c,txns)
            self.assertIn('ARI-2025-1',c.items);self.assertIn('ARI-2025-2',c.items)
            self.assertNotIn('ARI-2025-3',c.items)  # cheque never returned
            self.assertEqual(r['reversal_reference_tokens'],2)
    def test_factory_return_restores_subsidiary_run_documents(self):
        # POLICIES 5.2: ES00's account pays PT01's run; the rejected transfer comes back to ES00.
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d)
            with gzip.open(c.history_dirs[0]/'ap_invoices.csv.gz','wt',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['item_id','entity','supplier_id','supplier_invoice_number','issue_date','total_amount'])
                w.writeheader();w.writerows([dict(item_id='API-1',entity='PT01',supplier_id='S1',supplier_invoice_number='F24/02987',issue_date='2024-09-20',total_amount='1414.62')])
            self.batch(c,'payment_runs','PT01_202412_0.csv',[dict(run_id='PR010024122624',run_type='transfers',execution_date='2024-12-26',debtor_account_id='ES00-PENA-EUR',end_to_end_id='PR010024122624-0131',beneficiary='Filters Alvorada Lda.',currency='EUR',transfer_amount='1414.62',document_number='F24/02987',document_amount='1414.62')])
            txns=[dict(text='DEVOLUCION TRANSFERENCIA MD07 | FILTERS ALVORADA LDA. | PR010024122624-0131',amount='1414.62',account_id='ES00-PENA-EUR',booking_date='2025-01-03',entity='ES00')]
            enrich_referenced_closed(c,txns)
            self.assertIn('API-1',c.items)
            self.assertEqual(c.items['API-1']['open_amount'],Decimal(0))
    def test_dd_batch_number_without_prefix_restores_closed_invoice(self):
        # POLICIES 5.4/5.5: our number without prefix ('RECIBO 0121479' for INV24-0121479).
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d)
            self.write(c,[dict(self.record('ARI-2025-1','INV25-0121479','2025-05-01','PT01'),total_amount='998.35'),self.record('ARI-2025-2','INV25-0121480','2025-05-01','PT01')])
            self.batch(c,'direct_debit_batches','PT01_B01_202506.csv',[dict(batch_id='B1',collection_date='2025-06-20',account_id='A',currency='EUR',mandate_id='M1',debtor_name='X',amount='998.35',remittance_info='RECIBO 0121479')])
            enrich_referenced_closed(c,[dict(text='DEVOLUCION RECIBO AM04',amount='-998.35',booking_date='2025-07-01',entity='PT01')])
            self.assertIn('ARI-2025-1',c.items);self.assertNotIn('ARI-2025-2',c.items)
    def test_recibo_digit_tail_restores_collected_invoice(self):
        # A batch row printing 'RECIBO 0121479' collected 'INV24-0121479' (POLICIES 5.4); a later return
        # re-opens it, so restore it when entity, digit tail and amount agree.
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d)
            rows=[dict(self.record('ARI-2024-1','INV24-0121479','2024-08-21','PT01'),total_amount='998.35'),
                  dict(self.record('ARI-2024-2','INV23-0121479','2023-08-21','PT01'),total_amount='120.00'),
                  dict(self.record('ARI-2024-3','INV24-0121479','2024-08-21','AU01'),total_amount='998.35'),
                  dict(self.record('ARI-2024-4','1102416120','2024-08-22','US01'),total_amount='246.03')]
            self.write(c,rows)
            base=dict(batch_id='B1',scheme='SEPA CORE',collection_date='2024-09-20',account_id='A',currency='EUR',batch_total='1',
                      end_to_end_id='B1-1',mandate_id='VTM-PT-0001462',debtor_name='X',debtor_iban='',re_presentation='N')
            self.batch(c,'direct_debit_batches','PT01_PENA_202409.csv',[dict(base,amount='998.35',remittance_info='RECIBO 0121479')])
            self.batch(c,'direct_debit_batches','US01_HPNB_202409.csv',[dict(base,amount='246.03',remittance_info='RECIBO 416120')])
            enrich_referenced_closed(c,[dict(text='x',booking_date='2024-10-01',entity='PT01')])
            self.assertIn('ARI-2024-1',c.items);self.assertIn('ARI-2024-4',c.items)
            self.assertNotIn('ARI-2024-2',c.items)  # other amount
            self.assertNotIn('ARI-2024-3',c.items)  # other entity
            self.assertEqual(c.items['ARI-2024-1']['open_amount'],Decimal(0))
if __name__=='__main__':unittest.main()
