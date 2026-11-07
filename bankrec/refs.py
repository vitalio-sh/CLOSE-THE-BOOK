"""Document-number index tolerant to the noise described in POLICIES §5 (no prefix, no leading zeros, truncation)."""
from __future__ import annotations

import re
from collections import defaultdict

import polars as pl

_NON = re.compile(r"[^A-Z0-9]")
_TOK = re.compile(r"[A-Z0-9][A-Z0-9/\-\.]{3,}[A-Z0-9]")


def norm(s) -> str:
    return _NON.sub("", (s or "").upper())


def keys_for(doc: str) -> list:
    """Variants under which a printed document number may be quoted."""
    n = norm(doc)
    if not n:
        return []
    out = {n}
    digits = re.sub(r"\D", "", n)
    if len(digits) >= 4:
        out.add(digits)
        out.add(digits.lstrip("0") or "0")
    # part after the last separator: FV25-0108480 -> 0108480 / 108480
    parts = re.split(r"[-/\.\s]", (doc or "").upper())
    if len(parts) > 1 and len(parts[-1]) >= 4:
        last = norm(parts[-1])
        out.add(last)
        out.add(last.lstrip("0") or "0")
    # strip alpha prefix: RE250207950 -> 250207950
    m = re.match(r"^[A-Z]+(\d.*)$", n)
    if m and len(m.group(1)) >= 4:
        out.add(m.group(1))
    return [k for k in out if len(k) >= 4]


def tokens(text: str) -> list:
    """Candidate reference tokens of a bank text (normalised), plus digit-only variants."""
    t = (text or "").upper()
    out = []
    for m in _TOK.finditer(t):
        n = norm(m.group(0))
        if len(n) >= 4:
            out.append(n)
            d = re.sub(r"\D", "", n)
            if len(d) >= 5 and d != n:
                out.append(d)
                out.append(d.lstrip("0"))
    return out


class RefIndex:
    def __init__(self, items: pl.DataFrame, cols=("document_number",)):
        self.idx: dict = defaultdict(set)
        for row in items.select(["item_id", "entity", *cols]).iter_rows():
            iid, ent = row[0], row[1]
            for c in row[2:]:
                for k in keys_for(c):
                    self.idx[(ent, k)].add(iid)
                    self.idx[("*", k)].add(iid)

    def get(self, key: str, entity: str = "*") -> set:
        return self.idx.get((entity, key), set())

    def find(self, text: str, entity: str = "*") -> list:
        """Items whose number appears in the text, in order of appearance."""
        seen, out = set(), []
        for tok in tokens(text):
            for iid in self.get(tok, entity):
                if iid not in seen:
                    seen.add(iid)
                    out.append(iid)
        return out
