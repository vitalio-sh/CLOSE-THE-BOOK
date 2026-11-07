import csv,gzip,sys,tempfile,unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recon.documents import DocumentMatcher,_earliest_phase_booking
class T4Tests(unittest.TestCase):
 def setup_case(self,base,days=('2025-07-03','2025-07-01')):
  phase=base/'live';history=base/'train';phase.mkdir();history.mkdir()
  with gzip.open(phase/'bank_transactions.csv.gz','wt') as f:
   w=csv.writer(f);w.writerow(['bank_txn_id','booking_date']);w.writerows((f't{i}',d) for i,d in enumerate(days))
  item=dict(item_id='ARI-1',entity='ES01',issue_date='2025-05-01',total_amount=Decimal(100),open_amount=Decimal(100),currency='EUR',kind='invoice')
  return SimpleNamespace(phase_dir=phase,history_dirs=[history],items={'ARI-1':item},by_number={'INV10001':['ARI-1']},payment_terms={},customers={}),phase,history
 def advices(self,folder,rows):
  root=folder/'remittance_advices';root.mkdir()
  with (root/'index.csv').open('w') as f:
   w=csv.DictWriter(f,fieldnames=['file','entity','payer_name','received_date','format']);w.writeheader()
   for name,day,ref in rows:
    w.writerow(dict(file=name,entity='ES01',payer_name='Acme Corporation',received_date=day,format='csv'))
    (root/name).write_text(f'payment_ref;{ref};payment_date;2025-07-01;currency;EUR\ndocument;doc_date;gross;discount_or_deduction;reason;paid;your_order\nINV10001;01/05/2025;100;;;100;\nTOTAL;;;;;100;\n')
 def txn(self,ref,day='2025-07-01'):
  return dict(bank_txn_id='t',entity='ES01',booking_date=day,amount='100',currency='EUR',text=ref)
 def test_boundary_unsorted_missing_current_index_and_guards(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,phase,history=self.setup_case(Path(d));self.advices(history,[('a.csv','2025-05-31','PAY00001'),('b.csv','2025-06-30','PAY00002'),('c.csv','2025-05-30','PAY00003'),('d.csv','2025-07-01','PAY00004')]);m=DocumentMatcher(ctx)
   self.assertEqual(_earliest_phase_booking(phase),'2025-07-01');self.assertEqual(m.stats['historical_files_total'],2);self.assertEqual(m.stats['files_total'],2)
   self.assertEqual(m.reconcile(self.txn('PAY00001'))[0]['item_id'],'ARI-1')
   self.assertIsNone(m.reconcile(self.txn('PAY00001','2025-07-02')))
   txn=self.txn('PAY00002');txn['entity']='ES02';self.assertIsNone(m.reconcile(txn))
   txn=self.txn('PAY00002');txn['amount']='99';self.assertIsNone(m.reconcile(txn))
   self.assertNotIn('PAY00003',m.by_ref);self.assertNotIn('PAY00004',m.by_ref)
 def test_duplicate_history_paths_do_not_create_ambiguity(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,phase,history=self.setup_case(Path(d));self.advices(history,[('a.csv','2025-06-30','PAY00001')]);alias=Path(d)/'alias';alias.symlink_to(history,target_is_directory=True);ctx.history_dirs=[history,history,alias]
   m=DocumentMatcher(ctx);self.assertEqual(m.stats['historical_files_total'],1);self.assertIsNotNone(m.reconcile(self.txn('PAY00001')))
 def test_current_and_historical_roots_with_same_basename(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,phase,history=self.setup_case(Path(d));self.advices(history,[('a.csv','2025-06-30','PAY00001')]);self.advices(phase,[('a.csv','2025-07-01','PAY00002')]);m=DocumentMatcher(ctx)
   self.assertEqual(m.stats['files_total'],2);self.assertIsNotNone(m.reconcile(self.txn('PAY00001')));self.assertIsNotNone(m.reconcile(self.txn('PAY00002')))
 def test_empty_phase_and_absent_history(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,phase,history=self.setup_case(Path(d),days=());self.advices(history,[('a.csv','2025-06-30','PAY00001')]);ctx.history_dirs.append(Path(d)/'absent');m=DocumentMatcher(ctx);self.assertEqual(m.stats['files_total'],0)
 def test_identical_copies_across_phase_roots_are_one_evidence(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,phase,history=self.setup_case(Path(d));self.advices(history,[('a.csv','2025-06-30','PAY00001')]);self.advices(phase,[('b.csv','2025-06-30','PAY00001')]);m=DocumentMatcher(ctx)
   self.assertEqual(m.stats['duplicate_historical_advices'],1);self.assertEqual(m.stats['parsed'],1);self.assertIsNotNone(m.reconcile(self.txn('PAY00001')))
 def test_distinct_advice_with_same_reference_remains_ambiguous(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,phase,history=self.setup_case(Path(d));self.advices(history,[('a.csv','2025-06-30','PAY00001')]);self.advices(phase,[('b.csv','2025-06-30','PAY00001')])
   item={**ctx.items['ARI-1'],'item_id':'ARI-2'};ctx.items['ARI-2']=item;ctx.by_number['INV10002']=['ARI-2']
   path=history/'remittance_advices/a.csv';path.write_text(path.read_text().replace('INV10001','INV10002'))
   m=DocumentMatcher(ctx);self.assertEqual(m.stats['duplicate_historical_advices'],0);self.assertIsNone(m.reconcile(self.txn('PAY00001')))
if __name__=='__main__':unittest.main()
