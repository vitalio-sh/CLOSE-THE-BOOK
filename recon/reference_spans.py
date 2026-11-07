"""Pure classification of ORIGINAL numeric spans; no identity/amount guessing.

A list is not safe merely because it looks like references. Every component must
be independently unique in the caller's scoped catalogue, including closed items.
"""
from datetime import date
import re
import unicodedata
from typing import NamedTuple, Tuple


class NumericReferenceSpan(NamedTuple):
    kind: str  # plain, reference_list, unsafe
    components: Tuple[str, ...]
    start: int
    end: int
    reason: str


# These markers are not currency codes when immediately preceding a number.
_DOCUMENT_MARKERS = {'INV', 'FAC', 'FRA', 'REF', 'NO', 'NR', 'N', 'PO', 'OC'}
_MONEY_WORDS = {'TOTAL', 'AMOUNT', 'IMPORTE', 'MONTANT', 'BETRAG', 'SUMA', 'CASH',
                'PCT', 'PERCENT', 'PERCENTAGE', 'BPS', 'BASISPOINTS', 'PORCENTAJE',
                'EURO', 'EUROS', 'DOLLAR', 'DOLLARS', 'PESO', 'PESOS', 'POUND', 'POUNDS',
                'STERLING', 'RUPEE', 'RUPEES', 'FRANC', 'FRANCS', 'YEN', 'YUAN',
                'RUBLE', 'RUBLES', 'ROUBLE', 'ROUBLES', 'РУБЛЕЙ', 'РУБЛИ', 'РУБЛЬ',
                'ГРИВЕНЬ', 'ГРИВНІ', 'ДОЛЛАРОВ', 'ЕВРО', 'REAI', 'REAIS', 'REAL',
                'DÓLAR', 'DÓLARES', 'LIRA', 'LIRAS', 'LIRE', 'ZLOTY', 'ZŁOTYCH',
                'KORUNA', 'KRONOR', 'KRONE', 'DIRHAM', 'DIRHAMS', 'DINAR', 'DINARS',
                'RIYAL', 'RIYALS', 'CENT', 'CENTS', 'CÉNTIMO', 'CÉNTIMOS', 'CENTIMO', 'CENTIMOS',
                'CENTAVO', 'CENTAVOS', 'CENTIME', 'CENTIMES', 'PENNY', 'PENNIES', 'PENCE',
                'GROSS', 'NET', 'NETTO', 'NETO', 'BRUTTO', 'BRUTO'}
_PERCENT = '%‰‱'
# Non-list numeric separators stay in the run so they cannot expose a misleading
# leading integer. They are rejected after the entire run is inspected.
_RUN = re.compile(r"\d+(?:[\s,;:/.'’‘\u066b\u066c\uff0c\uff0e\uff0f\-\u2212\u2013]+\d+)*")
_CONNECTED = re.compile(r'[A-Za-z0-9_./-]+')
_WORD_LEFT = re.compile(r'([^\W\d_]+)\s*$', re.UNICODE)
_WORD_RIGHT = re.compile(r'^\s*([^\W\d_]+)(?!\w)', re.UNICODE)


def _calendar(value):
    if len(value) == 4 and 1900 <= int(value) <= 2100:
        return True
    if len(value) != 8:
        return False
    alternatives = [(int(value[:4]), int(value[4:6]), int(value[6:])),
                    (int(value[4:]), int(value[2:4]), int(value[:2])),
                    (int(value[4:]), int(value[:2]), int(value[2:4]))]
    for year, month, day in alternatives:
        if 1900 <= year <= 2100:
            try:
                date(year, month, day)
            except ValueError:
                continue
            return True
    return False


