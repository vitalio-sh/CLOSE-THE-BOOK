"""Independent R1 complete-identity probes against frozen candidate source."""
from pathlib import Path
import argparse,json,sys,tempfile,hashlib
p=argparse.ArgumentParser();p.add_argument('--candidate',required=True);a=p.parse_args()
root=Path(a.candidate).resolve();sys.path[:0]=[str(root),str(root/'tests')]
from test_mixed_payment_runs import MixedRunTests
from recon.batches import BatchMatcher
from recon.batch_fx import run_reference_ids,mixed_run_proposal_valid
checks=[]
with tempfile.TemporaryDirectory() as d:
 ctx,txn,_=MixedRunTests().fixtures(Path(d));matcher=BatchMatcher(ctx);known='PR010025010401'
 for suffix in ['9','A','_9','.9',':9','-9','/9','|9','_X','.X',':X']:
  text='BULK PAYMENT '+known+suffix+' 2 TRANSFERS';t={**txn,'text':text};rows=matcher.reconcile(t)
  checks.append(dict(text=text,expected='abstain',actual=rows,guard_accepts=bool(rows and mixed_run_proposal_valid(ctx,t,rows)),passed=rows is None,reference_ids=sorted(run_reference_ids(t))))
 for text in ['BULK PAYMENT '+known+' 2 TRANSFERS','TRANSF. EMITIDAS FICHERO C34 PR0100250 | 10401 | (2 ORD.)']:
  t={**txn,'text':text};rows=matcher.reconcile(t)
  checks.append(dict(text=text,expected='allocate',passed=bool(rows),actual=rows,reference_ids=sorted(run_reference_ids(t))))
print(json.dumps(dict(candidate=str(root),batch_fx_sha256=hashlib.sha256((root/'recon/batch_fx.py').read_bytes()).hexdigest(),failures=sum(not x['passed'] for x in checks),checks=checks),indent=2))
