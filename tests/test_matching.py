import unittest
from types import SimpleNamespace
from decimal import Decimal as D
from recon.matching import reconcile,classify,norm

def context(items):
    by_number={};by_party={}
    for i in items:
        by_number.setdefault(norm(i['document_number']),[]).append(i['item_id'])
        by_party.setdefault(i['party_id'],[]).append(i['item_id'])
    return SimpleNamespace(items={i['item_id']:i for i in items},by_number=by_number,by_party=by_party,
        customers={'C1':{'name':'Alice Water Works','country':'ES'}},suppliers={},accounts={},history={},payment_terms={})
def invoice(i='I1',number='FV25-00001',amount='100',**kw):
    return dict(item_id=i,document_number=number,party_id='C1',side='AR',kind='invoice',open_amount=D(amount),total_amount=D(amount),currency='EUR',entity='ES01',issue_date='2025-01-01',**kw)
def txn(amount='100',text='PAGO FV25-00001',**kw):
    return dict(bank_txn_id='T1',amount=amount,text=text,currency='EUR',entity='ES01',booking_date='2025-01-15',**kw)
class MatchingTests(unittest.TestCase):
    def test_partial_and_immutable_balance(self):
        c=context([invoice()]);r=reconcile(txn('60'),c)[0]
        self.assertEqual((r['amount'],r['exception']),('60.00','PARTIAL'));self.assertEqual(c.items['I1']['open_amount'],D(100))
    def test_no_global_amount_guess(self):
        self.assertIsNone(reconcile(txn(text='Mystery sender'),context([invoice()])))
    def test_party_fifo(self):
        a=invoice();b=invoice('I2','FV25-00002');b['issue_date']='2025-01-02'
        self.assertEqual(reconcile(txn(text='Alice Water Works'),context([b,a]))[0]['item_id'],'I1')
    def test_fifo_skips_invoice_quoted_by_same_day_sibling(self):
        # POLICIES 5.6: FIFO only without a usable reference; a same-day line quoting the
        # oldest invoice pays it, so the unreferenced receipt takes the next oldest.
        from recon.matching import index_same_day
        a=invoice();b=invoice('I2','FV25-00002');b['issue_date']='2025-01-02'
        c=context([a,b]);t1=txn(text='Alice Water Works PAYMENT');t2=txn(text='Alice Water Works FV25-00001');t2['bank_txn_id']='T2'
        index_same_day(c,[t1,t2])
        self.assertEqual(reconcile(t1,c)[0]['item_id'],'I2')
        other=txn(text='Alice Water Works FV25-00001');other.update(bank_txn_id='T3',booking_date='2025-01-16')
        index_same_day(c,[t1,other])
        self.assertEqual(reconcile(t1,c)[0]['item_id'],'I1')
    def test_accepted_discount(self):
        c=context([invoice(payment_terms='D2')]);c.payment_terms={'D2':{'early_payment_discount_pct':'2','discount_days':'14'}}
        self.assertEqual(reconcile(txn('98'),c)[0]['exception'],'SHORT_DISCOUNT')
        self.assertEqual(reconcile(txn('98'),c)[0]['amount'],'100.00')
    def test_late_discount(self):
        c=context([invoice(payment_terms='D2')]);c.payment_terms={'D2':{'early_payment_discount_pct':'2','discount_days':'2'}}
        self.assertEqual(reconcile(txn('98'),c)[0]['exception'],'PARTIAL')
    def test_fx_original_amount(self):
        c=context([invoice()]);t=txn('90',orig_amount='100',orig_currency='EUR');t['currency']='GBP'
        r=reconcile(t,c)[0];self.assertEqual((r['amount'],r['exception']),('100.00','FX_DIFFERENCE'))
    def test_return_positive_allocation(self):
        c=context([invoice()]);r=reconcile(txn('-100','DEVOLUCION RECIBO FV25-00001'),c)[0]
        self.assertEqual((r['amount'],r['exception']),('100.00','RETURNED'))
    def test_nonitem(self):
        r=classify(txn('-45','COMISION DEVOLUCION RECIBO'),context([]))[0]
        self.assertEqual((r['category'],r['item_id'],r['amount']),('BANK_FEES','',''))
    def test_credit_note_positive_netting(self):
        a=invoice();c=invoice('CREDIT','AB25-00001','20');c['kind']='credit'
        rows=reconcile(txn('80','Alice Water Works PAGO FV25-00001 AB25-00001'),context([a,c]))
        self.assertEqual({r['item_id']:r['amount'] for r in rows},{'I1':'100.00','CREDIT':'20.00'})
    def test_future_invoice_excluded(self):
        a=invoice();a['issue_date']='2025-02-01'
        self.assertIsNone(reconcile(txn(),context([a])))
    def test_refund_not_accidentally_matched(self):
        a=invoice();a['side']='AP'
        self.assertIsNone(reconcile(txn('-100','DEVOLUCION PAGO DUPLICADO FV25-00001'),context([a])))
    def test_partial_return(self):
        c=context([invoice()]);r=reconcile(txn('-30','DEVOLUCION RECIBO FV25-00001'),c)[0]
        self.assertEqual((r['amount'],r['exception']),('30.00','RETURNED'))
    def test_unique_scoped_subset(self):
        items=[invoice('A','F1','60'),invoice('B','F2','40'),invoice('C','F3','17')]
        rows=reconcile(txn('100','Alice Water Works PAYMENT'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'A','B'})
    def test_ambiguous_subset_takes_fifo_prefix(self):
        # Two exact sets; POLICIES 5.6 FIFO: the oldest-first prefix (A, B) is paid.
        items=[invoice('A','F1','60'),invoice('B','F2','40'),invoice('C','F3','30'),invoice('D','F4','70')]
        rows=reconcile(txn('100','Alice Water Works PAYMENT'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'A','B'})
    def test_free_subset_without_fifo_shape_rejected(self):
        # Exact sets exist (A+C, A+B+D) but none is a prefix or a contiguous run: abstain.
        items=[invoice('A','F1','60'),invoice('B','F2','10'),invoice('C','F3','40'),invoice('D','F4','30')]
        for n,i in enumerate(items):i['issue_date']=f'2025-01-0{n+1}'
        self.assertIsNone(reconcile(txn('100','Alice Water Works PAYMENT'),context(items)))
    def test_truncated_reference_prefix(self):
        items=[invoice('A','INV24-015107','100'),invoice('B','INV24-099999','100')]
        rows=reconcile(txn('100','Alice Water Works INVOICE INV24-0151'),context(items))
        self.assertEqual([r['item_id'] for r in rows],['A'])
    def test_gap_fill_after_quoted_invoice(self):
        items=[invoice('A','FV25-00001','60'),invoice('B','FV25-00002','40'),invoice('C','FV25-00003','25')]
        rows=reconcile(txn('100','Alice Water Works FV25-00001'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'A','B'})
    def test_credit_in_set_needs_its_related_invoice(self):
        from recon.matching import _credits_supported
        a=invoice('A','FV25-00001','100');b=invoice('B','FV25-00002','40')
        c=invoice('C','AB25-00001','40',related_document='FV25-00001');c['kind']='credit'
        self.assertTrue(_credits_supported([a,c]))
        c['related_document']='OTHER';self.assertFalse(_credits_supported([a,b,c]))
        c['related_document']='FV25-00002';self.assertFalse(_credits_supported([a,b,c]))  # full cancellation
    def test_truncated_prefix_never_forces_single_reference(self):
        # 'INV24-0115' matches one invoice by prefix, but the cash is the FIFO pair: no abstention.
        items=[invoice('A','INV24-011501','60'),invoice('B','FV25-00002','40')]
        items[1]['issue_date']='2025-01-02'
        rows=reconcile(txn('100','Alice Water Works INVOICE INV24-0115'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'A','B'})
    def test_signature_overpayment_single(self):
        items=[invoice('A','F1','480'),invoice('B','F2','700')]
        rows=reconcile(txn('500','Alice Water Works PAYMENT'),context(items))
        self.assertEqual([(r['item_id'],r['amount'],r['exception']) for r in rows],[('A','480.00','OVERPAYMENT')])
    def test_pdf_replacement(self):
        a=invoice();b=invoice('I2','FV25-00002')
        c=context([a,b]);c.pdf_facts=SimpleNamespace(resolve=lambda iid:'I2' if iid=='I1' else iid,get=lambda iid:{})
        self.assertEqual(reconcile(txn(),c)[0]['item_id'],'I2')
    def test_pdf_service_month_overrides_fifo(self):
        a=invoice();b=invoice('I2','FV25-00002');b['issue_date']='2025-01-02'
        c=context([a,b]);c.pdf_facts=SimpleNamespace(resolve=lambda iid:iid,get=lambda iid:{'service_month':'2024-12' if iid=='I2' else '2024-11'})
        self.assertEqual(reconcile(txn('100','Alice Water Works SERVICE DECEMBER 2024'),c)[0]['item_id'],'I2')
    def test_closed_history_requires_party(self):
        a=invoice();a.update(open_amount=D(0),historical_closed=True)
        self.assertIsNone(reconcile(txn('100','BANK REF FV25-00001'),context([a])))
    def test_closed_history_rejects_arbitrary_cash(self):
        a=invoice();a.update(open_amount=D(0),historical_closed=True)
        self.assertIsNone(reconcile(txn('500','Alice Water Works FV25-00001'),context([a])))
    def test_closed_history_exact_party_duplicate(self):
        a=invoice();a.update(open_amount=D(0),historical_closed=True)
        r=reconcile(txn('100','Alice Water Works FV25-00001'),context([a]))[0]
        self.assertEqual((r['amount'],r['exception']),('100.00','DUPLICATE_PAYMENT'))
    def test_current_closed_item_uses_applied_history(self):
        a=invoice();a['open_amount']=D(0);c=context([a]);c.history={'I1':[{'amount':'40','exception':'PARTIAL'}]}
        r=reconcile(txn('40','FV25-00001'),c)[0]
        self.assertEqual((r['amount'],r['exception']),('40.00','DUPLICATE_PAYMENT'))
        self.assertIsNone(reconcile(txn('99','FV25-00001'),c))
    def test_category_words_do_not_match_names_or_substrings(self):
        c=context([])
        self.assertIsNone(classify(txn(text='TRANSFER TO FRAISSE SARL'),c))
        self.assertIsNone(classify(txn(text='EXPRES ES91 PAYMENT'),c))
        self.assertIsNone(classify(txn(text='PAYMENT',counterparty_name='BANK FEES SERVICES LTD'),c))
        self.assertEqual(classify(txn('-10','FRAIS VIREMENT'),c)[0]['category'],'BANK_FEES')
        self.assertEqual(classify(txn('-10','PRES.ES01-123456'),c)[0]['category'],'LOAN_REPAYMENT')
    def test_generic_reembolso_does_not_preempt_credit(self):
        a=invoice('APC-1','RFV25-00001');a.update(side='AP',kind='credit')
        c=context([a]);t=txn('100','REEMBOLSO RFV25-00001')
        self.assertIsNone(classify(t,c));self.assertEqual(reconcile(t,c)[0]['item_id'],'APC-1')
    def test_bank_charge_requires_rail_country_and_both_limits(self):
        c=context([invoice(amount='1000')]);c.customers['C1']['country']='US'
        t=txn('985','SWIFT TRANSFER FV25-00001')
        r=reconcile(t,c)[0];self.assertEqual((r['amount'],r['exception']),('1000.00','SHORT_BANK_CHARGES'))
        self.assertEqual(reconcile(txn('970','SWIFT TRANSFER FV25-00001'),c)[0]['exception'],'PARTIAL')
        self.assertEqual(reconcile(txn('985','PAGO FV25-00001'),c)[0]['exception'],'PARTIAL')
        c.customers['C1']['country']='ES'
        self.assertEqual(reconcile(t,c)[0]['exception'],'PARTIAL')
        c=context([invoice(amount='10000')]);c.customers['C1']['country']='US'
        self.assertEqual(reconcile(txn('9900','SWIFT TRANSFER FV25-00001'),c)[0]['exception'],'PARTIAL')
    def test_bank_charge_last_available_fx_rate(self):
        a=invoice(amount='10000');a['currency']='USD';c=context([a]);c.customers['C1']['country']='US'
        c.fx_rates={'2025-01-14':{'USD':D(2)},'2025-01-16':{'USD':D(1)}}
        t=txn('9900','SWIFT TRANSFER FV25-00001');t['currency']='USD'
        self.assertEqual(reconcile(t,c)[0]['exception'],'SHORT_BANK_CHARGES')
        c.fx_rates={'2025-01-16':{'USD':D(2)}}
        self.assertEqual(reconcile(t,c)[0]['exception'],'PARTIAL')
    def test_no_card_settlement_fee(self):
        self.assertIsNone(classify(txn('100','LIQUIDACION REMESA DE COMERCIOS'),context([])))
    def test_four_invoice_unique_subset(self):
        items=[invoice(str(i),'FV25-'+str(10000+i),str(a)) for i,a in enumerate((11,23,47,89))]
        rows=reconcile(txn('170','Alice Water Works PAYMENT'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'0','1','2','3'})
    def test_large_unreferenced_subset_abstains(self):
        items=[invoice(str(i),'FV25-'+str(10000+i),str(2**i)) for i in range(8)]
        self.assertIsNone(reconcile(txn('255','Alice Water Works PAYMENT'),context(items)))
    def test_multiple_references_net_related_unmentioned_credit(self):
        a=invoice(amount='100');b=invoice('I2','FV25-00002','70')
        cr=invoice('CREDIT','CN25-90001','20');cr.update(kind='credit',related_document=a['document_number'])
        rows=reconcile(txn('150','Alice Water Works FV25-00001 FV25-00002'),context([a,b,cr]))
        self.assertEqual({r['item_id']:r['amount'] for r in rows},{'I1':'100.00','I2':'70.00','CREDIT':'20.00'})
        self.assertTrue(all(not r['exception'] for r in rows))
    def test_netting_does_not_take_unrelated_credit(self):
        a=invoice();b=invoice('I2','FV25-00002','70')
        cr=invoice('CREDIT','CN25-90001','20');cr.update(kind='credit',related_document='UNRELATED')
        self.assertIsNone(reconcile(txn('150','Alice Water Works FV25-00001 FV25-00002'),context([a,b,cr])))
    def test_group_discount_policy_and_late_partial(self):
        a=invoice(payment_terms='D2');b=invoice('I2','FV25-00002','70')
        c=context([a,b]);c.payment_terms={'D2':{'early_payment_discount_pct':'2','discount_days':'14'}}
        t=txn('168','Alice Water Works FV25-00001 FV25-00002')
        rows={r['item_id']:r for r in reconcile(t,c)}
        self.assertEqual((rows['I1']['amount'],rows['I1']['exception']),('100.00','SHORT_DISCOUNT'))
        c.payment_terms['D2']['discount_days']='2'
        rows={r['item_id']:r for r in reconcile(t,c)}
        self.assertEqual((rows['I1']['amount'],rows['I1']['exception']),('98.00','PARTIAL'))
    def test_group_deduction_ambiguity_abstains(self):
        a=invoice(payment_terms='D2');b=invoice('I2','FV25-00002','100',payment_terms='D2')
        c=context([a,b]);c.payment_terms={'D2':{'early_payment_discount_pct':'2','discount_days':'14'}}
        self.assertIsNone(reconcile(txn('198','Alice Water Works FV25-00001 FV25-00002'),c))
    def test_noisy_refs_to_other_party_do_not_override_identified_payer(self):
        a=invoice('A','FV25-10001','60');b=invoice('B','FV25-10002','40')
        x=invoice('X','FV25-90001','17');x['party_id']='OTHER'
        y=invoice('Y','FV25-90002','23');y['party_id']='OTHER'
        rows=reconcile(txn('100','Alice Water Works FV25-90001 FV25-90002'),context([a,b,x,y]))
        self.assertEqual({r['item_id'] for r in rows},{'A','B'})
    def test_yearless_service_month_uses_next_month_invoice(self):
        a=invoice();a['issue_date']='2025-05-01'
        b=invoice('I2','FV25-00002');b['issue_date']='2025-06-01'
        t=txn('100','Alice Water Works SERVICIOS MAYO');t['booking_date']='2025-06-20'
        self.assertEqual(reconcile(t,context([a,b]))[0]['item_id'],'I2')
    def test_yearless_service_month_crosses_year_boundary(self):
        a=invoice();a['issue_date']='2024-12-01'
        b=invoice('I2','FV25-00002');b['issue_date']='2025-01-01'
        t=txn('100','Alice Water Works MANT. DICIEMBRE')
        self.assertEqual(reconcile(t,context([a,b]))[0]['item_id'],'I2')
    def test_service_month_ambiguous_or_missing_does_not_guess_fifo(self):
        a=invoice();b=invoice('I2','FV25-00002');b['issue_date']='2025-01-02'
        c=context([a,b])
        self.assertIsNone(reconcile(txn('100','Alice Water Works SERVICE DECEMBER 2024'),c))
        self.assertIsNone(reconcile(txn('100','Alice Water Works SERVICE OCTOBER 2024'),c))
    def test_yearless_service_month_does_not_choose_between_years(self):
        a=invoice();a['issue_date']='2024-05-01'
        b=invoice('I2','FV25-00002');b['issue_date']='2025-05-01'
        t=txn('100','Alice Water Works SERVICE APRIL');t['booking_date']='2025-06-01'
        self.assertIsNone(reconcile(t,context([a,b])))
        t['text']='Alice Water Works SERVICE APRIL 2025'
        self.assertEqual(reconcile(t,context([a,b]))[0]['item_id'],'I2')
    def test_other_party_search_budget_cannot_prove_unique_payer(self):
        for size in (7,25):
            with self.subTest(size=size):
                items=[invoice('A'+str(i),'FA-'+str(10000+i),'1') for i in range(size)]
                for iid,amount in [('B1',2),('B2',size-2)]:
                    item=invoice(iid,'FB-'+iid,str(amount));item['party_id']='C2';items.append(item)
                c=context(items);c.customers['C2']={'name':'Alice Water Works','country':'ES'}
                self.assertIsNone(reconcile(txn(str(size),'Alice Water Works PAYMENT'),c))
    def test_over_budget_party_with_insufficient_total_is_not_ambiguous(self):
        items=[invoice('A'+str(i),'FA-'+str(10000+i),'1') for i in range(25)]
        for iid,amount in [('B1',40),('B2',60)]:
            item=invoice(iid,'FB-'+iid,str(amount));item['party_id']='C2';items.append(item)
        c=context(items);c.customers['C2']={'name':'Alice Water Works','country':'ES'}
        self.assertEqual({r['item_id'] for r in reconcile(txn('100','Alice Water Works PAYMENT'),c)},{'B1','B2'})
    def test_explicit_payer_references_scope_budget_proof(self):
        items=[invoice('A'+str(i),'FA-'+str(10000+i),'2') for i in range(25)]
        for iid,number,amount in [('B1','FB-90001',10),('B2','FB-90002',15),('B3','FB-90003',5)]:
            item=invoice(iid,number,str(amount));item['party_id']='C2';items.append(item)
        c=context(items);c.customers['C2']={'name':'Alice Water Works','country':'ES'}
        rows=reconcile(txn('30','Alice Water Works FB-90001 FB-90002'),c)
        self.assertEqual({r['item_id'] for r in rows},{'B1','B2','B3'})

    def test_numeric_suffix_reference_beats_truncated_prefix(self):
        # 'INVOICE INV24-0116' (cut text) is a prefix of B, but B is also quoted as '0116143'.
        items=[invoice('A','INV24-0116705','100'),invoice('B','INV24-0116143','50'),
               invoice('C','INV24-0117623','30'),invoice('E','INV24-0100001','50')]
        items[3]['issue_date']='2024-12-01'   # older exact 50: a gap fill would take it
        t=txn('180','DIRECT CREDIT ALICE WATER INVOICE INV24-0116',counterparty_name='Alice Water Works',
              remittance_info='INVOICE INV24-0116705 0116143 0117623')
        self.assertEqual({r['item_id'] for r in reconcile(t,context(items))},{'A','B','C'})
    def test_unique_rest_completes_cut_reference_list(self):
        # Quoted A; the cut list hides B and D. B+D is the only exact combination of the rest.
        items=[invoice('A','FV25-00001','100'),invoice('B','FV25-00002','37.15'),invoice('C','FV25-00003','12.30'),
               invoice('D','FV25-00004','55.55'),invoice('E','FV25-00005','80.00')]
        for n,i in enumerate(items):i['issue_date']=f'2025-01-0{n+1}'
        rows=reconcile(txn('192.70','Alice Water Works FRA FV25-00001'),context(items))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in rows},
                         {('A','100.00',''),('B','37.15',''),('D','55.55','')})
    def test_unique_rest_ambiguous_abstains_from_completion(self):
        # B+D and C+E both make up the gap of 60: no completion (only the quoted invoice is emitted).
        items=[invoice('A','FV25-00001','100'),invoice('B','FV25-00002','25'),invoice('C','FV25-00003','20'),
               invoice('D','FV25-00004','35'),invoice('E','FV25-00005','40')]
        for n,i in enumerate(items):i['issue_date']=f'2025-01-0{n+1}'
        rows=reconcile(txn('160','Alice Water Works FRA FV25-00001'),context(items)) or []
        self.assertNotIn('B',{r['item_id'] for r in rows})
if __name__=='__main__':unittest.main()