def _unit_after(text):
    tail = text.lstrip(' \t\r\n\u00a0\u202f()[]{}:=,;/-–—')
    if not tail:
        return False
    # Any Unicode currency symbol, optionally decorated R$/US$/HK$/etc.
    prefix = re.match(r'[A-Za-z]{0,3}\s*', tail)
    rest = tail[prefix.end():]
    if rest and (unicodedata.category(rest[0]) == 'Sc' or rest[0] in _PERCENT):
        return True
    if tail[0] in _PERCENT or unicodedata.category(tail[0]) == 'Sc':
        return True
    word = _WORD_RIGHT.match(tail)
    if word:
        value = word[1].upper()
        # A short right-hand unit is ambiguous: COP/XAF/KRW/Kč/zł/kr, etc.
        return value in _MONEY_WORDS or (len(value) <= 3 and value not in _DOCUMENT_MARKERS)
    return False


def _unit_before(text):
    head = text.rstrip(' \t\r\n\u00a0\u202f()[]{}:=,;/-–—')
    if not head:
        return False
    if unicodedata.category(head[-1]) == 'Sc' or head[-1] in _PERCENT:
        return True
    word = _WORD_LEFT.search(head.rstrip(':= '))
    if word:
        value = word[1].upper()
        return value in _MONEY_WORDS or (len(value) <= 3 and value not in _DOCUMENT_MARKERS)
    return False


def classify_numeric_reference_span(raw, start, end):
    """Classify an original token or complete components within one numeric run.

    All component queries return the SAME maximal run bounds/components/status.
    A query cutting a digit component or crossing an alphanumeric ID is unsafe.
    No normalization, punctuation stripping, or identity catalogue is used here.
    """
    invalid = NumericReferenceSpan('unsafe', (), -1, -1, 'invalid_span')
    if (not isinstance(raw, str) or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(raw)):
        return invalid
    # Numeric portions of explicit alphanumeric IDs are not standalone tokens.
    masked = list(raw)
    for item in _CONNECTED.finditer(raw):
        if re.match(r'[A-Za-z_]', item[0]):
            for index in range(item.start(), item.end()):
                masked[index] = 'X'
    for run in _RUN.finditer(''.join(masked)):
        if not run.start() <= start < end <= run.end():
            continue
        numbers = list(re.finditer(r'[0-9]+', raw[run.start():run.end()]))
        beginnings = {run.start()+n.start() for n in numbers}
        endings = {run.start()+n.end() for n in numbers}
        components = tuple(n[0] for n in numbers)
        def result(kind, reason):
            return NumericReferenceSpan(kind, components, run.start(), run.end(), reason)
        if any(ch.isdecimal() and ch not in '0123456789' for ch in raw[run.start():run.end()]):
            return result('unsafe', 'localized_numeric_group')
        if start not in beginnings or end not in endings:
            return result('unsafe', 'partial_component')
        if _unit_before(raw[:run.start()]) or _unit_after(raw[run.end():]):
            return result('unsafe', 'monetary_or_percentage_unit')
        # A numeric-leading token ending in a unit must stay visible to the run
        # above (e.g. 50000,000000INR); other attached letters are not numbers.
        if ((run.start() and (raw[run.start()-1].isalpha() or raw[run.start()-1] == '_'))
                or (run.end() < len(raw) and (raw[run.end()].isalpha() or raw[run.end()] == '_'))):
            return result('unsafe', 'attached_alphanumeric')
        separators = re.sub(r'[0-9]', '', raw[run.start():run.end()])
        if any(not (ch.isspace() or ch in ',;/') for ch in separators):
            return result('unsafe', 'formatted_numeric_or_date')
        if any(_calendar(value) for value in components):
            return result('unsafe', 'calendar_number')
        if len(components) > 1:
            if any(len(value) < 5 for value in components):
                return result('unsafe', 'short_group_or_fraction')
            return result('reference_list', 'every_component_requires_unique_identity')
        return result('plain', 'requires_document_context_and_unique_identity')
    return invalid


def numeric_reference_span_safe(raw, start, end, *, known_reference_tokens=None):
    """Hard masks cannot be overridden; lists require all proven unique tokens."""
    span = classify_numeric_reference_span(raw, start, end)
    if span.kind == 'unsafe':
        return False
    if span.kind == 'plain':
        return True
    if known_reference_tokens is None:
        return False
    return all(value in known_reference_tokens for value in span.components)
