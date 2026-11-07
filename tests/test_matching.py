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
    def test_ambiguous_subset_rejected(self):
        items=[invoice('A','F1','60'),invoice('B','F2','40'),invoice('C','F3','30'),invoice('D','F4','70')]
        self.assertIsNone(reconcile(txn('100','Alice Water Works PAYMENT'),context(items)))
    def test_pdf_replacement(self):
        a=invoice();b=invoice('I2','FV25-00002')
        c=context([a,b]);c.pdf_facts=SimpleNamespace(resolve=lambda iid:'I2' if iid=='I1' else iid,get=lambda iid:{})
        self.assertEqual(reconcile(txn(),c)[0]['item_id'],'I2')
    def test_pdf_service_month_overrides_fifo(self):
        a=invoice();b=invoice('I2','FV25-00002');b['issue_date']='2025-01-02'
        c=context([a,b]);c.pdf_facts=SimpleNamespace(resolve=lambda iid:iid,get=lambda iid:{'service_month':'2024-12' if iid=='I2' else '2024-11'})
        self.assertEqual(reconcile(txn('100','Alice Water Works SERVICE DECEMBER 2024'),c)[0]['item_id'],'I2')
    def test_no_card_settlement_fee(self):
        self.assertIsNone(classify(txn('100','LIQUIDACION REMESA DE COMERCIOS'),context([])))
if __name__=='__main__':unittest.main()
