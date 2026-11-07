"""Bounded, optional, lazy invoice-PDF facts. Never reads labels or changes balances.

Attach ``ctx.pdf_facts = PdfFacts(ctx)``. ``get(iid)`` returns extracted hints;
``resolve(iid)`` returns a uniquely documented replacement or the original iid.
Budget exhaustion returns no new evidence, never a guessed fact. Runtime is
bounded by max_documents and a cumulative active-time budget; repeated reads
use a content-addressed, versioned disk cache with atomic writes and checksums.
Corrupt entries reparse within the same budget. Time when this module is unused
does not spend its budget. Cold/warm runs may discover different amounts of
optional evidence within a wall-time budget; enabling this mode is not a claim
of output determinism across cache states.
"""
from collections import defaultdict
from pathlib import Path
import csv
import hashlib
import json
import os
import tempfile
import re
import shutil
import subprocess
import time
import unicodedata

VERSION = 2
NS_MONTHS = [
    ('JANUARY','ENERO','JANVIER','JANUAR'),('FEBRUARY','FEBRERO','FEVRIER','FEBRUAR'),
    ('MARCH','MARZO','MARS','MARZ'),('APRIL','ABRIL','AVRIL'),('MAY','MAYO','MAI'),
    ('JUNE','JUNIO','JUIN','JUNI'),('JULY','JULIO','JUILLET','JULI'),
    ('AUGUST','AGOSTO','AOUT'),('SEPTEMBER','SEPTIEMBRE','SEPTEMBRE'),
    ('OCTOBER','OCTUBRE','OCTOBRE','OKTOBER'),('NOVEMBER','NOVIEMBRE','NOVEMBRE'),
    ('DECEMBER','DICIEMBRE','DECEMBRE','DEZEMBER')]
MONTHS = {v:i+1 for i,vs in enumerate(NS_MONTHS) for v in vs}
MONEY = r'\d[\d.,]*[.,]\d{2}'

def norm(s):
    return re.sub('[^A-Z0-9]', '', ascii_text(s))

def ascii_text(s):
    return unicodedata.normalize('NFKD',str(s)).encode('ascii','ignore').decode().upper()

def amount(s):
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):s=s.replace('.','').replace(',','.')
        else:s=s.replace(',','')
    elif ',' in s:s=s.replace(',','.')
    return s

def extract_facts(text):
    """Only extract explicit clauses, not ordinary invoice dates or bank details."""
    result = {}
    flat = re.sub(r'\s+', ' ', text)
    a = ascii_text(flat)
    replacement = re.search(r'(?:ANULA Y SUSTITUYE A (?:LA )?FACTURA|CANCELS AND REPLACES INVOICE)\s+([A-Z0-9][A-Z0-9/._-]*)', a)
    if replacement:result['replaces_document_number'] = replacement[1].rstrip('.')
    month = re.search(r'(?:MONTH OF|MES DE|MOIS DE|LEISTUNGSMONAT)\s+([A-Z]+)\s+(?:DE\s+)?(20\d{2})\b',a)
    if month and month[1] in MONTHS:
        result['service_month'] = f'{month[2]}-{MONTHS[month[1]]:02d}'
    retention = re.search(r'(?:RETENCION EN GARANTIA DEL|RETENTION)\s+(\d+(?:[.,]\d+)?)\s*%',a)
    if retention:
        result['retention_pct'] = retention[1].replace(',','.')
        release = re.search(r'(?:FINAL ACCEPTANCE|RECEPCION DEFINITIVA)\s*\((\d{2})/(\d{2})/(\d{4})\)',a)
        if release:result['retention_release_date']=f'{release[3]}-{release[2]}-{release[1]}'
    instal = re.search(r'(?:PAYMENT TERMS:|FORMA DE PAGO:)\s*(\d+)\s+(?:INSTALMENTS|VENCIMIENTOS)',a)
    if instal:result['instalments_count']=int(instal[1])
    advance = re.search(r'(?:LESS ADVANCE RECEIVED AGAINST PROFORMA|A DEDUCIR ANTICIPO RECIBIDO SEGUN PROFORMA)\s+([A-Z0-9/_-]+)(?:\s+DE|:)?\s+('+MONEY+')',a)
    if advance:
        result['advance_proforma']=advance[1]
        result['advance_amount']=amount(advance[2])
    net = re.search(r'(?:NET AMOUNT DUE|IMPORTE NETO A PAGAR|AMOUNT PAYABLE AT DUE DATE:|IMPORTE A PAGAR AL VENCIMIENTO:)\s*('+MONEY+')',a)
    if net:result['net_due']=amount(net[1])
    # Only a notice with explicit old/new wording supplies changed-account facts.
    change = re.search(r'(?:DESDE LA CUENTA|FROM ACCOUNT)\s+([A-Z]{2}\d{2}[A-Z0-9]{10,30})\s*,?\s*(?:EN SUSTITUCION DE LA ANTERIOR|REPLACING(?: THE PREVIOUS(?: ACCOUNT)?)?)\s+([A-Z]{2}\d{2}[A-Z0-9]{10,30})',a)
    if change:
        result['new_iban'],result['old_iban']=change[1],change[2]
    return result

