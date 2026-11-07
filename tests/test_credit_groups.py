import unittest
from decimal import Decimal as D

from recon.credit_groups import reconcile
from tests.test_matching import context, invoice, txn


def credit(i, number, amount, related, reason='Devolución / rectificación', **kw):
    item = invoice(i, number, amount, related_document=related, reason=reason, **kw)
    item['kind'] = 'credit'
    return item


def ids(rows):
    return {r['item_id']: (r['amount'], r['exception']) for r in rows} if rows else None


class CreditGroupTests(unittest.TestCase):
    def test_single_invoice_net_of_its_credit(self):
        # POLICIES 2: the credit note is a positive row at its full amount, no exception.
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1'), invoice('B', 'F2', '55')]
        rows = reconcile(txn('70', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(ids(rows), {'A': ('100.00', ''), 'C': ('30.00', '')})

    def test_quoted_credit_brings_its_invoice(self):
        items = [invoice('A', 'FA240121967', '870.24'), credit('C', 'AV240100569', '130.54', 'FA240121967'),
                 invoice('B', 'FA240119469', '327.61')]
        rows = reconcile(txn('739.70', 'Alice Water Works FACTURE FA241021967 CN 100569'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'C'})

    def test_contiguous_run_with_credit(self):
        items = [invoice('A', 'F1', '100'), invoice('B', 'F2', '200'), credit('C', 'AB1', '50', 'F2'),
                 invoice('E', 'F3', '999')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        rows = reconcile(txn('250', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'B', 'C'})

    def test_run_never_carries_fully_credited_invoice(self):
        # Zero-net veto: an invoice fully offset by its credit is not swept into a run.
        items = [invoice('OLD', 'F0', '40'), credit('Z', 'AB0', '40', 'F0'), invoice('A', 'F1', '100'),
                 invoice('B', 'F2', '200'), credit('C', 'AB1', '50', 'F2')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        rows = reconcile(txn('250', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'B', 'C'})

    def test_zero_net_invoice_inside_run_travels_with_it(self):
        # A fully credited invoice issued inside the paid span is cleared with the payment.
        items = [invoice('A', 'F1', '100'), invoice('Z', 'F2', '40'), credit('ZC', 'AB2', '40', 'F2'),
                 invoice('B', 'F3', '200'), invoice('E', 'F4', '999')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        rows = reconcile(txn('300', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'Z', 'ZC', 'B'})

    def test_zero_net_pair_is_no_credit_evidence_for_listed_invoices(self):
        # Noisy invoice references (a one-character typo) mark an explicit list: the
        # zero-net pair inside the span cannot turn it into a credit-note group.
        items = [invoice('A', 'FV24-0100001', '100'), invoice('Z', 'FV24-0100002', '40'),
                 credit('ZC', 'AB24-0100002', '40', 'FV24-0100002'), invoice('B', 'FV24-0100003', '200'),
                 invoice('E', 'FV24-0100004', '999')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        self.assertIsNone(reconcile(txn('300', 'Alice Water Works FV24-0100081 FV24-0100093'), context(items)))

    def test_gapped_run_skips_an_invoice_paid_elsewhere(self):
        from recon.credit_groups import _runs
        units = [('A', '100'), ('B', '260'), ('X', '777'), ('E', '300'), ('G', '50')]
        items = []
        for n, (key, amount) in enumerate(units):
            items.append(invoice(key, 'F' + key, amount))
            items[-1]['issue_date'] = f'2025-01-{10 + n}'
        items.append(credit('C', 'ABB', '60', 'FB'))
        items[-1]['issue_date'] = '2025-01-11'
        rows = reconcile(txn('600', 'Alice Water Works PAYMENT'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'B', 'C', 'E'})
        book = [(items[0], [], 10000), (items[1], [items[5]], 20000), (items[2], [], 77700),
                (items[3], [], 30000), (items[4], [], 5000)]
        found = _runs(book, 60000, 2)
        self.assertEqual([(gaps, [u[0]['item_id'] for u in run]) for gaps, run in found], [(1, ['A', 'B', 'E'])])
        self.assertEqual(_runs(book, 60000, 0), [])

    def test_early_families_never_add_zero_net_pairs(self):
        # Explicitly listed invoices (ar_1toN_text) do not carry an unlisted zero-net pair.
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'FV24-0100001', '100'), invoice('Z', 'FV24-0100002', '40'),
                 credit('ZC', 'AB24-0100002', '40', 'FV24-0100002'), invoice('B', 'FV24-0100003', '200'),
                 credit('BC', 'AB24-0100003', '50', 'FV24-0100003')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        rows = reconcile_early(txn('250', 'Alice Water Works FV24-0100001 FV24-0100003'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'B', 'BC'})

    def test_cancelled_invoice_never_settled(self):
        # POLICIES 5.7: an invoice cancelled by a credit note is not settled by bank.
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1', reason='Anulación de factura')]
        self.assertIsNone(reconcile(txn('70', 'Alice Water Works PAYMENT'), context(items)))

    def test_ambiguous_single_nets_abstain(self):
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1'),
                 invoice('B', 'F2', '90'), credit('E', 'AB2', '20', 'F2')]
        self.assertIsNone(reconcile(txn('70', 'Alice Water Works PAYMENT'), context(items)))

    def test_unknown_payer_abstains(self):
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1')]
        self.assertIsNone(reconcile(txn('70', 'Mystery sender'), context(items)))

    def test_small_subsets_unique_or_nothing(self):
        from recon.credit_groups import _small_subsets
        self.assertEqual(len(_small_subsets([5, 7, 11, 13], 18, 2)), 2)   # 5+13 and 7+11: ambiguous
        self.assertEqual(_small_subsets([5, 7, 11, 40], 23, 3), [(0, 1, 2)])
        self.assertEqual(_small_subsets([5, 7, 11, 40], 99, 3), [])

    def test_early_net_of_quoted_credit_beats_withholding(self):
        # POLICIES 2/5.9: the open credit referencing the invoice explains the difference.
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'NF0124002785', '3041.27'), credit('C', 'ND0124000097', '304.12', 'NF0124002785'),
                 invoice('B', 'NF0124002999', '500')]
        rows = reconcile_early(txn('2737.15', 'Alice Water Works FATURA NF01240 CN 000097'), context(items))
        self.assertEqual(ids(rows), {'A': ('3041.27', ''), 'C': ('304.12', '')})

    def test_early_single_net_without_reference(self):
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'FA1', '11415.22'), credit('C', 'AV1', '1141.51', 'FA1')]
        rows = reconcile_early(txn('10273.71', 'Alice Water Works REMI'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'C'})

    def test_early_quoted_anchor_with_remainder(self):
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'FV24-0214960', '3485.85'), credit('C', 'AB24-0200436', '1742.92', 'FV24-0214960'),
                 invoice('B', 'FV24-0216571', '848.34'), invoice('E', 'FV24-0216586', '975.62'),
                 invoice('G', 'FV24-0216999', '5000')]
        rows = reconcile_early(txn('3566.89', 'Alice Water Works PAGO FRA. FV24-0214960 FV24'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'C', 'B', 'E'})

    def test_early_yields_to_other_quoted_invoice(self):
        # A quoted open invoice outside the net solution keeps ordinary matching in charge.
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'FV24-0413922', '520.23'), credit('C', 'AB24-0400371', '78.03', 'FV24-0413922'),
                 invoice('B', 'FV24-0499999', '900')]
        self.assertIsNone(reconcile_early(txn('442.20', 'Alice Water Works FRA. FV24-0499999'), context(items)))

    def test_early_accepts_one_character_typo_of_solution(self):
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'FV24-0413922', '520.23'), credit('C', 'AB24-0400371', '78.03', 'FV24-0413922'),
                 invoice('B', 'FV24-0418922', '900')]
        rows = reconcile_early(txn('442.20', 'Alice Water Works FRA. FV24-0418922'), context(items))
        self.assertEqual(set(ids(rows)), {'A', 'C'})

    def test_early_skips_unanchored_subsets(self):
        from recon.credit_groups import reconcile_early
        items = [invoice('A', 'F1', '100'), invoice('B', 'F2', '200'), credit('C', 'AB1', '50', 'F2'),
                 invoice('E', 'F3', '999')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        self.assertIsNone(reconcile_early(txn('250', 'Alice Water Works PAYMENT'), context(items)))

    def test_outgoing_lines_ignored(self):
        items = [invoice('A', 'F1', '100'), credit('C', 'AB1', '30', 'F1')]
        self.assertIsNone(reconcile(txn('-70', 'Alice Water Works'), context(items)))


if __name__ == '__main__':
    unittest.main()
