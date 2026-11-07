#!/usr/bin/env python3
"""Evaluate an independent TRAIN time window with oracle PRIOR balances only.

This is not a pure end-to-end holdout: prior golden allocations reconstruct bank
settlement balances and history. Target-window golden is used ONLY after solving,
by the official scorer. Non-bank cancellations are not inferred from labels.
"""
import argparse
from collections import Counter
import csv
from datetime import date
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import resource
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS = ['PARTIAL','OVERPAYMENT','SHORT_DISCOUNT','SHORT_BANK_CHARGES',
              'SHORT_WITHHOLDING','SHORT_DISPUTE','DUPLICATE_PAYMENT',
              'UNIDENTIFIED','RETURNED','FX_DIFFERENCE']

def read(path):
    path = Path(path)
    with (gzip.open if path.suffix == '.gz' else open)(path,'rt',encoding='utf-8-sig',newline='') as f:
        yield from csv.DictReader(f)

def write_csv(path, rows, fields):
    with gzip.open(path,'wt',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,lineterminator='\n');w.writeheader();w.writerows(rows)

def link(src,dest):
    dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists():dest.symlink_to(src.resolve(),target_is_directory=src.is_dir())

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',default=str(ROOT/'data/workspace'))
    p.add_argument('--start',default='2024-10-01')
    p.add_argument('--end',default='2024-10-31')
    p.add_argument('--out',default=str(ROOT/'outputs/window_oct2024'))
    a=p.parse_args(); date.fromisoformat(a.start);date.fromisoformat(a.end)
    if a.start>a.end:raise ValueError('start must precede end')
    started=time.monotonic();dest=Path(a.out).resolve();dest.mkdir(parents=True,exist_ok=True)
    if (dest/'report.json').exists():raise FileExistsError('Use a fresh output directory to preserve evaluation provenance')
    source=Path(a.data).resolve();train=source/'train'
    frozen=dest/'source_snapshot';package=frozen/'frozen_recon';package.mkdir(parents=True,exist_ok=True)
    hashes={}
    for path in sorted((ROOT/'recon').glob('*.py')):
        data=path.read_bytes();(package/path.name).write_bytes(data)
        hashes['recon/'+path.name]=hashlib.sha256(data).hexdigest()
    for path in [ROOT/'vendor/starter/bankrec_score.py',Path(__file__)]:
        data=path.read_bytes();(frozen/path.name).write_bytes(data)
        hashes[str(path.relative_to(ROOT))]=hashlib.sha256(data).hexdigest()
    (dest/'source_hashes.json').write_text(json.dumps(hashes,indent=2)+'\n')
    sys.path.insert(0,str(frozen))
    C=importlib.import_module('frozen_recon.context')
    B=importlib.import_module('frozen_recon.batches')
    D=importlib.import_module('frozen_recon.documents')
    M=importlib.import_module('frozen_recon.matching')
    V=importlib.import_module('frozen_recon.validate')
    S=importlib.import_module('bankrec_score')

    # Source inputs are selected by dates only, never by target labels.
    input_root=dest/'input';phase=input_root/'train';phase.mkdir(parents=True,exist_ok=True)
    link(source/'masters',input_root/'masters')
    txn_meta={};target=[]
    for txn in read(train/'bank_transactions.csv.gz'):
        day=txn['booking_date']
        if day<a.start:txn_meta[txn['bank_txn_id']]=(day,txn['account_id'])
        elif day<=a.end:target.append(txn)
    target.sort(key=lambda t:(t['booking_date'],t['bank_txn_id']))
    if not target:raise ValueError('Window has no transactions')
    fields=list(target[0]);write_csv(phase/'bank_transactions.csv.gz',target,fields)
    for folder in ('payment_runs','direct_debit_batches','card_settlements','cheque_deposits'):
        for path in (train/folder).glob('*.csv'):
            if path.name.startswith('._'):continue
            # Filenames documented as ENTITY[_BRANCH]_YYYYMM[_N].csv.
            import re
            months=re.findall(r'(?:^|_)(20\d{4})(?=_|\.)',path.name)
            if months and months[0]<=a.end[:7].replace('-',''):
                link(path,phase/folder/path.name)
    advice_root=phase/'remittance_advices';advice_root.mkdir(exist_ok=True)
    # Earlier advice can pay during the window: include preceding 31 days.
    from datetime import timedelta
    earliest=(date.fromisoformat(a.start)-timedelta(days=31)).isoformat()
    metadata=[r for r in read(train/'remittance_advices/index.csv')
              if earliest<=r['received_date']<=a.end]
    with (advice_root/'index.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(metadata[0]) if metadata else ['file','entity','payer_name','received_date','format'])
        w.writeheader();w.writerows(metadata)
    for meta in metadata:link(train/'remittance_advices'/meta['file'],advice_root/meta['file'])

    class WindowContext(C.Context):
        def load_items(self):
            for name in ('ar_invoices','ap_invoices','credit_notes'):
                for row in read(train/(name+'.csv.gz')):
                    if row['issue_date']<=a.end:self.add_item(row)
            for iid,row in self.items.items():
                self.by_number[C.norm(row['document_number'])].append(iid)
                self.by_party[row['party_id']].append(iid)
    ctx=WindowContext(input_root,'train')
    print(f'Loaded {len(ctx.items):,} input items; {len(target):,} target transactions. {time.monotonic()-started:.1f}s',flush=True)

    # Reconstruct only strictly pre-window labels. Enforce chronological stream.
    previous=('', '');prior_count=0
    for row in read(train/'golden_allocations.csv.gz'):
        info=txn_meta.get(row['bank_txn_id'])
        if info is None:continue
        day,account=info
        if not day<a.start:raise AssertionError('Prior-label cutoff violated')
        key=(day,row['bank_txn_id'])
        if key<previous:raise ValueError('Prior golden is not chronological; sort before reconstruction')
        previous=key
        if row['item_id'] and row['item_id'] in ctx.items:
            ctx.apply({'booking_date':day,'account_id':account},[row]);prior_count+=1
    del txn_meta
    positive=sum(i['open_amount']>0 for i in ctx.items.values())
    print(f'Reconstructed {prior_count:,} strictly prior allocations; {positive:,} items with positive balance. {time.monotonic()-started:.1f}s',flush=True)
    handlers=[('batches',B.BatchMatcher(ctx).reconcile),('documents',D.DocumentMatcher(ctx).reconcile),
              ('category',lambda txn:M.classify(txn,ctx)),('matching',lambda txn:M.reconcile(txn,ctx))]
    print(f'Indexes ready in {time.monotonic()-started:.1f}s',flush=True)
    counts=Counter();out=dest/'submission.csv.gz';inference_start=time.monotonic()
    with gzip.open(out,'wt',encoding='utf-8',newline='') as f, (dest/'audit.jsonl').open('w') as audit:
        writer=csv.DictWriter(f,fieldnames=V.FIELDS,lineterminator='\n');writer.writeheader()
        for n,txn in enumerate(target,1):
            rows=None;module='unidentified'
            for name,handler in handlers:
                rows=handler(txn)
                if rows:module=name;break
            if not rows:rows=[dict(zip(V.FIELDS,[txn['bank_txn_id'],'','','UNIDENTIFIED','UNIDENTIFIED']))]
            rows=[{k:str(r.get(k,'') or '') for k in V.FIELDS} for r in rows]
            writer.writerows(rows);ctx.apply(txn,rows);counts[module]+=1
            audit.write(json.dumps({'bank_txn_id':txn['bank_txn_id'],'module':module})+'\n')
            if n%5000==0:print(f'{n}/{len(target)} elapsed={time.monotonic()-started:.1f}s modules={dict(counts)}',flush=True)
    inference_seconds=time.monotonic()-inference_start
    validation=V.validate(out,ctx)
    # Only after predictions are closed, materialize target golden for scoring.
    target_ids={txn['bank_txn_id'] for txn in target}
    label_file=dest/'target_golden.csv.gz'
    with gzip.open(label_file,'wt',encoding='utf-8',newline='') as f:
        writer=None
        for row in read(train/'golden_allocations.csv.gz'):
            if row['bank_txn_id'] not in target_ids:continue
            if writer is None:
                writer=csv.DictWriter(f,fieldnames=list(row),lineterminator='\n');writer.writeheader()
            writer.writerow(row)
    golden=S.load_golden(str(label_file),str(phase/'bank_transactions.csv.gz'),EXCEPTIONS,sorted(ctx.categories))
    score=S.score_submission(golden,out.read_bytes(),out.name)
    report={'start':a.start,'end':a.end,'assumption':'Oracle bank-allocation history strictly before window; not pure end-to-end holdout. Non-bank cancellations not reconstructed.',
            'target_labels_used_only_after_predictions':True,'future_invoice_cutoff':a.end,
            'advice_received_cutoff':a.end,'master_data_caveat':'Organizer masters are static later snapshots, not reconstructed as-of-window masters','input_items':len(ctx.items),'positive_balances_before_target_run':positive,
            'prior_allocations':prior_count,'transactions':len(target),'modules':dict(counts),
            'seconds_total':round(time.monotonic()-started,2),'seconds_inference':round(inference_seconds,2),
            'max_rss_bytes_macos':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'validation':validation,'score':score,'source_hashes':hashes,
            'output_sha256':hashlib.sha256(out.read_bytes()).hexdigest()}
    (dest/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source_hashes','score')},indent=2),flush=True)
    print(json.dumps({k:v for k,v in score.items() if k!='segments'},indent=2),flush=True)
    if not validation['ok'] or not score['ok']:raise SystemExit(1)

if __name__=='__main__':main()
