import unittest
from types import SimpleNamespace
from test_matching import context, invoice, txn
from recon.financial_references import finance_reference_sets

class IntegrationReferenceGuards(unittest.TestCase):
    def test_pdf_replacement_rechecks_date_and_entity(self):
        old=invoice('OLD','FV25-50000');new=invoice('NEW','FV25-60000')
        c=context([old,new]);c.pdf_facts=SimpleNamespace(resolve=lambda i:'NEW' if i=='OLD' else i)
        t=txn('90','Alice Water Works FV25-50000')
        self.assertEqual(finance_reference_sets(t,c)['all'],{'NEW'})
        new['issue_date']='2025-02-01';self.assertEqual(finance_reference_sets(t,c)['all'],set())
        new['issue_date']='2025-01-01';new['entity']='ES02';self.assertEqual(finance_reference_sets(t,c)['all'],set())
    def test_closed_credit_and_po_preserved_together(self):
        a=invoice('A','FV25-50000');credit=invoice('C','CN25-60000');credit.update(kind='credit',open_amount=0,customer_po='PO223344')
        c=context([a,credit]);t=txn('90','Alice Water Works FV25-50000 PO223344')
        self.assertEqual(finance_reference_sets(t,c)['all'],{'A','C'})
