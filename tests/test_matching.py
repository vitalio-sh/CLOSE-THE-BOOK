import unittest
from types import SimpleNamespace
from decimal import Decimal as D
from recon.matching import reconcile as _reconcile_only,classify,norm,quoted_groups,quoted_only
def reconcile(t,c):
    return _reconcile_only(t,c) or quoted_groups(t,c) or quoted_only(t,c)

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
    def test_transfer_after_dd_return_pays_reopened_invoice(self):
        # POLICIES 5.10: the customer pays the returned (re-opened) invoice by transfer, not the oldest.
        a=invoice();b=invoice('I2','FV25-00002');b['issue_date']='2025-01-02'
        c=context([b,a]);c.history={'I2':[{'item_id':'I2','exception':'RETURNED','amount':'100'}]}
        self.assertEqual(reconcile(txn(text='Alice Water Works'),c)[0]['item_id'],'I2')
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
    def test_bank_charge_foreign_sepa_member_and_fixed_fee(self):
        # TRAIN: a CH payer of an Italian entity loses a fixed intermediary fee (/CHGS/).
        a=invoice(amount='1029.67');a['entity']='IT01';c=context([a]);c.customers['C1']['country']='CH'
        t=txn('1009.67','ALICE AG PAGO FV25-00001 /CHGS/EUR20,00');t['entity']='IT01'
        r=reconcile(t,c)[0];self.assertEqual((r['amount'],r['exception']),('1029.67','SHORT_BANK_CHARGES'))
        # Same country as the entity: never a bank fee.
        c.customers['C1']['country']='IT'
        self.assertEqual(reconcile(t,c)[0]['exception'],'PARTIAL')
        # An exact 2 % share is a late discount (PARTIAL), not a fee, for an extended payer.
        b=invoice(amount='463.12');b['entity']='IT01';c=context([b]);c.customers['C1']['country']='CH'
        t=txn('453.86','ALICE AG PAGO FV25-00001 /CHGS/');t['entity']='IT01'
        self.assertEqual(reconcile(t,c)[0]['exception'],'PARTIAL')
        # A 45 EUR-equivalent fee is 2.4 % of a small CAD invoice: still a bank fee.
        d=invoice(amount='2762.76');d.update(currency='CAD',entity='US01');c=context([d]);c.customers['C1']['country']='CA'
        c.fx_rates={'2025-01-14':{'CAD':D('1.48')}}
        t=txn('2696.16','WIRE IN /ORG/ALICE LTD /OBI/INVOICE FV25-00001',bank_code='PMNT/RCDT/XBCT');t.update(currency='CAD',entity='US01')
        self.assertEqual(reconcile(t,c)[0]['exception'],'SHORT_BANK_CHARGES')
    def test_overpayment_requires_round_or_percent_excess(self):
        c=context([invoice(amount='607.42')])
        r=reconcile(txn('691.81','PAGO FV25-00001'),c)[0]
        self.assertEqual((r['item_id'],r['amount'],r['exception']),('I1','607.42',''))
        r=reconcile(txn('612.42','PAGO FV25-00001'),c)[0]
        self.assertEqual((r['amount'],r['exception']),('607.42','OVERPAYMENT'))
    def test_bank_charge_last_available_fx_rate(self):
        # Bounds rule for a currency without a fee history (no fixed schedule).
        a=invoice(amount='10000');a['currency']='GBP';c=context([a]);c.customers['C1']['country']='US'
        c.fx_rates={'2025-01-14':{'GBP':D(2)},'2025-01-16':{'GBP':D(1)}}
        t=txn('9900','SWIFT TRANSFER FV25-00001');t['currency']='GBP'
        self.assertEqual(reconcile(t,c)[0]['exception'],'SHORT_BANK_CHARGES')
        c.fx_rates={'2025-01-16':{'GBP':D(2)}}
        self.assertEqual(reconcile(t,c)[0]['exception'],'PARTIAL')
    def test_bank_charge_fixed_fee_schedule(self):
        # TRAIN: fees are 10/15/20/25/35/45 EUR, converted at a fixed rate and truncated.
        a=invoice(amount='10000');a['currency']='USD';c=context([a]);c.customers['C1']['country']='US'
        c.fx_rates={'2025-01-14':{'USD':D('1.1')}}
        t=txn('9970.38','SWIFT TRANSFER FV25-00001');t['currency']='USD'
        r=reconcile(t,c)[0];self.assertEqual((r['amount'],r['exception']),('10000.00','SHORT_BANK_CHARGES'))
        t=txn('9970.00','SWIFT TRANSFER FV25-00001');t['currency']='USD'
        self.assertEqual(reconcile(t,c)[0]['exception'],'PARTIAL')
        # A small off-schedule shortfall on a cross-border line is not a bank fee.
        b=invoice(amount='156.07');b.update(currency='CAD',entity='US01');c=context([b]);c.customers['C1']['country']='CA'
        c.fx_rates={'2025-01-14':{'CAD':D('1.48')}}
        t=txn('152.95','WIRE IN /ORG/ALICE LTD /OBI/INVOICE FV25-00001',bank_code='PMNT/RCDT/XBCT');t.update(currency='CAD',entity='US01')
        self.assertNotEqual(reconcile(t,c)[0]['exception'],'SHORT_BANK_CHARGES')
    def test_no_card_settlement_fee(self):
        self.assertIsNone(classify(txn('100','LIQUIDACION REMESA DE COMERCIOS'),context([])))
    def test_four_invoice_unique_subset(self):
        items=[invoice(str(i),'FV25-'+str(10000+i),str(a)) for i,a in enumerate((11,23,47,89))]
        rows=reconcile(txn('170','Alice Water Works PAYMENT'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'0','1','2','3'})
    def test_large_unreferenced_subset_abstains(self):
        # A free (non-FIFO-shaped) 7-of-9 amount-only subset is not evidence.
        items=[invoice(str(i),'FV25-'+str(10000+i),str(2**i)) for i in range(9)]
        self.assertIsNone(reconcile(txn('477','Alice Water Works PAYMENT'),context(items)))
    def test_whole_open_book_is_fifo_shaped(self):
        # POLICIES 5.6 FIFO: the oldest-first prefix of the payer's open items (here all of
        # them) is the known-payer ladder's set, whatever its size.
        items=[invoice(str(i),'FV25-'+str(10000+i),str(2**i)) for i in range(8)]
        rows=reconcile(txn('255','Alice Water Works PAYMENT'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{str(i) for i in range(8)})
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
        # The discounted invoice is the older one of the group: no newest-invoice grace (TRAIN).
        b['issue_date']='2025-01-02'
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
    def test_service_month_with_cut_year_digits(self):
        # Bank text cut after '20' of '2024': the month still selects the invoice.
        a=invoice();a['issue_date']='2024-11-01'
        b=invoice('I2','FV25-00002');b['issue_date']='2024-12-02'
        t=txn('100','Alice Water Works SERVICE OCTOBER 20');t['booking_date']='2025-01-02'
        self.assertEqual(reconcile(t,context([a,b]))[0]['item_id'],'I1')
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
    def test_quoted_invoices_emitted_when_rest_unexplained(self):
        # Cash covers quoted A and C plus an unidentifiable rest: emit A and C at their open amounts.
        items=[invoice('A','INV24-0110001','100'),invoice('B','INV24-0110002','40'),invoice('C','INV24-0110003','60'),
               invoice('D','INV24-0110004','45')]
        t=txn('1000','Alice Water Works INV# INV24-0110001 0110003')
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in reconcile(t,context(items))},
                         {('A','100.00',''),('C','60.00','')})
    def test_quoted_only_needs_surplus_and_unique_suffix(self):
        items=[invoice('A','INV24-0110001','100'),invoice('C','INV24-0110003','60'),invoice('X','INV23-0110003','70')]
        rows=reconcile(txn('1000','Alice Water Works INV# INV24-0110001 0110003'),context(items))
        self.assertEqual({r['item_id'] for r in rows},{'A'})   # '0110003' names two open invoices
        from recon.matching import _quoted_only
        self.assertIsNone(_quoted_only(txn('150','Alice Water Works INV# INV24-0110001 INV24-0110003'),context(items)))
    def test_quoted_invoices_short_cash_one_partial_share(self):
        # 15177.39 = 1731.80 in full + 60 % of 22409.33 (13445.59, floor): B is PARTIAL.
        items=[invoice('A','1032420259','1731.80'),invoice('B','1032421437','22409.33')]
        rows=reconcile(txn('15177.39','Alice Water Works INV 1032420259 421437'),context(items))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in rows},
                         {('A','1731.80',''),('B','13445.59','PARTIAL')})
    def test_quoted_short_cash_without_partial_share_abstains(self):
        items=[invoice('A','1032420259','1731.80'),invoice('B','1032421437','22409.33')]
        from recon.matching import _quoted_partial
        self.assertIsNone(_quoted_partial(txn('15000.00','Alice Water Works INV 1032420259 421437'),context(items)))
    def test_quoted_rest_with_one_unquoted_partial_share(self):
        # Quoted A in full; the rest is 50 % of unquoted B. No all-in-full combination exists.
        items=[invoice('A','INV24-0110001','100.10'),invoice('B','INV24-0110002','400.50'),invoice('C','INV24-0110003','37.00')]
        rows=reconcile(txn('300.35','Alice Water Works INV# INV24-0110001'),context(items))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in rows},
                         {('A','100.10',''),('B','200.25','PARTIAL')})
    def test_quoted_rest_partial_ambiguous_falls_back_to_quoted(self):
        # 50 % of B (200.25) and 50 % of D (200.25) both fill the gap: only A is emitted.
        items=[invoice('A','INV24-0110001','100.10'),invoice('B','INV24-0110002','400.50'),invoice('D','INV24-0110004','400.50')]
        rows=reconcile(txn('300.35','Alice Water Works INV# INV24-0110001'),context(items))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in rows},{('A','100.10','')})
    def test_unique_rest_defers_when_a_credit_family_explains_the_receipt(self):
        # A+B quoted. Invoice-only rest: C+E. With credit K on E, the units E-K and J also make up
        # the gap (credit_groups unit_subset, POLICIES 5.9): the unique rest defers to it.
        from recon.credit_groups import reconcile as credit_reconcile
        items=[invoice('A','FV25-00001','100'),invoice('B','FV25-00002','37.15'),invoice('C','FV25-00003','12.30'),
               invoice('D','FV25-00004','55.55'),invoice('E','FV25-00005','80.00'),
               invoice('F','FV25-00006','173.90'),invoice('J','FV25-00007','32.30')]   # D+F: no whole-book subset
        for n,i in enumerate(items):i['issue_date']=f'2025-01-0{n+1}'
        credit=invoice('K','AB25-00001','20.00',related_document='FV25-00005');credit['kind']='credit'
        t=txn('229.45','Alice Water Works FRA FV25-00001 FV25-00002')
        self.assertEqual({r['item_id'] for r in reconcile(t,context(items))},{'A','B','C','E'})
        self.assertIsNone(_reconcile_only(t,context(items+[credit])))   # matching defers; credit_groups runs next
        self.assertEqual({r['item_id'] for r in credit_reconcile(t,context(items+[credit]))},{'A','B','E','J','K'})
        # A cancelling credit note (POLICIES 5.7) never nets a receipt: the completion stands.
        cancel=dict(credit,reason='ANULACION FACTURA')
        self.assertEqual({r['item_id'] for r in reconcile(t,context(items+[cancel]))},{'A','B','C','E'})
    def test_party_partial_subset_unique_share(self):
        # No reference: A, C in full and 60 % of B (truncated 240.30 of 400.50) make up the cash.
        from recon.matching import party_partial_subset
        items=[invoice('A','INV24-0110001','100.10'),invoice('B','INV24-0110002','400.50'),invoice('C','INV24-0110003','37.00')]
        rows=party_partial_subset(txn('377.40','Alice Water Works PAYMENT'),context(items))
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in rows},
                         {('A','100.10',''),('B','240.30','PARTIAL'),('C','37.00','')})
    def test_party_partial_subset_abstains(self):
        from recon.matching import party_partial_subset
        items=[invoice('A','INV24-0110001','100.10'),invoice('B','INV24-0110002','400.50'),invoice('C','INV24-0110003','37.00')]
        # An all-in-full reading exists (A+C): not a partial group.
        self.assertIsNone(party_partial_subset(txn('137.10','Alice Water Works PAYMENT'),context(items)))
        # Two partial readings (60 % of B or of D): ambiguous.
        d=invoice('D','INV24-0110004','400.50')
        self.assertIsNone(party_partial_subset(txn('377.40','Alice Water Works PAYMENT'),context(items+[d])))
        # No identified payer.
        self.assertIsNone(party_partial_subset(txn('377.40','PAYMENT'),context(items)))
        # The in-full members must be fresh invoices, not residuals of earlier partial payments.
        resid=[dict(i) for i in items];resid[2]['total_amount']=D('370.00')
        self.assertIsNone(party_partial_subset(txn('377.40','Alice Water Works PAYMENT'),context(resid)))
        # Open related credit note: credit-note families decide.
        k=invoice('K','AB24-0000001','10.00',related_document='INV24-0110003');k['kind']='credit'
        self.assertIsNone(party_partial_subset(txn('377.40','Alice Water Works PAYMENT'),context(items+[k])))
    def test_full_numbers_of_lookalike_payer_scope_the_rest(self):
        # The name matches C1, but the full numbers are C2's: C2's book completes the cut list.
        items=[invoice('A','FV25-00001','100'),invoice('B','FV25-00002','37.15'),invoice('C','FV25-00003','12.30'),
               invoice('D','FV25-00004','55.55'),invoice('E','FV25-00005','80.00'),invoice('Z','FV25-09999','92.30')]
        for n,i in enumerate(items):i['issue_date']=f'2025-01-0{n+1}'
        for i in items[:5]:i['party_id']='C2'
        c=context(items);c.customers['C2']={'name':'Alice Water Works S.L.U.','country':'ES'}
        c.customers['C1']={'name':'Alice Water Works S.L.','country':'ES'}
        rows=reconcile(txn('229.45','Alice Water Works S.L. FRA FV25-00001 FV25-00002'),c)
        self.assertEqual({r['item_id'] for r in rows},{'A','B','C','E'})
        # A number that merely collides with an unrelated customer's invoice does not scope its book.
        c=context(items);c.customers['C2']={'name':'Bob Pools Iberia','country':'ES'}
        self.assertNotIn('C',{r['item_id'] for r in reconcile(txn('229.45','Alice Water Works FRA FV25-00001 FV25-00002'),c) or []})
    def test_dispute_settlement_pays_remainder_without_exception(self):
        # POLICIES 3 SHORT_DISPUTE: the later payment of a disputed remainder has no exception,
        # even when the earlier dispute was not recognised and the open balance is larger.
        from recon.matching import dispute_settlement
        c=context([invoice(amount='1000')])
        r=dispute_settlement(txn('120','DIRECT CREDIT ALICE WATER WORKS SETTLEMENT FV25-00001'),c)
        self.assertEqual((r[0]['item_id'],r[0]['amount'],r[0]['exception']),('I1','120.00',''))
        r=dispute_settlement(txn('120','/NAME/ALICE WATER WORKS/REMI/REGULARIZACION FV25-00001/EREF'),c)
        self.assertEqual(r[0]['exception'],'')
        # Exact remainder: ordinary matching already says '' (no rewrite needed).
        self.assertIsNone(dispute_settlement(txn('1000','ALICE WATER WORKS SETTLEMENT FV25-00001'),c))
        # No settlement wording, card settlements, or an unquoted invoice: no change.
        self.assertIsNone(dispute_settlement(txn('120','ALICE WATER WORKS PAGO FV25-00001'),c))
        self.assertIsNone(dispute_settlement(txn('120','CARD SETTLEMENT 20250101 FV25-00001'),c))
        self.assertIsNone(dispute_settlement(txn('120','ALICE WATER WORKS SETTLEMENT 2025'),c))
    def test_residual_group_remainder_plus_one_invoice(self):
        from recon.matching import residual_group
        items=[invoice('A','INV24-0110001','100.10'),invoice('B','INV24-0110002','400.50'),invoice('C','INV24-0110003','37.00'),
               invoice('D','INV24-0110004','55.55')]
        items[1]['open_amount']=D('160.20')   # 60 % of B was paid earlier: the remainder is open
        rows=residual_group(txn('197.20','Alice Water Works PAYMENT'),context(items))   # B remainder + C
        self.assertEqual({(r['item_id'],r['amount'],r['exception']) for r in rows},{('B','160.20',''),('C','37.00','')})
        # A pair of fresh invoices (A+C) is not this rule.
        self.assertIsNone(residual_group(txn('137.10','Alice Water Works PAYMENT'),context(items)))
        # A competing fresh triple of the same total (A+D+E = 197.20) abstains.
        e=invoice('E','INV24-0110005','41.55')
        self.assertIsNone(residual_group(txn('197.20','Alice Water Works PAYMENT'),context(items+[e])))