def _content_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _facts_payload(result):
    if not isinstance(result, dict) or any(not isinstance(k, str) or
            not (isinstance(v, str) or (k == 'instalments_count' and type(v) is int))
            for k, v in result.items()):
        raise ValueError('Invalid cached PDF fact types')
    return json.dumps(result, sort_keys=True, separators=(',', ':'))


def _write_facts_cache(cache, identity, result):
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = _facts_payload(result)
    envelope = {'version': VERSION, 'identity': identity, 'facts': result,
                'payload_sha256': hashlib.sha256(payload.encode()).hexdigest()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache.parent,
                                         prefix='.' + cache.name, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(envelope, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cache)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

class PdfFacts:
    def __init__(self, ctx, *, enabled=True, max_documents=4000, max_party_documents=64,
                 timeout_seconds=3, max_seconds=30, cache_dir=None):
        self.ctx=ctx
        self.enabled=bool(enabled)
        self.max_documents=max(0,int(max_documents))
        self.max_party_documents=max(0,int(max_party_documents))
        self.timeout_seconds=max(0.001,float(timeout_seconds))
        self.max_seconds=max(0.0,float(max_seconds))
        self._active_seconds=0.0
        self.tool=shutil.which('pdftotext') if enabled else None
        self._backend = str(self.tool or '')
        if self.tool:
            try:
                stat = Path(self.tool).stat()
                self._backend += f'|{stat.st_size}|{stat.st_mtime_ns}'
            except OSError:
                pass
        self.cache_dir=Path(cache_dir) if cache_dir is not None else Path('outputs/pdf_facts_cache')
        self.by_item=defaultdict(list)
        self.by_party=defaultdict(list)
        self.by_notice=defaultdict(list)
        self._memory={}
        self._party_scanned=set()
        self._replacements=defaultdict(set)
        self.stats={'extractions':0,'cache_hits':0,'memory_hits':0,'failures':0,'timeouts':0,
                    'budget_skips':0,'time_budget_skips':0,'indexed':0,'active_seconds':0.0,
                    'max_seconds':self.max_seconds,'time_budget_exhausted':False,
                    'sources_with_facts':0,'replacement_links':0,'service_month_facts':0,
                    'pdf_tool_available':bool(self.tool),'enabled':self.enabled,
                    'cache_corruptions':0,'cache_write_errors':0}
        if not self.enabled:return
        # Current phase first; exact duplicate historical paths are harmless.
        roots=[Path(ctx.phase_dir)]+[Path(x) for x in getattr(ctx,'history_dirs',[])]
        seen=set()
        for root in roots:
            index=root/'pdfs/index.csv'
            if not index.exists():continue
            with index.open(encoding='utf-8-sig') as f:
                for record in csv.DictReader(f):
                    key=(record.get('entity'),record.get('party_id'),norm(record.get('document_number','')))
                    notice=record.get('document_type')=='bank_account_notice'
                    seen_key=key if not notice else (*key,record['file'])
                    if seen_key in seen:continue
                    ids=[iid for iid in ctx.by_number.get(key[2],[]) if ctx.items[iid].get('entity')==key[0] and ctx.items[iid].get('party_id')==key[1]]
                    if not ids and not notice:continue
                    seen.add(seen_key)
                    record={**record,'path':root/'pdfs'/record['file'],'item_ids':ids}
                    self.stats['indexed']+=1
                    if notice:
                        self.by_notice[(key[0],key[1])].append(record)
                        continue
                    for iid in ids:self.by_item[iid].append(record)
                    self.by_party[(key[0],key[1])].append(record)
        for records in self.by_party.values():
            records.sort(key=lambda r:(r.get('document_date',''),r['file']),reverse=True)

    def _read(self, record):
        path=record['path']
        if path in self._memory:
            self.stats['memory_hits']+=1
            return self._memory[path]
        if not self.enabled or not self.tool:return {}
        if self._active_seconds >= self.max_seconds:
            self.stats['time_budget_skips']+=1
            self.stats['time_budget_exhausted']=True
            return {}
        # Charge only active reads/extractions, not time between matcher calls.
        started=time.monotonic()
        def remaining():
            return self.max_seconds-self._active_seconds-(time.monotonic()-started)
        try:
            # Content identity catches edits even when size/mtime are preserved.
            identity = f'{VERSION}|{self._backend}|{_content_digest(path)}'
            cache = self.cache_dir / (hashlib.sha256(identity.encode()).hexdigest() + '.json')
            if remaining() <= 0:
                self.stats['time_budget_skips'] += 1
                return {}
            result = None
            try:
                entry = json.loads(cache.read_text())
                facts = entry['facts']
                payload = _facts_payload(facts)
                if (entry.get('version') != VERSION or entry.get('identity') != identity or
                        entry.get('payload_sha256') != hashlib.sha256(payload.encode()).hexdigest()):
                    raise ValueError('PDF cache identity or checksum mismatch')
                result = facts
                self.stats['cache_hits'] += 1
            except FileNotFoundError:
                pass
            except (OSError, ValueError, KeyError, TypeError):
                self.stats['cache_corruptions'] += 1
            if result is None:
                if self.stats['extractions'] >= self.max_documents:
                    self.stats['budget_skips'] += 1
                    return {}
                seconds_left = remaining()
                if seconds_left <= 0:
                    self.stats['time_budget_skips'] += 1
                    return {}
                self.stats['extractions'] += 1
                text = subprocess.run([self.tool, '-layout', str(path), '-'], capture_output=True, text=True,
                                      timeout=min(self.timeout_seconds, seconds_left), check=True).stdout
                result = extract_facts(text)
                if remaining() > 0:
                    try:
                        _write_facts_cache(cache, identity, result)
                    except OSError:
                        # A disposable cache failure must not discard parsed evidence.
                        self.stats['cache_write_errors'] += 1
            if result:self.stats['sources_with_facts']+=1
            if result.get('service_month'):self.stats['service_month_facts']+=1
            result={**result,'source':str(path),'document_number':record.get('document_number','')}
            self._memory[path]=result
            old=result.get('replaces_document_number')
            if old and len(record['item_ids'])==1:
                self._replacements[(record['entity'],record['party_id'],norm(old))].add(record['item_ids'][0])
                self.stats['replacement_links']+=1
            return result
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            self.stats['failures']+=1
            if isinstance(exc,subprocess.TimeoutExpired):self.stats['timeouts']+=1
            self._memory[path]={}
            return {}
        finally:
            self._active_seconds+=max(0.0,time.monotonic()-started)
            self.stats['active_seconds']=round(self._active_seconds,6)
            self.stats['time_budget_exhausted']=self._active_seconds>=self.max_seconds

    def get(self,item_id):
        records=self.by_item.get(item_id,[])
        return dict(self._read(records[0])) if len(records)==1 else {}

    def notices(self, party_id, entity):
        return [dict(f) for r in self.by_notice.get((entity,party_id),[])[:self.max_party_documents] if (f:=self._read(r))]

    def _scan_party(self,item):
        key=(item.get('entity'),item.get('party_id'))
        if key in self._party_scanned:return
        self._party_scanned.add(key)
        records=self.by_party.get(key,[])
        # New invoices are checked first because replacement documents are newer.
        for record in records[:self.max_party_documents]:
            if self._active_seconds>=self.max_seconds:
                self.stats['time_budget_exhausted']=True
                break
            self._read(record)

    def resolve(self,item_id):
        """Resolve only a unique documented replacement; no amount/name guesses."""
        if not self.enabled or item_id not in self.ctx.items:return item_id
        item=self.ctx.items[item_id]
        self._scan_party(item)
        seen={item_id}
        for _ in range(8):
            item=self.ctx.items[item_id]
            key=(item.get('entity'),item.get('party_id'),norm(item.get('document_number','')))
            matches=self._replacements.get(key,set())
            if len(matches)!=1:return item_id
            new=next(iter(matches))
            if new in seen:return item_id
            seen.add(new);item_id=new
        return item_id
