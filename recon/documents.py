"""Explicit remittance evidence. No labels are used and balances are not mutated.

Advice caching is opt-in with ctx.document_cache_dir or RECON_DOCUMENT_CACHE_DIR.
Keys include source content, parser version, metadata and PDF backend identity.
Only successfully parsed evidence is cached, never matching decisions or balances.
Corrupt/unreadable caches reparse; write failures retain the fresh parsed evidence.
Workers use ctx.document_workers or RECON_DOCUMENT_WORKERS (1–32, default 8);
submission windows cap pending parses at twice that count. This bounds concurrent
work, not the total memory needed for retained parsed advices and invoice indexes.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import csv
import hashlib
import json
import os
import tempfile
import re
import shutil
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

ADVICE_CACHE_VERSION = 1
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

def _advice_payload(advice):
    data = asdict(advice)
    data.pop('source')  # Source is rebound to the current verified input path.
    for line in data['lines']:
        line['gross'], line['paid'] = str(line['gross']), str(line['paid'])
    return data


def _restore_advice(data, path):
    if not isinstance(data, dict) or type(data.get('short_refs')) is not bool:
        raise ValueError('Invalid advice cache object')
    fields = ('reference', 'entity', 'payer', 'received', 'currency', 'payment_date')
    if any(not isinstance(data.get(k), str) for k in fields):
        raise ValueError('Invalid advice cache metadata')
    if not isinstance(data.get('lines'), list) or not data['lines']:
        raise ValueError('Empty or invalid cached advice lines')
    lines = []
    for line in data['lines']:
        if not isinstance(line, dict) or any(not isinstance(line.get(k), str) for k in
                                          ('number', 'gross', 'paid', 'reason', 'document_date')):
            raise ValueError('Invalid advice line types')
        gross, paid = Decimal(line['gross']), Decimal(line['paid'])
        if not gross.is_finite() or not paid.is_finite():
            raise ValueError('Non-finite advice amounts')
        lines.append(Line(line['number'], gross, paid, line['reason'], line['document_date']))
    return Advice(**{k: data[k] for k in fields}, lines=lines, source=str(path), short_refs=data['short_refs'])


def cached_parse_advice(path, meta, pdf_tool, cache_dir, backend=''):
    """Return (advice, cache outcome, write_failed); cache is only an optimization."""
    if cache_dir is None or (path.suffix == '.pdf' and not pdf_tool):
        return parse_advice(path, meta, pdf_tool), 'disabled', False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    identity = json.dumps({'version': ADVICE_CACHE_VERSION, 'sha256': digest.hexdigest(),
                           'format': path.suffix, 'backend': backend if path.suffix == '.pdf' else '',
                           'metadata': {k: meta.get(k, '') for k in ('entity', 'payer_name', 'received_date')}},
                          sort_keys=True, separators=(',', ':'))
    key = hashlib.sha256(identity.encode()).hexdigest()
    cache = Path(cache_dir) / (key + '.json')
    status = 'miss'
    try:
        envelope = json.loads(cache.read_text(encoding='utf-8'))
        data = envelope['advice']
        payload = json.dumps(data, sort_keys=True, separators=(',', ':'))
        if (envelope.get('identity') != identity or
                envelope.get('payload_sha256') != hashlib.sha256(payload.encode()).hexdigest()):
            raise ValueError('Advice cache identity or checksum mismatch')
        return _restore_advice(data, path), 'hit', False
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        status = 'corrupt'
    advice = parse_advice(path, meta, pdf_tool)
    if advice is None:
        return None, status, False  # Unsupported/failed parsing is never cached as evidence.
    temporary = None
    try:
        data = _advice_payload(advice)
        payload = json.dumps(data, sort_keys=True, separators=(',', ':'))
        envelope = {'identity': identity, 'advice': data,
                    'payload_sha256': hashlib.sha256(payload.encode()).hexdigest()}
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache.parent,
                                         prefix='.' + key, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(envelope, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cache)
        return advice, status, False
    except OSError:
        return advice, status, True
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def document_workers(ctx):
    value = getattr(ctx, 'document_workers', os.environ.get('RECON_DOCUMENT_WORKERS', '8'))
    try:
        return max(1, min(32, int(value)))
    except (TypeError, ValueError):
        return 8

def _earliest_phase_booking(phase_dir):
    """Return the earliest booking_date in the phase's bank transactions, or None."""
    import gzip
    for suffix in ('.csv.gz', '.csv'):
        path = Path(phase_dir) / ('bank_transactions' + suffix)
        if path.exists():
            opener = gzip.open if suffix.endswith('.gz') else open
            earliest = None
            with opener(path, 'rt', encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    d = row.get('booking_date', '')
                    if d and (earliest is None or d < earliest):
                        earliest = d
            return earliest
    return None


def _historical_advice_entries(history_dirs, cutoff_start, cutoff_end):
    """Yield (root, meta_row) for advices with received_date in [cutoff_start, cutoff_end).

    cutoff_start is inclusive, cutoff_end (the first booking date) is exclusive.
    Absent or non-directory history dirs and missing index files are skipped.
    """
    for hdir in history_dirs:
        root = Path(hdir) / 'remittance_advices'
        index = root / 'index.csv'
        if not index.exists():
            continue
        with index.open(encoding='utf-8-sig') as f:
            for meta in csv.DictReader(f):
                rd = meta.get('received_date', '')
                if rd and cutoff_start <= rd < cutoff_end:
                    yield root, meta


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
        workers = document_workers(ctx)
        cache_dir = getattr(ctx, 'document_cache_dir', os.environ.get('RECON_DOCUMENT_CACHE_DIR')) or None
        backend = str(tool or '')
        if tool:
            try:
                stat = Path(tool).stat()
                backend += f'|{stat.st_size}|{stat.st_mtime_ns}'
            except OSError:
                pass
        self.stats = {'pdf_tool_available': bool(tool), 'files_total': 0, 'parsed': 0,
                      'skipped_pdfs': 0, 'parse_errors': 0, 'unsupported_or_empty': 0, 'warnings': [], 'changed_layout_files': 0,
                      'changed_layout_rows': 0, 'short_reference_unresolved_rows': 0,
                      'short_reference_ambiguous_rows': 0, 'workers': workers, 'max_in_flight': workers * 2,
                      'cache_enabled': cache_dir is not None, 'cache_version': ADVICE_CACHE_VERSION,
                      'cache_hits': 0, 'cache_misses': 0, 'cache_corruptions': 0, 'cache_write_errors': 0}
        metadata = []
        if index.exists():
            with index.open(encoding='utf-8-sig') as f:
                metadata = [(root, meta) for meta in csv.DictReader(f)]
        earliest = _earliest_phase_booking(ctx.phase_dir)
        historical = []
        if earliest:
            from datetime import timedelta
            cutoff_start = (date.fromisoformat(earliest) - timedelta(days=31)).isoformat()
            historical = list(_historical_advice_entries(
                getattr(ctx, 'history_dirs', ()), cutoff_start, earliest))
        # The same physical source may be reachable through repeated/symlinked
        # history dirs. Do not index it twice and manufacture an ambiguous match.
        def source_key(entry):
            folder, meta = entry
            return ((folder / meta['file']).resolve(), tuple(sorted(meta.items())))
        seen = {source_key(entry) for entry in metadata}
        self.stats['historical_files_total'] = 0
        historical_paths = set()
        for entry in historical:
            key = source_key(entry)
            if key not in seen:
                metadata.append(entry)
                seen.add(key)
                self.stats['historical_files_total'] += 1
                historical_paths.add(key[0])
        self.stats['files_total'] = len(metadata)
        self.stats['skipped_pdfs'] = sum(Path(m['file']).suffix == '.pdf' for _, m in metadata) if not tool else 0
        if self.stats['skipped_pdfs']:
            self.stats['warnings'].append(f"pdftotext unavailable: {self.stats['skipped_pdfs']} PDF remittance advices were skipped")
        def parse(entry):
            source_root, meta = entry
            try:
                advice, status, write_failed = cached_parse_advice(source_root / meta['file'], meta, tool, cache_dir, backend)
                return advice, status, write_failed, None
            except (ValueError, KeyError, OSError, InvalidOperation, ET.ParseError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
                return None, 'error', False, (meta['file'], str(exc))
        def results(pool):
            # executor.map eagerly submits inputs; bounded windows cap pending work.
            for start in range(0, len(metadata), workers * 2):
                yield from pool.map(parse, metadata[start:start + workers * 2])
        seen_evidence = set()
        self.stats['duplicate_historical_advices'] = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for advice, status, write_failed, error in results(pool):
                if error:
                    self.errors.append(error)
                if status in ('hit', 'miss', 'corrupt'):
                    self.stats[{'hit':'cache_hits', 'miss':'cache_misses', 'corrupt':'cache_corruptions'}[status]] += 1
                if write_failed:
                    self.stats['cache_write_errors'] += 1
                if advice is None:
                    continue
                # SAMPLE can contain a copy of a prior-phase advice. Identical
                # parsed facts are one piece of evidence, not competing matches.
                # Preserve genuinely different advice (including deductions).
                evidence = json.dumps(_advice_payload(advice), sort_keys=True)
                if Path(advice.source).resolve() in historical_paths and evidence in seen_evidence:
                    self.stats['duplicate_historical_advices'] += 1
                    continue
                seen_evidence.add(evidence)
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
        self.stats['unsupported_or_empty'] = len(metadata) - self.advice_count - len(self.errors) - self.stats['skipped_pdfs'] - self.stats['duplicate_historical_advices']
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

    def _br_ret_withholding(self, line, item, balance):
        """Disambiguate Brazil's RET advice code with policy and master evidence.

        RET alone also describes non-tax retention. Require an AR invoice owned
        by a Brazilian customer and a full, unpaid invoice whose net cash equals
        a policy tax rate. Never clear an unexplained or already-part-paid gap.
        """
        if (norm(line.reason) != 'RET' or item.get('entity') != 'BR01'
                or item.get('side') != 'AR' or item.get('kind') != 'invoice'
                or not item.get('item_id', '').startswith('ARI-')):
            return False
        customer = getattr(self.ctx, 'customers', {}).get(item.get('party_id'), {})
        if customer.get('country') != 'BR':
            return False
        total = money(item.get('total_amount', '0'))
        if total <= 0 or balance != total or line.gross != total or line.paid <= 0:
            return False
        return any(abs(line.paid - (total * (1 - rate)).quantize(
            CENT, rounding=ROUND_HALF_UP)) <= CENT
            for rate in (Decimal('0.0125'), Decimal('0.015'), Decimal('0.10')))

    def _unlabelled_discount(self, txn, line, item, balance):
        """Recognize an omitted advice reason only from exact contractual terms.

        An explicit reason takes precedence. A previously part-paid invoice,
        tax-rate coincidence or small cash gap cannot establish a discount.
        The caller still resolves the entire advice and proves its cash total.
        """
        if (norm(line.reason) or item.get('side') != 'AR'
                or item.get('kind') != 'invoice'
                or not item.get('item_id', '').startswith('ARI-')):
            return False
        total = money(item.get('total_amount', '0'))
        if total <= 0 or balance != total or line.gross != total or not 0 < line.paid < total:
            return False
        terms = getattr(self.ctx, 'payment_terms', {}).get(item.get('payment_terms'), {})
        if not terms.get('discount_days'):
            return False
        try:
            pct = Decimal(str(terms.get('early_payment_discount_pct') or '0'))
            elapsed = (date.fromisoformat(txn['booking_date']) - date.fromisoformat(item['issue_date'])).days
            discount_days = int(terms['discount_days'])
            deadline = discount_days + 3
        except (ValueError, KeyError, TypeError, InvalidOperation):
            return False
        if not pct.is_finite() or discount_days < 0 or not 0 < pct < 100 or not 0 <= elapsed <= deadline:
            return False
        expected = (total * (1 - pct / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
        return abs(line.paid - expected) <= CENT

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
                elif self._br_ret_withholding(line, item, balance):
                    exc, amount = 'SHORT_WITHHOLDING', balance
                elif any(x in reason for x in ('WITHHOLD','RETENCIONFISCAL','RETENTIONTAX')):
                    exc, amount = 'SHORT_WITHHOLDING', balance
                elif self._unlabelled_discount(txn, line, item, balance):
                    exc, amount = 'SHORT_DISCOUNT', balance
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