if __name__=='__main__':unittest.main()


class ShortNamePartyTests(unittest.TestCase):
    def ctx(self, customers):
        c = context([invoice()])
        c.customers = customers
        return c

    def test_short_person_name_identifies_payer(self):
        from recon.matching import party_candidates
        c = self.ctx({'C1': {'name': 'Emma Ross', 'entity': 'ES01'}})
        t = txn('100', 'ACH CREDIT EMMA ROSS CCD PAYMENT', counterparty_name='Emma Ross')
        self.assertEqual(party_candidates(t, c), ['C1'])
        self.assertEqual(reconcile(t, c)[0]['item_id'], 'I1')

    def test_short_name_needs_whole_words_and_one_local_payer(self):
        from recon.matching import party_candidates
        c = self.ctx({'C1': {'name': 'Emma Ross', 'entity': 'ES01'}})
        self.assertEqual(party_candidates(txn('100', 'GEMMAROSSI SL'), c), [])
        c = self.ctx({'C1': {'name': 'Emma Ross', 'entity': 'ES01'}, 'C2': {'name': 'Emma Ross', 'entity': 'ES01'},
                      'C3': {'name': 'Emma Ross', 'entity': 'US01'}})
        self.assertEqual(party_candidates(txn('100', 'EMMA ROSS'), c), [])
