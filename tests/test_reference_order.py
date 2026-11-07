import copy,csv,io,json,sys,unittest,os
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
B=Path(__file__).resolve().parent
from recon.context import Context,norm
from recon.engine import execute,order_transactions,exact_reference_items,explicit_reference_items
from recon.matching import reconcile
from recon.validate import FIELDS

def ctx(two=False):
 c=object.__new__(Context)
 c.customers={'p':dict(name='Example Customer SL',country='ES')};c.suppliers={}
 c.payment_terms={};c.fx_rates={};c.history=defaultdict(list)
 c.items={};c.by_number=defaultdict(list);c.by_party=defaultdict(list)
 for iid,num,issued in [('i','FV24-0000123','2024-01-01')]+([('j','FV24-0000124','2024-01-02')] if two else []):
  item=dict(item_id=iid,document_number=num,party_id='p',customer_name='Example Customer SL',entity='ES01',side='AR',kind='invoice',currency='EUR',total_amount=D(100),open_amount=D(100),issue_date=issued)
  c.items[iid]=item;c.by_number[norm(num)].append(iid);c.by_party['p'].append(iid)
 return c

def t(tid,text='',amount='100',day='2024-02-01'):
 return dict(bank_txn_id=tid,booking_date=day,account_id='a',entity='ES01',currency='EUR',amount=amount,text=text,counterparty_name='Example Customer SL')

def run(c,ts):
 output,audit=io.StringIO(),io.StringIO();writer=csv.DictWriter(output,fieldnames=FIELDS);writer.writeheader()
 execute(c,ts,[('matching',lambda txn:reconcile(txn,c))],writer,audit)
 return list(csv.DictReader(io.StringIO(output.getvalue()))),[json.loads(x)['bank_txn_id'] for x in audit.getvalue().splitlines()]

class GuardedOrderTests(unittest.TestCase):
 def test_reference_reservation_fixes_fifo_collision(self):
  c=ctx(True);rows,order=run(c,[t('a'),t('b','FRA FV24-0000123')])
  self.assertEqual(order,['b','a']);self.assertEqual({r['bank_txn_id']:r['item_id'] for r in rows},{'a':'j','b':'i'})
  self.assertEqual([r['exception'] for r in rows],['',''])
  self.assertEqual(sum(len(h) for h in c.history.values()),2)

 def test_partial_return_repayment_duplicate_sequence(self):
  c=ctx();ts=[t('a','A CUENTA FV24-0000123','60'),t('b','DEVOLUCION TRANSFERENCIA FV24-0000123','-60'),t('c','FV24-0000123'),t('d','DUPLICATE PAYMENT FV24-0000123')]
  rows,order=run(c,ts)
  self.assertEqual(order,['a','b','c','d']);self.assertEqual([r['exception'] for r in rows],['PARTIAL','RETURNED','','DUPLICATE_PAYMENT'])
  self.assertEqual(c.items['i']['open_amount'],0);self.assertEqual(len(c.history['i']),4)

 def test_partial_unmarked_not_promoted(self):
  c=ctx();a=t('a','FV24-0000123','60');b=t('b','FV24-0000123','40')
  rows,order=run(c,[b,a]);self.assertEqual(order,['a','b']);self.assertEqual([r['exception'] for r in rows],['PARTIAL',''])

 def test_retention_release_sequence(self):
  c=ctx();rows,order=run(c,[t('a','RETENTION FV24-0000123','90'),t('b','FV24-0000123','10')])
  self.assertEqual(order,['a','b']);self.assertEqual([r['exception'] for r in rows],['PARTIAL',''])

 def test_dispute_settlement_sequence(self):
  c=ctx();rows,order=run(c,[t('a','DISPUTE FV24-0000123','70'),t('b','FV24-0000123','30')])
  self.assertEqual(order,['a','b']);self.assertEqual([r['exception'] for r in rows],['SHORT_DISPUTE',''])

 def test_late_discount_remainder_sequence(self):
  c=ctx();c.payment_terms={'T30D2':{'early_payment_discount_pct':'2','discount_days':'10'}};c.items['i']['payment_terms']='T30D2'
  rows,order=run(c,[t('a','FV24-0000123','98'),t('b','FV24-0000123','2')])
  self.assertEqual(order,['a','b']);self.assertEqual([r['exception'] for r in rows],['PARTIAL',''])

 def test_never_cross_dates_and_stable_ties(self):
  c=ctx(True);a=t('a',day='2024-01-31');b=t('b','FV24-0000123');d=t('d','FV24-0000124')
  self.assertEqual([x['bank_txn_id'] for x in order_transactions(c,[d,b,a])],['a','b','d'])

 def test_multi_reference_keeps_all_identities_no_single_priority(self):
  c=ctx(True);a=t('a');b=t('b','FV24-0000123 FV24-0000124');self.assertEqual(exact_reference_items(b,c),{'i','j'})
  self.assertEqual(order_transactions(c,[b,a]),[a,b])
  c.items['j']['open_amount']=D(0);self.assertEqual(exact_reference_items(b,c),{'i','j'})
  self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_credit_reference_blocks_single_invoice_priority(self):
  c=ctx(True);c.items['j']['kind']='credit';a=t('a');b=t('b','FV24-0000123 FV24-0000124')
  self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_amount_dates_po_suffixes_not_priority(self):
  for text in ['0000123','100.00 EUR','20240201','PO0000123','XXFV24-0000123YY']:
   c=ctx();a=t('a');b=t('b',text);self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_wrong_entity_future_and_currency_not_promoted(self):
  for key,value in [('entity','FR01'),('issue_date','2024-03-01'),('currency','USD')]:
   c=ctx();c.items['i'][key]=value;a=t('a');b=t('b','FV24-0000123');self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_orig_amount_fx_can_establish_full_payment(self):
  c=ctx();c.items['i']['currency']='USD';a=t('a');b=t('b','FV24-0000123','90');b.update(orig_amount='100',orig_currency='USD')
  self.assertEqual(order_transactions(c,[a,b]),[b,a])

 def test_unidentified_return_preserves_position(self):
  c=ctx();a=t('a','RETURNED ITEM','-100');a['counterparty_name']='';b=t('b','FV24-0000123')
  self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_no_state_mutation_by_scheduler(self):
  c=ctx(True);before=copy.deepcopy(c.items);history=copy.deepcopy(c.history)
  order_transactions(c,[t('a'),t('b','FV24-0000123')]);self.assertEqual(c.items,before);self.assertEqual(c.history,history)



