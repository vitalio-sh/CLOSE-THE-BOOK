import argparse,csv,gzip,json,time,hashlib
from collections import Counter
from pathlib import Path
from .context import Context,read_csv
from .validate import FIELDS,validate,official_structure

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data',default='data/workspace')
    p.add_argument('--phase',choices=['sample','train','live','final'],default='sample')
    p.add_argument('--out',required=True)
    p.add_argument('--modules',default='batches,documents,matching')
    p.add_argument('--pdf-facts',action='store_true',help='Enable budgeted optional invoice-PDF facts')
    p.add_argument('--pdf-max-documents',type=int,default=100)
    p.add_argument('--pdf-budget-seconds',type=float,default=30)
    a=p.parse_args();start=time.monotonic()
    source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))}
    ctx=Context(a.data,a.phase)
    print(f'Loaded {len(ctx.items)} items in {time.monotonic()-start:.1f}s',flush=True)
    txns=list(read_csv(ctx.phase_dir/'bank_transactions.csv.gz'))
    txns.sort(key=lambda t:(t['booking_date'],t['bank_txn_id']))
    from .history import enrich_referenced_closed
    history_report=enrich_referenced_closed(ctx,txns)
    print('Historical references: '+json.dumps(history_report),flush=True)
    if a.pdf_facts:
        from .pdf_facts import PdfFacts
        ctx.pdf_facts=PdfFacts(ctx,max_documents=a.pdf_max_documents,max_seconds=a.pdf_budget_seconds)
    enabled=a.modules.split(','); handlers=[]; document_stats=None
    if 'batches' in enabled:
        from .batches import BatchMatcher
        handlers.append(('batches',BatchMatcher(ctx).reconcile))
    if 'documents' in enabled:
        from .documents import DocumentMatcher
        document_matcher=DocumentMatcher(ctx)
        document_stats=document_matcher.stats
        print('Document parsing: '+json.dumps(document_stats),flush=True)
        handlers.append(('documents',document_matcher.reconcile))
    if 'matching' in enabled:
        from .matching import reconcile,classify
        handlers.append(('category',lambda t:classify(t,ctx)))
        handlers.append(('matching',lambda t:reconcile(t,ctx)))
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    counts=Counter(); rejected=Counter();opener=gzip.open if out.suffix=='.gz' else open
    with opener(out,'wt',encoding='utf-8',newline='') as f, open(str(out)+'.audit.jsonl','w') as audit:
        w=csv.DictWriter(f,fieldnames=FIELDS,lineterminator='\n');w.writeheader()
        for i,txn in enumerate(txns):
            rows=None;source='unidentified'
            for name,handler in handlers:
                rows=handler(txn)
                if rows and not ctx.proposal_valid(txn,rows):
                    rejected[name]+=1;rows=None
                if rows:source=name;break
            if not rows:
                rows=[dict(zip(FIELDS,[txn['bank_txn_id'],'','','UNIDENTIFIED','UNIDENTIFIED']))]
            rows=[{k:str(r.get(k,'') or '') for k in FIELDS} for r in rows]
            w.writerows(rows);ctx.apply(txn,rows);counts[source]+=1
            audit.write(json.dumps({'bank_txn_id':txn['bank_txn_id'],'module':source,'items':[r['item_id'] for r in rows]})+'\n')
            if (i+1)%1000==0:print(f'{i+1}/{len(txns)} {dict(counts)} elapsed={time.monotonic()-start:.1f}s',flush=True)
    report={'phase':a.phase,'document_parsing':document_stats,'historical_references':history_report,'pdf_facts':getattr(getattr(ctx,'pdf_facts',None),'stats',None),'modules':dict(counts),'rejected_proposals':dict(rejected),'source_hashes':source_hashes,'source_changed_during_run':source_hashes!={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))},'seconds':round(time.monotonic()-start,2),'validation':validate(out,ctx),'official_structure':official_structure(out,ctx),'sha256':hashlib.sha256(out.read_bytes()).hexdigest()}
    Path(str(out)+'.report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    if not report['validation']['ok'] or not report['official_structure']['ok'] or report['source_changed_during_run']:raise SystemExit(1)
if __name__=='__main__':main()
