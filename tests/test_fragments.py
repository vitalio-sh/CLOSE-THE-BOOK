import unittest
from decimal import Decimal as D
from tests.test_matching import context, invoice, txn
from recon.matching import fragment_partial, _unwrapped, _fragment_tokens


def book():
    # One payer with many open invoices; numbers share prefixes like a real ledger.
    items = [invoice(f'I{n}', f'10624{n:05d}', str(100 + n)) for n in range(1, 40)]
    for n, item in enumerate(items):
        item['issue_date'] = f'2024-08-{1 + n % 28:02d}'
    return items


class FragmentPartialTests(unittest.TestCase):
    def test_unique_fragments_anchor_partial_set(self):
        # '400012' and '400027' each occur in one document number; '10624' and '00' are ambiguous.
        items = book()
        t = txn('5000', 'Alice Water Works INVOICE 10624 400012 400027 10624000 2400031')
        rows = fragment_partial(t, context(items))
        self.assertEqual({(r['item_id'], r['amount'], r['exception']) for r in rows},
                         {('I12', '112.00', ''), ('I27', '127.00', ''), ('I31', '131.00', '')})

    def test_requires_identified_payer(self):
        t = txn('5000', 'INVOICE 400012 400027 2400031')
        self.assertIsNone(fragment_partial(t, context(book())))

    def test_requires_several_fragments_and_anchors(self):
        self.assertIsNone(fragment_partial(txn('5000', 'Alice Water Works INVOICE 400012'), context(book())))
        # Three fragments but a single unique anchor: abstain.
        self.assertIsNone(fragment_partial(txn('5000', 'Alice Water Works INV 400012 10624000 106240'), context(book())))

    def test_anchors_must_not_exceed_cash(self):
        t = txn('200', 'Alice Water Works INVOICE 400012 400027 2400031')
        self.assertIsNone(fragment_partial(t, context(book())))

    def test_anchors_plus_fifo_remainder_give_full_set(self):
        items = book()
        # Cash = I12 + I27 + the oldest other open invoice I1 (101).
        t = txn(str(112 + 127 + 101), 'Alice Water Works INVOICE 400012 400027 10624')
        rows = fragment_partial(t, context(items))
        self.assertEqual({r['item_id'] for r in rows}, {'I12', 'I27', 'I1'})

    def test_closed_and_other_currency_documents_are_not_anchors(self):
        items = book()
        items[11]['open_amount'] = D(0)          # I12 already settled
        items[26]['currency'] = 'USD'            # I27 in another currency
        t = txn('5000', 'Alice Water Works INVOICE 400012 400027 2400031 400035 400038')
        rows = fragment_partial(t, context(items))
        self.assertEqual({r['item_id'] for r in rows}, {'I31', 'I35', 'I38'})

    def test_returns_and_outgoing_lines_ignored(self):
        self.assertIsNone(fragment_partial(txn('-5000', 'Alice Water Works 400012 400027 2400031'), context(book())))
        self.assertIsNone(fragment_partial(txn('5000', 'DEVOLUCION RECIBO Alice Water Works 400012 400027 2400031'), context(book())))

    def test_norma43_wrapped_concept_is_rejoined(self):
        text = 'TRANSFERENCIA DE FERRETERIA AZUL S.COO | P. | CONCEPTO: FACTURA FV24-05 FV24-05 FV24 | -050 FV24-0507608 FV24-050 FV24-050563 | 6 FV24-05'
        self.assertIn('S.COOP. CONCEPTO', _unwrapped(text))
        tokens = _fragment_tokens({'text': text})
        self.assertIn('FV240505636', tokens)
        self.assertNotIn('FV24050563', tokens)


if __name__ == '__main__':
    unittest.main()