class NumericOrderTests(unittest.TestCase):
 def test_typed_numeric_full_reference_promoted(self):
  c=ctx(True);a=t('a');b=t('b','INVOICE 0000123');self.assertEqual(order_transactions(c,[a,b]),[b,a])

 def test_numeric_whole_group_preserves_closed_and_credit_ambiguity(self):
  c=ctx(True);c.items['j']['kind']='credit';c.items['j']['open_amount']=D(0)
  b=t('b','INVOICES 0000123, 0000124');self.assertEqual(explicit_reference_items(b,c),{'i','j'})
  a=t('a');self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_numeric_raw_spans_reject_financial_values(self):
  c=ctx()
  for text in ['0000123','INVOICE 0000123.45','INVOICE 0000123,45','INVOICE 0000123 EUR','INVOICE 0000123%','INVOICE $0000123','PO 0000123','INVOICE 20240201','INVOICE 01022024','INVOICE 0000100','INVOICE 0001000']:
   q=t('q',text,amount='10' if text.endswith('0001000') else '100')
   self.assertFalse(explicit_reference_items(q,c),text)

 def test_numeric_unknown_payer_no_suffix_global_search(self):
  c=ctx();b=t('b','INV 0000123');b['counterparty_name']='Unknown';self.assertFalse(explicit_reference_items(b,c))

 def test_short_discount_not_promoted(self):
  c=ctx();a=t('a');b=t('b','INV 0000123','98');self.assertEqual(order_transactions(c,[b,a]),[a,b])

 def test_numeric_return_repayment_keeps_chronology(self):
  c=ctx();a=t('a','RETURNED ITEM INV 0000123','-100');b=t('b','INV 0000123')
  self.assertEqual(order_transactions(c,[b,a]),[a,b])



class BarrierTests(unittest.TestCase):
 def test_unknown_return_is_barrier_but_later_pair_can_reorder(self):
  c=ctx(True);a=t('a','RETURNED ITEM','-100');a['counterparty_name']=''
  b=t('b');d=t('d','FV24-0000123')
  self.assertEqual(order_transactions(c,[d,b,a]),[a,d,b])

 def test_cannot_jump_across_unknown_return(self):
  c=ctx(True);a=t('a');b=t('b','RETURNED ITEM','-100');b['counterparty_name']='';d=t('d','FV24-0000123')
  self.assertEqual(order_transactions(c,[d,b,a]),[a,b,d])



class EntityBarrierTests(unittest.TestCase):
 def test_unrelated_entity_return_does_not_block_pair(self):
  c=ctx(True)
  for item in c.items.values():item['entity']='US01'
  a=t('a');a['entity']='US01';b=t('b','RETURNED ITEM','-100');b['counterparty_name']='';b['entity']='DE01';d=t('d','FV24-0000123');d['entity']='US01'
  self.assertEqual(order_transactions(c,[d,b,a]),[d,b,a])

 def test_factory_entities_share_unknown_return_barrier(self):
  c=ctx();c.items['i']['side']='AP';a=t('a',amount='-100');b=t('b','RETURNED ITEM');b['counterparty_name']='';b['entity']='PT01';d=t('d','FV24-0000123','-100');d['entity']='ES00'
  self.assertEqual(order_transactions(c,[d,b,a]),[a,b,d])



if __name__ == '__main__':
    unittest.main()
