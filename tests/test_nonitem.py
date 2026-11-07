import csv
import io
from collections import defaultdict
from decimal import Decimal
import unittest

from recon.context import Context
from recon.engine import execute
from recon.nonitem import (NonItemClassifier, reversal_originals, text_key, unidentified_evidence)
from recon.validate import FIELDS

CATEGORIES = {'BANK_FEES', 'BANK_REVERSAL', 'UNIDENTIFIED', 'INSURANCE', 'TAX_PAYMENT', 'SOCIAL_SECURITY',
              'CUSTOMER_REFUND', 'SUPPLIER_REFUND', 'INTERCOMPANY', 'TAX_REFUND'}


def txn(tid, amount, text='', date='2024-10-02', account='ES01-X-EUR', **kw):
    return dict(bank_txn_id=tid, amount=amount, text=text, booking_date=date, account_id=account,
                ref1='', ref2=kw.pop('ref2', ''), remittance_info=kw.pop('remittance_info', ''),
                counterparty_name=kw.pop('counterparty_name', ''), **kw)


def context(txns=()):
    ctx = object.__new__(Context)
    ctx.items = {}
    ctx.history = defaultdict(list)
    ctx.categories = set(CATEGORIES)
    ctx.phase_txns = list(txns)
    return ctx


class ReversalTests(unittest.TestCase):
    def test_both_lines_of_a_bank_error_are_bank_reversal(self):
        # POLICIES §4: the wrong movement and its "ANULACION OPERACION INDEBIDA" are both BANK_REVERSAL.
        original = txn('B1', '261.80', 'TRANSFERENCIA RECIBIDA', date='2024-09-26')
        decoy = txn('B0', '261.80', 'TRANSFERENCIA RECIBIDA', date='2024-09-26', account='ES01-OTHER')
        reversal = txn('B2', '-261.80', 'ANULACION OPERACION INDEBIDA N 2024092689733505 TRANSFERENCIA RECIBIDA',
                       date='2024-09-27')
        self.assertEqual(reversal_originals([decoy, original, reversal]), {'B1': 'B2'})
        classifier = NonItemClassifier(context([decoy, original, reversal]))
        for line in (original, reversal):
            self.assertEqual(classifier.reconcile(line)[0]['category'], 'BANK_REVERSAL')

    def test_reversal_prefers_quoted_text_and_coded_date(self):
        early = txn('A', '-50.00', 'RECIBO', date='2024-10-01')
        quoted = txn('B', '-50.00', 'SEG. SOCIAL COTIZ.', date='2024-10-03')
        reversal = txn('C', '50.00', 'ANULACION OPERACION INDEBIDA N 2024100301 SEG. SOCIAL COTIZ.', date='2024-10-04')
        self.assertEqual(reversal_originals([early, quoted, reversal]), {'B': 'C'})

    def test_no_pair_without_opposite_amount(self):
        line = txn('A', '-50.00', 'RECIBO')
        reversal = txn('C', '49.00', 'ANULACION OPERACION INDEBIDA N 20241002 RECIBO')
        self.assertEqual(reversal_originals([line, reversal]), {})


class UnidentifiedEvidenceTests(unittest.TestCase):
    def test_generic_reference_receipts(self):
        self.assertEqual(unidentified_evidence(txn('a', '72.71', 'ACH CREDIT POOL & SPA CCD', remittance_info='REF 92730')),
                         'generic_reference')
        self.assertEqual(unidentified_evidence(txn('b', '400.01', '/ORDP/PISCINAS OASIS S.A./REMI/TRANSFER')),
                         'generic_reference')
        self.assertEqual(unidentified_evidence(txn('c', '30556.60', 'SPEI RECIBIDO CLAVE 1 REF 3824376 CONCEPTO '
                                                                    'PAGO PEDIDO FRANCISCO ORTEGA MEDINA')),
                         'generic_reference')

    def test_invoice_references_are_not_evidence(self):
        self.assertIsNone(unidentified_evidence(txn('a', '573.01', 'DIRECT CREDIT X', remittance_info='INV# INV24-0114358')))
        self.assertIsNone(unidentified_evidence(txn('b', '573.01', 'FP BENNETT POOLS LLP BALANCE N/REF 2023')))
        self.assertIsNone(unidentified_evidence(txn('c', '573.01', 'FP KMA AQUATICS LTD 230320409789 ORDER')))
        self.assertIsNone(unidentified_evidence(txn('d', '573.01', 'SPEI RECIBIDO CONCEPTO PAGO FRA. A24-123')))

    def test_cash_initials_and_round(self):
        self.assertEqual(unidentified_evidence(txn('a', '795.66', 'INGRESO EFECTIVO OFICINA 0107')), 'cash_deposit')
        self.assertEqual(unidentified_evidence(txn('b', '151.71', 'DIRECT CREDIT M.C.', counterparty_name='M.C.')),
                         'initials_payer')
        self.assertEqual(unidentified_evidence(txn('c', '967.86', 'PRZELEW PRZYCH. R.N.S. TYTUL: PAYMENT')),
                         'initials_payer')
        self.assertIsNone(unidentified_evidence(txn('d', '967.86', 'BONIFICO DA IMPIANTI MORALES HERMANOS S.P. CAUSALE')))
        self.assertEqual(unidentified_evidence(txn('e', '2000.00', 'DIRECT CREDIT JENNIFER ROGERS')), 'round_amount')
        self.assertIsNone(unidentified_evidence(txn('e', '2000.00', 'DIRECT CREDIT X'), allow_round=False))
        self.assertIsNone(unidentified_evidence(txn('f', '20000.00', 'DIRECT CREDIT JENNIFER ROGERS')))
        self.assertIsNone(unidentified_evidence(txn('g', '-2000.00', 'TRANSFER')))

    def test_front_handler_never_claims_round_amounts(self):
        classifier = NonItemClassifier(context())
        self.assertIsNone(classifier.reconcile(txn('a', '2000.00', 'DIRECT CREDIT JENNIFER ROGERS')))
        rows = classifier.reconcile(txn('b', '544.25', 'SEPA', remittance_info='ORDER'))
        self.assertEqual((rows[0]['category'], rows[0]['exception']), ('UNIDENTIFIED', 'UNIDENTIFIED'))


