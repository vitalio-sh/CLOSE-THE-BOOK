from types import SimpleNamespace
import unittest
from decimal import Decimal as D
from test_matching import invoice,txn,context
from recon.financial_sequences import FinancialSequences

class RetentionTests(unittest.TestCase):
    def setup_matcher(self,items,facts):
        c=context(items);m=FinancialSequences(c);m.facts=SimpleNamespace(enabled=True,get=lambda i:facts.get(i,{}));return c,m
    def facts(self,**kw):
        return dict(source='invoice.pdf',net_due='90.00',retention_pct='10',**kw)
    def test_pdf_confirmed_retention_is_partial_and_immutable(self):
        c,m=self.setup_matcher([invoice()],{'I1':self.facts()})
        r=m.reconcile(txn('90','Alice Water Works PAYMENT'))
        self.assertEqual((r[0]['amount'],r[0]['exception']),('90.00','PARTIAL'))
        self.assertEqual(c.items['I1']['open_amount'],D(100))
    def test_ratio_without_document_abstains(self):
        c,m=self.setup_matcher([invoice()],{})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works PAYMENT')))
    def test_two_documented_candidates_abstain(self):
        c,m=self.setup_matcher([invoice(),invoice('I2','FV25-00002')],{'I1':self.facts(),'I2':self.facts()})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works PAYMENT')))
    def test_existing_remainder_not_reinterpreted(self):
        a=invoice();a['open_amount']=D(90);c,m=self.setup_matcher([a],{'I1':self.facts()})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works PAYMENT')))
    def test_wrong_currency_and_future_invoice_abstain(self):
        a=invoice();a['currency']='USD';c,m=self.setup_matcher([a],{'I1':self.facts()})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works PAYMENT')))
        a=invoice();a['issue_date']='2025-02-01';c,m=self.setup_matcher([a],{'I1':self.facts()})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works PAYMENT')))
    def test_fx_keeps_partial_exception(self):
        a=invoice();a['currency']='USD';c,m=self.setup_matcher([a],{'I1':self.facts()})
        r=m.reconcile(txn('80','Alice Water Works PAYMENT',orig_amount='90',orig_currency='USD'))
        self.assertEqual(r[0]['exception'],'PARTIAL')
    def test_pdf_net_disagreement_abstains(self):
        facts=self.facts();facts['net_due']='89.00';c,m=self.setup_matcher([invoice()],{'I1':facts})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works PAYMENT')))

class AnchorSafetyTests(RetentionTests):
    def test_named_other_invoice_prevents_amount_only_retention(self):
        c,m=self.setup_matcher([invoice(),invoice('I2','FV25-00002','200')],{'I1':self.facts()})
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works FV25-00002')))
    def test_multiple_exact_suffix_and_po_refs_never_drop_item(self):
        c,m=self.setup_matcher([invoice(),invoice('I2','FV25-00002','50')],{'I1':self.facts()})
        c.items['I2']['customer_po']='PO223344'
        for second in ['FV25-00002','00002','PO223344']:
            self.assertIsNone(m.reconcile(txn('90','Alice Water Works FV25-00001 '+second)))
    def test_explicit_credit_also_prevents_single_item_retention(self):
        c,m=self.setup_matcher([invoice(),invoice('CR','CN25-00003','10')],{'I1':self.facts()})
        c.items['CR']['kind']='credit'
        self.assertIsNone(m.reconcile(txn('90','Alice Water Works FV25-00001 CN25-00003')))

class BoundedReferenceTests(RetentionTests):
    def test_amount_and_longer_identifier_do_not_disambiguate_two_plans(self):
        c,m=self.setup_matcher([invoice(number='FV25-50000'),invoice('I2','FV25-60000')],{'I1':self.facts(),'I2':self.facts()})
        for ending in ['TOTAL 50000','FV25-500009','BATCH 50000','2025-05-01','50000.00 EUR']:
            t=txn('90','Alice Water Works '+ending)
            self.assertEqual(m.explicit_refs(t),set());self.assertIsNone(m.reconcile(t))
    def test_exact_document_and_typed_suffix_are_supported(self):
        c,m=self.setup_matcher([invoice(number='FV25-50000'),invoice('I2','FV25-60000')],{'I1':self.facts(),'I2':self.facts()})
        for ending in ['FV25-50000','INVOICE 50000']:
            self.assertEqual(m.explicit_refs(txn('90','Alice Water Works '+ending)),{'I1'})
        self.assertEqual(m.explicit_refs(txn('90','Alice Water Works FV25-50000 60000')),{'I1','I2'})

class RetentionDisputePriorityTests(RetentionTests):
    def test_explicit_dispute_blocks_retention_fallback(self):
        c,m=self.setup_matcher([invoice()],{'I1':self.facts()})
        for reason in ['DISPUTE DAMAGED GOODS','RECLAMACION','PENALTY','DEDUCCION']:
            self.assertIsNone(m.reconcile(txn('90','Alice Water Works '+reason)))
            self.assertIsNone(m.reconcile(txn('90','Alice Water Works FV25-00001 '+reason)))
        self.assertEqual(m.reconcile(txn('90','Alice Water Works PAYMENT'))[0]['exception'],'PARTIAL')

class NormalizedDisputeTests(unittest.TestCase):
    def test_accented_and_plain_dispute_precede_retention_and_plans(self):
        c,m=RetentionTests().setup_matcher([invoice()],{'I1':dict(source='invoice.pdf',net_due='90.00',retention_pct='10')})
        for text in ['DEDUCCION','DEDUCCIÓN','deducción','RECLAMACION','RÉCLAMATION','DISPUTE DAMAGED GOODS']:
            for reference in ['', 'FV25-00001 ']:
                self.assertIsNone(m.reconcile(txn('90','Alice Water Works '+reference+text)),text)
        self.assertEqual(m.reconcile(txn('90','Alice Water Works PAYMENT'))[0]['exception'],'PARTIAL')
        if hasattr(m,'plan_candidates'):
            c,m=RetentionTests().setup_matcher([invoice()],{'I1':dict(source='invoice.pdf',instalments_count=2)})
            self.assertIsNone(m.reconcile_referenced(txn('50','Alice Water Works FV25-00001 DEDUCCIÓN')))
            self.assertEqual(m.reconcile_referenced(txn('50','Alice Water Works FV25-00001 PAYMENT'))[0]['exception'],'PARTIAL')
