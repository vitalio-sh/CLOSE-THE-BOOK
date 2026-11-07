"""Independent structural validation without hidden labels."""
import csv,gzip,io
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .context import read_csv
FIELDS=['bank_txn_id','item_id','amount','category','exception']
EXCEPTIONS={'','PARTIAL','OVERPAYMENT','SHORT_DISCOUNT','SHORT_BANK_CHARGES','SHORT_WITHHOLDING','SHORT_DISPUTE','DUPLICATE_PAYMENT','UNIDENTIFIED','RETURNED','FX_DIFFERENCE'}

def validate(path,ctx):
    path=Path(path); errors=[]; seen=set(); modes={}; covered=set(); n=0
    ids={r['bank_txn_id'] for r in read_csv(ctx.phase_dir/'bank_transactions.csv.gz')}
    def err(message):
        if len(errors)<50: errors.append(message)
    opener=gzip.open if path.suffix=='.gz' else open
    size=0
    with opener(path,'rb') as raw:
        for chunk in iter(lambda:raw.read(1024*1024),b''):
            size+=len(chunk)
    if size>60*1024*1024:err('Uncompressed size exceeds 60 MiB')
    with opener(path,'rt',encoding='utf-8',newline='') as f:
        reader=csv.DictReader(f)
        if reader.fieldnames != FIELDS:err('Noncanonical header')
        for n,row in enumerate(reader,1):
            if None in row or any(value is None for value in row.values()):
                err(f'{n}: wrong field count');continue
            tid=row.get('bank_txn_id','');iid=row.get('item_id','');cat=row.get('category','');exc=row.get('exception','')
            if tid not in ids:err(f'{n}: unknown transaction {tid}')
            covered.add(tid)
            if (tid,iid) in seen:err(f'{n}: duplicate pair')
            seen.add((tid,iid));mode=bool(iid)
            if tid in modes and modes[tid]!=mode:err(f'{n}: mixed item/category rows')
            modes[tid]=mode
            if exc not in EXCEPTIONS:err(f'{n}: unknown exception')
            if iid:
                if iid not in ctx.items:err(f'{n}: unknown item {iid}')
                if cat:err(f'{n}: item has category')
                try:
                    amount=Decimal(row['amount'])
                    if not amount.is_finite() or amount<0:err(f'{n}: invalid amount')
                except (InvalidOperation,KeyError):err(f'{n}: invalid amount')
            else:
                if row.get('amount'):err(f'{n}: amount without item')
                if cat not in ctx.categories:err(f'{n}: unknown category')
    if n>3000000:err('Too many rows')
    missing=ids-covered
    if missing:err(f'{len(missing)} missing transactions')
    return {'ok':not errors,'rows':n,'transactions':len(ids),'covered':len(covered & ids),'missing':len(missing),'uncompressed_bytes':size,'errors':errors}

def official_structure(path,ctx):
    """Use organizer parser with known phase IDs but no labels; discard bogus metrics."""
    import importlib.util
    source=Path(__file__).resolve().parents[1]/'vendor/starter/bankrec_score.py'
    spec=importlib.util.spec_from_file_location('official_bankrec_scorer',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    empty=module.Golden()
    empty.exceptions=sorted(EXCEPTIONS-{''});empty.categories=sorted(ctx.categories)
    empty.txns={r['bank_txn_id']:({},'','','unlabelled') for r in read_csv(ctx.phase_dir/'bank_transactions.csv.gz')}
    result=module.score_submission(empty,Path(path).read_bytes(),str(path))
    return {'ok':result['ok'],'errors':result['errors'],'note':'Format only; no hidden labels or accuracy estimate.'}