class LookupTests(unittest.TestCase):
    def test_text_key_ignores_digits_and_punctuation(self):
        self.assertEqual(text_key(txn('a', '-11.61', 'LIQUIDACION DE INTERESES, COMISIONES Y | GASTOS')),
                         ('-', 'LIQUIDACION DE INTERESES'))
        self.assertEqual(text_key(txn('b', '-1.00', 'TGSS. COTIZACION 001')), ('-', 'TGSS COTIZACION'))

    def test_frozen_lookup_categories(self):
        classifier = NonItemClassifier(context())
        cases = [('-40838.25', 'SEGUROS MULTIRRIESGO', 'INSURANCE'), ('-3229.30', 'TAX AUTHORITY', 'TAX_PAYMENT'),
                 ('-146044.56', 'TGSS. COTIZACION 001', 'SOCIAL_SECURITY'),
                 ('-11.61', 'LIQUIDACION DE INTERESES, COMISIONES Y GASTOS', 'BANK_FEES')]
        for amount, text, category in cases:
            rows = classifier.reconcile(txn('x', amount, text))
            self.assertEqual((rows[0]['category'], rows[0]['exception']), (category, ''), text)


class TextRuleTests(unittest.TestCase):
    def test_refund_wording(self):
        classifier = NonItemClassifier(context())
        cases = [('-234.49', 'FP ASHBRIDGE SPA SERV REFUND DUPLICATE P', 'CUSTOMER_REFUND'),
                 ('512.00', 'TRANSF. A SU FAVOR | DEVOLUCION PAGO DUPLICADO', 'SUPPLIER_REFUND'),
                 ('-66178.75', 'DGFIP TVA', 'TAX_PAYMENT'),
                 ('100000.00', 'DEVOLUCION PRESTAMO VALTERMIA IBERICA, | S.A.U.', 'INTERCOMPANY')]
        for amount, text, category in cases:
            self.assertEqual(classifier.reconcile(txn('x', amount, text))[0]['category'], category, text)


class FallbackTests(unittest.TestCase):
    def run_fallback(self, line):
        out, audit = io.StringIO(), io.StringIO()
        writer = csv.DictWriter(out, fieldnames=FIELDS); writer.writeheader()
        execute(context([line]), [line], [], writer, audit)
        row = list(csv.DictReader(io.StringIO(out.getvalue())))[0]
        return row['category'], row['exception']

    def test_unmatched_receipt_without_evidence_keeps_only_category(self):
        self.assertEqual(self.run_fallback(txn('a', '1234.56', 'ACH CREDIT OAK MESA POOL', remittance_info='PAYMENT')),
                         ('UNIDENTIFIED', ''))

    def test_unmatched_receipt_with_evidence_gets_exception(self):
        self.assertEqual(self.run_fallback(txn('a', '300.00', 'ACH CREDIT WATER CARE')), ('UNIDENTIFIED', 'UNIDENTIFIED'))


    def test_unmatched_reembolso_receipt_is_supplier_refund(self):
        line = txn('a', '5327.09', 'ACH CREDIT SERVICES SANDERS CCD', remittance_info='REEMBOLSO INV0002506')
        self.assertEqual(self.run_fallback(line), ('SUPPLIER_REFUND', ''))

    def test_unmatched_outgoing_line_is_customer_refund(self):
        line = txn('a', '-434.21', 'TRANSFERENCIA A VIDAL Y HERRERO HOTELE | S S.A.')
        self.assertEqual(self.run_fallback(line), ('CUSTOMER_REFUND', ''))


if __name__ == '__main__':
    unittest.main()
