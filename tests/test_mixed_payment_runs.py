import copy,csv,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal as D
from recon.batches import BatchMatcher
from recon.batch_fx import mixed_run_cash_evidence, mixed_run_proposal_valid, money, signature, mixed_run_allocate, run_reference_ids

FIELDS=['run_id','run_type','execution_date','debtor_account_id','end_to_end_id','beneficiary','beneficiary_iban','currency','transfer_amount','document_number','document_type','document_amount','remittance_info']
class MixedRunTests(unittest.TestCase):
 def fixtures(self,root,extra=()):
  folder=root/'payment_runs';folder.mkdir()
  base=dict(run_id='PR010025010401',run_type='aggregated',execution_date='2025-01-04',debtor_account_id='US01-USD',beneficiary='Supplier Company',beneficiary_iban='',remittance_info='')
  details=[dict(base,end_to_end_id='US-001',currency='USD',transfer_amount='100.00',document_number='INV-USD',document_type='INVOICE',document_amount='100.00'),dict(base,end_to_end_id='US-002',currency='EUR',transfer_amount='90.00',document_number='INV-EUR',document_type='INVOICE',document_amount='100.00'),dict(base,end_to_end_id='US-002',currency='EUR',transfer_amount='90.00',document_number='CR-EUR',document_type='CREDIT_NOTE',document_amount='-10.00')]
  with (folder/'US01_202501.csv').open('w') as f:
   w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(details+list(extra))
  items={}
  for iid,num,currency,amount,kind in [('API-1','INV-USD','USD','100','invoice'),('API-2','INV-EUR','EUR','100','invoice'),('APC-3','CR-EUR','EUR','10','credit')]:
   items[iid]=dict(item_id=iid,document_number=num,entity='US01',party_id='S1',supplier_name='Supplier Company',issue_date='2025-01-01',currency=currency,total_amount=D(amount),open_amount=D(amount),kind=kind,side='AP')
  ctx=SimpleNamespace(phase_dir=root,history_dirs=[],items=items,by_number={'INVUSD':['API-1'],'INVEUR':['API-2'],'CREUR':['APC-3']},by_party={'S1':list(items)},customers={},suppliers={'S1':{'name':'Supplier Company'}},fx_rates={'2025-01-03':{'USD':D('1.2')},'2025-01-06':{'USD':D('9')}})
  txn=dict(bank_txn_id='BT1',account_id='US01-USD',entity='US01',booking_date='2025-01-04',amount='-208.54',currency='USD',text='BULK PAYMENT PR010025010401 2 TRANSFERS')
  return ctx,txn,details
 def test_full_mixed_run_fx_flags_credit_and_guard_evidence(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx);rows=m.reconcile(txn);self.assertIsNotNone(rows)
   self.assertEqual({r['item_id']:(r['amount'],r['exception']) for r in rows},{'API-1':('100.00',''),'API-2':('100.00','FX_DIFFERENCE'),'APC-3':('10.00','')})
   self.assertTrue(mixed_run_proposal_valid(ctx,txn,rows));self.assertEqual(ctx._mixed_run_proposal_evidence['cash']['rate_evidence']['USD'][1],'2025-01-03')
   self.assertFalse(mixed_run_proposal_valid(ctx,{**txn,'amount':'-208.55'},rows))
   changed=copy.deepcopy(rows);changed[0]['amount']='99.00';self.assertFalse(mixed_run_proposal_valid(ctx,txn,changed))
 def test_explicit_identity_count_and_cash_gates(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx)
   for changes in [dict(text='BULK PAYMENT PR010025010401 3 TRANSFERS'),dict(text='BULK PAYMENT 2 TRANSFERS'),dict(account_id='OTHER'),dict(entity='ES01'),dict(booking_date='2025-01-05'),dict(amount='-207.00'),dict(amount='-211.26'),dict(amount='-190.00')]:
    with self.subTest(changes=changes):self.assertIsNone(m.reconcile({**txn,**changes}))
   ctx.fx_rates={'2025-01-06':{'USD':D('1.2')}};self.assertIsNone(m.reconcile(txn))
 def test_tolerance_applies_only_to_foreign_component(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details[0].update(document_amount='1000000.00',transfer_amount='1000000.00');details=[dict(r,_entity='US01') for r in details]
   self.assertIsNone(mixed_run_cash_evidence(ctx,{**txn,'amount':'-1000113.00'},details))
   self.assertIsNotNone(mixed_run_cash_evidence(ctx,{**txn,'amount':'-1000108.54'},details))
 def test_distinct_accounts_with_colliding_run_ids_are_separated(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);ctx,txn,details=self.fixtures(root)
   extra=[dict(r,debtor_account_id='US01-OTHER',end_to_end_id=r['end_to_end_id']) for r in details]
   with (root/'payment_runs/US01_extra.csv').open('w') as f:
    w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(extra)
   m=BatchMatcher(ctx);self.assertIsNotNone(m.reconcile(txn));self.assertIsNotNone(m.reconcile({**txn,'account_id':'US01-OTHER'}))
 def test_missing_member_or_transfer_total_mismatch_abstains(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx);del ctx.by_number['INVEUR'];del ctx.items['API-2'];ctx.by_party['S1'].remove('API-2');self.assertIsNone(m.reconcile(txn))
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details=[dict(r,_entity='US01') for r in details];details[1]['transfer_amount']='91.00';self.assertIsNone(mixed_run_cash_evidence(ctx,txn,details))
 def test_guard_requires_evidence_and_rechecks_details(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx);rows=m.reconcile(txn);saved=ctx._mixed_run_proposal_evidence;del ctx._mixed_run_proposal_evidence;self.assertFalse(mixed_run_proposal_valid(ctx,txn,rows));ctx._mixed_run_proposal_evidence=saved;saved['details']=tuple(dict(r,execution_date='2025-01-03') for r in saved['details']);self.assertFalse(mixed_run_proposal_valid(ctx,txn,rows))
 def test_guard_rejects_same_total_substitution_after_registration(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx);rows=m.reconcile(txn)
   ctx.items['API-4']={**ctx.items['API-2'],'item_id':'API-4','document_number':'OTHER-DOC'};ctx.by_number['OTHERDOC']=['API-4']
   changed=[{**r,'item_id':'API-4'} if r['item_id']=='API-2' else dict(r) for r in rows]
   ctx._mixed_run_proposal_evidence['signature']=signature(txn,changed)
   self.assertFalse(mixed_run_proposal_valid(ctx,txn,changed))
 def test_second_foreign_currency_abstains(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));extra=dict(details[0],currency='GBP',end_to_end_id='US-003');details=[dict(r,_entity='US01') for r in details+[extra]];ctx.fx_rates['2025-01-03']['GBP']=D('.8')
   self.assertIsNone(mixed_run_cash_evidence(ctx,{**txn,'text':txn['text'].replace('2 TRANSFERS','3 TRANSFERS')},details))
 def test_factor_fallback_requires_explicit_wording_and_coherent_supplier(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details=[dict(r,_entity='US01',beneficiary='Ribagorza Confirming') for r in details]
   self.assertIsNotNone(mixed_run_allocate(ctx,txn,details))
   self.assertIsNone(mixed_run_allocate(ctx,txn,[dict(r,beneficiary='Unrelated Company') for r in details]))
   ctx.items['APC-3']['party_id']='S2';self.assertIsNone(mixed_run_allocate(ctx,txn,details))
 def test_factor_rejects_eligible_identity_ties_and_excludes_incompatible_items(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details=[dict(r,_entity='US01',beneficiary='Example Factoring') for r in details]
   ctx.items['API-4']={**ctx.items['API-2'],'item_id':'API-4','party_id':'S2'};ctx.by_number['INVEUR'].append('API-4')
   self.assertIsNone(mixed_run_allocate(ctx,txn,details))
   ctx.items['API-4']['issue_date']='2025-01-05';self.assertIsNotNone(mixed_run_allocate(ctx,txn,details))
   ctx.items['API-4']['issue_date']='2025-01-01';ctx.items['API-4']['open_amount']=D('90');self.assertIsNotNone(mixed_run_allocate(ctx,txn,details))
   ctx.items['API-4']['open_amount']=D('110');ctx.items['API-4']['total_amount']=D('110');self.assertIsNotNone(mixed_run_allocate(ctx,txn,details))
 def test_factor_preserves_normal_supplier_evidence_first(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details=[dict(r,_entity='US01') for r in details]
   ctx.items['API-4']={**ctx.items['API-2'],'item_id':'API-4','party_id':'S2','supplier_name':'Different Company'};ctx.by_number['INVEUR'].append('API-4')
   self.assertIsNotNone(mixed_run_allocate(ctx,txn,details))
 def test_credit_sign_and_fragment_identity_are_checked(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details=[dict(r,_entity='US01',beneficiary='Example Factoring') for r in details]
   wrong=[dict(r,document_amount='10.00') if r['document_type']=='CREDIT_NOTE' else r for r in details]
   self.assertIsNone(mixed_run_allocate(ctx,txn,wrong))
   details[1]['document_amount']='40.00';details.append(dict(details[1],document_amount='60.00'))
   rows=mixed_run_allocate(ctx,txn,details);self.assertIsNotNone(rows)
   self.assertEqual(next(r['amount'] for r in rows if r['item_id']=='API-2'),'100.00')
 def test_repeated_source_detail_preserved_and_copied_file_deduplicated(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);ctx,txn,details=self.fixtures(root)
   # Actual PR010125071007 repeats A004545 EUR2756.51 in one transfer.
   # Model that source multiplicity alongside a foreign-currency transfer.
   details[0].update(document_number='A004545',document_amount='2756.51',transfer_amount='5513.02',remittance_info='FRAS A004545 A004545')
   details.insert(1,dict(details[0]));ctx.items['API-1'].update(document_number='A004545',open_amount=D('5513.02'),total_amount=D('5513.02'));ctx.by_number['A004545']=ctx.by_number.pop('INVUSD');txn['amount']='-5621.56'
   source=root/'payment_runs/US01_202501.csv'
   with source.open('w') as f:
    w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(details)
   history=root/'history';(history/'payment_runs').mkdir(parents=True)
   (history/'payment_runs/US01_copy.csv').write_bytes(source.read_bytes());ctx.history_dirs=[history]
   m=BatchMatcher(ctx);groups=list(m.mixed_run_groups.values());self.assertEqual(len(groups),1);self.assertEqual(len(groups[0][0]),4)
   rows=m.reconcile(txn);self.assertIsNotNone(rows);self.assertEqual(next(r['amount'] for r in rows if r['item_id']=='API-1'),'5513.02');self.assertTrue(mixed_run_proposal_valid(ctx,txn,rows))
   ctx.items['API-1']['open_amount']=D('2756.51');self.assertIsNone(m.reconcile(txn))
 def test_complete_run_tokens_not_prefixes_and_real_separator_formatting(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));m=BatchMatcher(ctx)
   for bad in ['BULK PAYMENT PR0100250104019 2 TRANSFERS','BULK PAYMENT PR010025010401A 2 TRANSFERS','BULK PAYMENT XPR010025010401 2 TRANSFERS','BULK PAYMENT PR010025010401 PR9999 2 TRANSFERS']:
    changed={**txn,'text':bad};self.assertIsNone(m.reconcile(changed))
    self.assertIsNone(mixed_run_cash_evidence(ctx,changed,[dict(r,_entity='US01') for r in details]))
   self.assertIsNotNone(m.reconcile({**txn,'text':'TRANSF. EMITIDAS FICHERO C34 PR0100250 | 10401 | (2 ORD.)'}))
 def test_transfer_total_requires_exact_rounded_equality(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,details=self.fixtures(Path(d));details=[dict(r,_entity='US01') for r in details]
   for wrong in ['99.99','100.01']:
    changed=[dict(r,transfer_amount=wrong) if r['end_to_end_id']=='US-001' else r for r in details]
    self.assertIsNone(mixed_run_cash_evidence(ctx,txn,changed))
 def test_carrier_copies_source_details_immutably(self):
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx);rows=m.reconcile(txn)
   detail=next(iter(m.mixed_run_groups.values()))[0][0];detail['document_number']='MUTATED'
   self.assertTrue(mixed_run_proposal_valid(ctx,txn,rows))
   with self.assertRaises(TypeError):ctx._mixed_run_proposal_evidence['details'][0]['document_number']='MUTATED'
 def test_complete_identifier_rejects_all_attached_punctuation_continuations(self):
  import string
  with tempfile.TemporaryDirectory() as d:
   ctx,txn,_=self.fixtures(Path(d));m=BatchMatcher(ctx)
   for punctuation in string.punctuation:
    for suffix in ('9','X'):
     bad={**txn,'text':f'BULK PAYMENT PR010025010401{punctuation}{suffix} 2 TRANSFERS'}
     with self.subTest(punctuation=punctuation,suffix=suffix):
      self.assertIsNone(m.reconcile(bad))
      self.assertNotEqual(run_reference_ids(bad),{'PR010025010401'})
   for prefix in ('X','X_','X.','X:','_','.',':','('):
    bad={**txn,'text':f'BULK PAYMENT {prefix}PR010025010401 2 TRANSFERS'}
    self.assertIsNone(m.reconcile(bad))
   for run in ('PR010025010401','PR0100250 | 10401','PR0100250/10401','PR0100250 - 10401'):
    self.assertIsNotNone(m.reconcile({**txn,'text':f'BULK PAYMENT {run} 2 TRANSFERS'}))
 def test_half_up_money(self):
  self.assertEqual(money('1.005'),D('1.01'))
if __name__=='__main__':unittest.main()
