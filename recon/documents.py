"""Explicit remittance evidence. No labels are used and account state is not mutated."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import csv
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

CENT = Decimal('0.01')
MONEY = r'-?\d[\d.,]*[.,]\d{2}'

def norm(value):
    return re.sub('[^A-Z0-9]', '', unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().upper())

def money(value):
    value = str(value).strip().replace(' ', '')
    if ',' in value and '.' in value:
        sep = ',' if value.rfind(',') > value.rfind('.') else '.'
        value = value.replace('.' if sep == ',' else ',', '').replace(',', '.')
    elif ',' in value:
        value = value.replace(',', '.')
    return Decimal(value or '0').quantize(CENT)

@dataclass
class Line:
    number: str
    gross: Decimal
    paid: Decimal
    reason: str = ''
    document_date: str = ''

@dataclass
class Advice:
    reference: str
    entity: str
    payer: str
    received: str
    currency: str
    lines: list
    source: str
    payment_date: str = ''
    short_refs: bool = False

    @property
    def total(self):
        return sum((x.paid for x in self.lines), Decimal(0))

def xlsx_rows(path):
    """Read cached cell values from the simple OOXML advice sheets, no formulas executed."""
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            shared = [''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        out = []
        for row in root.findall('.//m:sheetData/m:row', ns):
            vals = {}
            for c in row:
                letters = re.match('[A-Z]+', c.attrib['r']).group()
                idx = 0
                for ch in letters:
                    idx = idx * 26 + ord(ch) - 64
                typ = c.attrib.get('t')
                val = c.find('m:v', ns)
                value = ''.join(c.find('m:is', ns).itertext()) if typ == 'inlineStr' and c.find('m:is', ns) is not None else (val.text if val is not None else '')
                if typ == 's':
                    value = shared[int(value)]
                vals[idx - 1] = value
            out.append([vals.get(i, '') for i in range(max(vals, default=-1) + 1)])
        return out

def parse_advice(path, meta, pdf_tool=None):
    path = Path(path)
    ref = currency = payment_date = ''
    lines = []
    short_refs = False
    if path.suffix == '.csv':
        text = path.read_text(encoding='utf-8-sig')
        if not text.strip():
            return None
        rows = list(csv.reader(text.splitlines(), delimiter=';' if ';' in text.splitlines()[0] else ','))
        header = dict(zip(rows[0][::2], rows[0][1::2]))
        ref, currency, payment_date = header.get('payment_ref', ''), header.get('currency', ''), header.get('payment_date', '')
        for row in csv.DictReader(text.splitlines()[1:], delimiter=';' if ';' in text.splitlines()[0] else ','):
            if row.get('document', '').upper() == 'TOTAL' or not row.get('document'):
                continue
            lines.append(Line(row['document'], money(row['gross']), money(row['paid']), row.get('reason', '')))
    elif path.suffix == '.xlsx':
        rows = xlsx_rows(path)
        header_idx = next((i for i, row in enumerate(rows) if row and norm(row[0]) in ('DOCUMENT', 'VOUCHER')), None)
        if header_idx is None:
            return None
        for row in rows[:header_idx]:
            if row and norm(row[0]) in ('PAYMENTADVICE', 'AVISODEPAGO'):
                ref = row[1] if len(row) > 1 else ''
                payment_date = row[2] if len(row) > 2 else ''
        headers = [norm(v) for v in rows[header_idx]]
        short_refs = headers == ['VOUCHER','INVOICE','PO','INVDATE','AMOUNT','DEDUCTION','NETPAID']
        for row in rows[header_idx + 1:]:
            if short_refs:
                if len(row) >= 7 and row[0] and norm(row[0]) != 'TOTAL':
                    # The drift template provides a deduction amount but no reason.
                    # Preserve paid amount; never invent a dispute/discount reason.
                    lines.append(Line(row[1], money(row[4]), money(row[6]), '', row[3]))
            elif len(row) >= 6 and row[0] and norm(row[0]) != 'TOTAL':
                lines.append(Line(row[0], money(row[2]), money(row[5]), row[4]))
    else:
        if path.suffix == '.pdf':
            if not pdf_tool:
                return None
            text = subprocess.run([pdf_tool, '-layout', str(path), '-'], capture_output=True, text=True, timeout=20, check=True).stdout
        elif path.suffix == '.txt':
            text = path.read_text(encoding='utf-8-sig')
        else:
            return None
        ref_match = re.search(r'(?:Subject:\s*(?:Aviso de pago|Payment advice)|Payment ref\.|Ref\.|Referencia(?: de pago)?)[ :\t]*([^\s]+)', text, re.I)
        if ref_match:
            ref = ref_match[1]
        cur = re.search(MONEY + r'\s+([A-Z]{3})\b', text)
        if cur:
            currency = cur[1]
        for line in text.splitlines():
            m = re.match(r'^\s*(\S+)\s+\d{2}/\d{2}/\d{2,4}\s+(' + MONEY + r')(.*)$', line)
            if not m:
                continue
            number, gross, tail = m.groups()
            amounts = re.findall(MONEY, tail)
            gross = money(gross)
            if path.suffix == '.txt':
                deduction = money(amounts[0]) if amounts else Decimal(0)
                paid = gross + deduction
            else:
                paid = money(amounts[-1]) if amounts else gross
            reason = re.sub(MONEY, '', tail).strip()
            lines.append(Line(number, gross, paid, reason))
    if not lines:
        return None
    return Advice(ref, meta.get('entity', ''), meta.get('payer_name', ''), meta.get('received_date', ''), currency, lines, str(path), payment_date, short_refs)

class DocumentMatcher:
    """Complete-group matching with exact advice reference or payer+cash evidence."""
    def __init__(self, ctx):
        self.ctx = ctx
        self.by_ref = defaultdict(list)
        self.by_amount = defaultdict(list)
        self.errors = []
        root = Path(ctx.phase_dir) / 'remittance_advices'
        index = root / 'index.csv'
        self.advice_count = 0
        self.document_aliases = defaultdict(list)
        self.short_aliases = defaultdict(list)
        self.payer_ids = defaultdict(set)
        self._short_requests = defaultdict(set)
        self._changed_advices = []
        for pid, party in getattr(ctx, 'customers', {}).items():
            for key in ('name', 'trade_name'):
                if party.get(key):
                    self.payer_ids[(party.get('entity'), norm(party[key]))].add(pid)
        self._missing_numbers = set()
        tool = shutil.which('pdftotext')
        self.stats = {'pdf_tool_available': bool(tool), 'files_total': 0, 'parsed': 0,
                      'skipped_pdfs': 0, 'parse_errors': 0, 'unsupported_or_empty': 0, 'warnings': [], 'changed_layout_files': 0,
                      'changed_layout_rows': 0, 'short_reference_unresolved_rows': 0,
                      'short_reference_ambiguous_rows': 0}
        if not index.exists():
            return
        with index.open(encoding='utf-8-sig') as f:
            metadata = list(csv.DictReader(f))
        self.stats['files_total'] = len(metadata)
        self.stats['skipped_pdfs'] = sum(Path(m['file']).suffix == '.pdf' for m in metadata) if not tool else 0
        if self.stats['skipped_pdfs']:
            self.stats['warnings'].append(f"pdftotext unavailable: {self.stats['skipped_pdfs']} PDF remittance advices were skipped")
        def parse(meta):
            try:
                return parse_advice(root / meta['file'], meta, tool)
            except (ValueError, KeyError, OSError, InvalidOperation, ET.ParseError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
                self.errors.append((meta['file'], str(exc)))
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            for advice in pool.map(parse, metadata):
                if advice is None:
                    continue
                self.advice_count += 1
                if advice.short_refs:
                    self._changed_advices.append(advice)
                    self.stats['changed_layout_files'] += 1
                    self.stats['changed_layout_rows'] += len(advice.lines)
                    for pid in self.payer_ids.get((advice.entity, norm(advice.payer)), ()):
                        for line in advice.lines:
                            n = norm(line.number)
                            if n.isdigit() and 3 <= len(n) <= 7:
                                self._short_requests[(advice.entity, pid)].add(n.lstrip('0') or '0')
                self._missing_numbers.update(norm(line.number) for line in advice.lines if norm(line.number) not in getattr(ctx, 'by_number', {}) and norm(line.number).isdigit() and len(norm(line.number)) >= 7)
                if advice.reference:
                    ref = norm(advice.reference)
                    self.by_ref[ref].append(advice)
                    # Banks truncate references; index useful long prefixes as well.
                    for size in (10, 12, 14, 16):
                        if len(ref) > size:
                            self.by_ref[ref[:size]].append(advice)
                self.by_amount[advice.total].append(advice)
        self.stats['parsed'] = self.advice_count
        self.stats['parse_errors'] = len(self.errors)
        self.stats['unsupported_or_empty'] = len(metadata) - self.advice_count - len(self.errors) - self.stats['skipped_pdfs']
        # The changed advice layout can omit invoice prefixes. Index only aliases
        # actually required by documents, then still require entity+exact gross.
        if self._missing_numbers:
            for number, ids in getattr(ctx, 'by_number', {}).items():
                variants = {re.sub(r'^[A-Z]+', '', number), number[-7:]}
                for alias in variants & self._missing_numbers:
                    self.document_aliases[alias].extend(ids)

        # Build only requested suffixes once, scoped by confirmed payer+entity.
        # This deliberately does not broaden the global document-number index.
        if self._short_requests:
            for iid, item in ctx.items.items():
                scope = (item.get('entity'), item.get('party_id'))
                wanted = self._short_requests.get(scope)
                if not wanted:
                    continue
                n = norm(item.get('document_number', ''))
                aliases = {(n[-size:].lstrip('0') or '0') for size in range(3,8) if len(n) >= size and n[-size:].isdigit()}
                for alias in wanted & aliases:
                    self.short_aliases[(*scope, alias)].append(iid)
        for advice in self._changed_advices:
            for line in advice.lines:
                candidates = self._line_candidates(line, advice)
                if not candidates:
                    self.stats['short_reference_unresolved_rows'] += 1
                elif len(candidates) > 1:
                    self.stats['short_reference_ambiguous_rows'] += 1

    def _line_candidates(self, line, advice):
        if advice.short_refs:
            n = norm(line.number)
            if not n.isdigit():
                return []
            alias = n.lstrip('0') or '0'
            ids = set()
            for pid in self.payer_ids.get((advice.entity, norm(advice.payer)), ()):
                ids.update(self.short_aliases.get((advice.entity, pid, alias), ()))
        else:
            ids = self.ctx.by_number.get(norm(line.number), []) or self.document_aliases.get(norm(line.number), [])
        candidates = [self.ctx.items[i] for i in ids if self.ctx.items[i].get('entity') == advice.entity]
        candidates = [x for x in candidates if abs(money(x.get('total_amount', '0')) - abs(line.gross)) <= CENT]
        if line.document_date:
            candidates = [x for x in candidates if x.get('issue_date') == line.document_date]
        return candidates

    def _rows(self, txn, advice, strong_ref):
        if advice.entity and txn.get('entity') != advice.entity:
            return None
        try:
            if advice.received and abs((date.fromisoformat(advice.received) - date.fromisoformat(txn['booking_date'])).days) > 31:
                return None
        except (ValueError, KeyError):
            return None
        # Resolve the whole group before comparing cash, so XLSX advice without
        # a currency header cannot silently treat USD invoice amounts as EUR.
        resolved, seen = [], set()
        for line in advice.lines:
            candidates = self._line_candidates(line, advice)
            candidates = [x for x in candidates if x.get('issue_date', '') <= txn['booking_date']]
            if len(candidates) != 1:
                return None
            item = candidates[0]
            iid = item['item_id']
            if iid in seen:
                return None
            seen.add(iid)
            resolved.append((line, item))
        currencies = {item.get('currency') for _, item in resolved}
        if len(currencies) != 1 or not next(iter(currencies), None):
            return None
        item_currency = next(iter(currencies))
        advice_currency = advice.currency or item_currency
        if item_currency != advice_currency:
            return None
        cash = abs(money(txn['amount']))
        fx = advice_currency != txn.get('currency')
        if fx:
            if not txn.get('orig_amount') or txn.get('orig_currency') != advice_currency:
                return None
            cash = abs(money(txn['orig_amount']))
        if abs(advice.total - cash) > CENT:
            return None
        if not strong_ref:
            text = norm(' '.join(str(txn.get(k, '')) for k in ('text', 'counterparty_name', 'remittance_info')))
            payer = norm(advice.payer)
            if len(payer) < 7 or not any(payer[:n] in text for n in (min(14, len(payer)), min(20, len(payer)), len(payer))):
                return None
        out = []
        for line, item in resolved:
            iid = item['item_id']
            balance = money(item.get('open_amount', item['total_amount']))
            if balance <= 0 or abs(line.paid) > balance + CENT:
                return None
            credit = item.get('kind') == 'credit' or iid.startswith(('ARC-', 'APC-'))
            exc = ''
            amount = abs(line.paid)
            reason = norm(line.reason)
            if not credit and abs(line.paid) < balance - CENT:
                if any(x in reason for x in ('DISCOUNT','PRONTOPAGO','SKONTO')):
                    terms = getattr(self.ctx, 'payment_terms', {}).get(item.get('payment_terms'), {})
                    try:
                        days = (date.fromisoformat(txn['booking_date']) - date.fromisoformat(item['issue_date'])).days
                        pct = Decimal(str(terms.get('early_payment_discount_pct', 0)))
                        expected = money(item['total_amount']) * (1 - pct / 100)
                        accepted = pct > 0 and 0 <= days <= int(terms.get('discount_days', 0)) + 3 and abs(expected.quantize(CENT) - amount) <= CENT
                    except (ValueError, KeyError):
                        accepted = False
                    exc = 'SHORT_DISCOUNT' if accepted else 'PARTIAL'
                    if accepted:
                        amount = balance
                elif any(x in reason for x in ('WITHHOLD','RETENCIONFISCAL','RETENTIONTAX')):
                    exc, amount = 'SHORT_WITHHOLDING', balance
                else:
                    exc = 'SHORT_DISPUTE' if reason else 'PARTIAL'
            if fx and not credit and exc not in ('PARTIAL', 'SHORT_DISPUTE'):
                exc = 'FX_DIFFERENCE'
            out.append({'bank_txn_id': txn['bank_txn_id'], 'item_id': iid, 'amount': f'{amount:.2f}', 'category': '', 'exception': exc})
        return out

    def reconcile(self, txn):
        if money(txn['amount']) <= 0:
            return None
        chunks = [str(txn.get(k, '')) for k in ('text', 'ref1', 'ref2', 'remittance_info', 'end_to_end_id')]
        keys = {norm(x) for chunk in chunks for x in re.split(r'[\s|:;,]+', chunk) if len(norm(x)) >= 7}
        candidates, seen = [], set()
        for key in keys:
            for advice in self.by_ref.get(key, []):
                if id(advice) not in seen:
                    candidates.append((advice, True)); seen.add(id(advice))
        # A recognized reference is stronger evidence than a coincidental cash
        # amount. If its group fails validation, do not substitute another advice.
        if not candidates:
            amount = abs(money(txn.get('orig_amount') or txn['amount']))
            for advice in self.by_amount.get(amount, []):
                if id(advice) not in seen:
                    candidates.append((advice, False)); seen.add(id(advice))
        matches = [rows for advice, strong in candidates if (rows := self._rows(txn, advice, strong))]
        return matches[0] if len(matches) == 1 else None
