#!/usr/bin/env python3
"""Compare completed SAMPLE/October/January task runs against exact predecessor."""
from pathlib import Path
import argparse,json,subprocess,sys,hashlib
p=argparse.ArgumentParser();p.add_argument('--before',required=True);p.add_argument('--official-live-record',required=True,help='Verified official LIVE response JSON; separate from local score');p.add_argument('--sample-manifest-bridge-before');p.add_argument('--sample-manifest-bridge-after');p.add_argument('--golden-side',choices=['before','after'],default='before');p.add_argument('--before-sample',help='Explicit revalidated SAMPLE baseline folder');p.add_argument('--after',required=True);p.add_argument('--task',required=True);p.add_argument('--before-tag',required=True);p.add_argument('--after-tag',required=True);a=p.parse_args()
lane=Path(__file__).resolve().parent;root=lane.parents[2]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def gitsha(tag):return subprocess.check_output(['git','rev-parse',tag+'^{commit}'],cwd=root,text=True).strip()
report={'harness_deviation':'compare_three_superset.py: identical-PDF assertion replaced by a strict-superset check (all baseline PDF inputs/facts present and byte-identical); every other check unchanged','task':a.task,'before_tag':a.before_tag,'before_sha':gitsha(a.before_tag),'candidate_tag':a.after_tag,'candidate_sha':gitsha(a.after_tag),'official_live_score_of_record':None,'evaluation_harness_sha256':{name:sha(lane/name) for name in ['compare_three.py','compare_three_superset.py','compare_task.py','audit_pair_changes.py']},'windows':{},'limitations':['SAMPLE has no opening snapshot.','Temporal windows use oracle prior opening balances and later static masters, so are development windows, not pure holdouts.','No LIVE/FINAL submission.']}
if a.official_live_record:
 official=Path(a.official_live_record).resolve();record=json.loads(official.read_text())
 assert record['phase']=='LIVE' and record['response']['status']=='scored'
 report['official_live_score_of_record']=record['response']['score']
 report['official_live_record']={'path':str(official),'sha256':sha(official),'candidate':record.get('candidate'),'candidate_commit':record.get('candidate_commit'),'sub_id':record['response']['sub_id']}
for window in ['sample','october','january']:
 before=(Path(a.before_sample).resolve() if window=='sample' and a.before_sample else lane/(a.before+'_'+window));after=lane/(a.after+'_'+window)
 if window=='sample':
  # A verified complete artifact bundle is required before looking at labels.
  left=json.loads((before/'experiment.json').read_text());right=json.loads((after/'experiment.json').read_text())
  assert left['status']==right['status']=='verified'
  if left['input_manifest']!=right['input_manifest']:
   assert a.sample_manifest_bridge_before and a.sample_manifest_bridge_after,'Input manifests differ without verified bridge'
   old_bridge=Path(a.sample_manifest_bridge_before).resolve();new_bridge=Path(a.sample_manifest_bridge_after).resolve()
   bo=json.loads((old_bridge/'experiment.json').read_text());bn=json.loads((new_bridge/'experiment.json').read_text())
   assert bo['status']==bn['status']=='verified'
   assert bo['source_hashes']==bn['source_hashes']
   assert bo['input_manifest']==left['input_manifest'] and bn['input_manifest']==right['input_manifest']
   assert sha(old_bridge/'submission.csv.gz')==sha(new_bridge/'submission.csv.gz')
   report['sample_manifest_bridge']={'before':str(old_bridge),'after':str(new_bridge),'same_source_and_prediction':True,'prediction_sha256':sha(old_bridge/'submission.csv.gz'),'reason':'Historical-advice hashing expansion; exact bridge source rerun confirms unchanged inference on expanded manifest.'}
  golden=root/'data/workspace/sample/golden_allocations.csv.gz';bank=root/'data/workspace/sample/bank_transactions.csv.gz'
 else:
  left=json.loads((before/'report.json').read_text());right=json.loads((after/'report.json').read_text())
  assert left['validation']['ok'] and right['validation']['ok'];assert left['input_manifest_sha256']==right['input_manifest_sha256'];assert left['target_golden_content_sha256']==right['target_golden_content_sha256']
  golden=(before if a.golden_side=='before' else after)/'target_golden.csv.gz';bank=Path(left['input_root'])/'live/bank_transactions.csv.gz'
 dest=lane/(a.task+'_'+window+'_comparison.json')
 subprocess.run([sys.executable,str(lane/'compare_task.py'),'--before',str(before/'submission.csv.gz'),'--after',str(after/'submission.csv.gz'),'--golden',str(golden),'--bank',str(bank),'--categories',str(lane/'categories.json'),'--exceptions',str(lane/'exceptions.json'),'--task',a.task,'--before-tag',a.before_tag,'--after-tag',a.after_tag,'--out',str(dest)],check=True)
 item=json.loads(dest.read_text());item['scorer_sha256']=sha(lane/'t0_source/vendor/starter/bankrec_score.py');report['windows'][window]=item
 pair_path=lane/(a.task+'_'+window+'_pair_audit.json')
 subprocess.run([sys.executable,str(lane/'audit_pair_changes.py'),'--before',str(before/'submission.csv.gz'),'--after',str(after/'submission.csv.gz'),'--golden',str(golden),'--out',str(pair_path)],check=True)
 pair=json.loads(pair_path.read_text())
 item['allocation_pair_audit']={'path':str(pair_path),'sha256':sha(pair_path),'definition':pair['definition'],'before_counts':pair['before_counts'],'after_counts':pair['after_counts'],'new_false_pairs':pair['new_false_pairs'],'removed_false_pairs':pair['removed_false_pairs'],'changed_existing_false_pairs':pair['changed_existing_false_pairs'],'segments':pair['segments']}
 item['run_provenance']={}
 for role,folder,manifest in [('before',before,left),('after',after,right)]:
  if window=='sample':
   release=json.loads((folder/'submission.csv.gz.report.json').read_text())
   identity={'manifest_sha256':sha(folder/'experiment.json'),'command':manifest['command'],'pdf_readiness':release.get('pdf_readiness')}
  else:
   identity={'manifest_sha256':sha(folder/'report.json'),'input_manifest_sha256':manifest['input_manifest_sha256'],'target_golden_raw_sha256':manifest['target_golden_sha256'],'target_golden_content_sha256':manifest['target_golden_content_sha256'],'pdf_readiness':manifest.get('pdf_readiness')}
  readiness=identity.get('pdf_readiness')
  if readiness and readiness.get('config',{}).get('strict'):
   assert readiness['complete'] and not readiness['reasons'], (role,window,'incomplete PDF evidence')
   evidence=Path(readiness['artifact_dir'])
   for filename,key,digest_key in [('pdf_inputs.json','files','inputs_sha256'),('pdf_facts_snapshot.json','facts','facts_sha256')]:
    payload=json.loads((evidence/filename).read_text())
    bound=hashlib.sha256(json.dumps(payload[key],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert bound==payload['sha256']==readiness[digest_key],(role,window,filename,'evidence digest mismatch')
   identity['pdf_snapshots_verified']=True
  item['run_provenance'][role]=identity
 pdf_before=item['run_provenance']['before'].get('pdf_readiness')
 pdf_after=item['run_provenance']['after'].get('pdf_readiness')
 if pdf_before and pdf_after and pdf_before.get('config',{}).get('strict') and pdf_after.get('config',{}).get('strict'):
  if pdf_before['inputs_sha256']==pdf_after['inputs_sha256'] and pdf_before['facts_sha256']==pdf_after['facts_sha256']:
   item['identical_pdf_evidence_verified']=True
  else:
   # DEVIATION (claude_competitor_lane): the candidate restores batch-referenced closed invoices,
   # which adds required PDFs. Accept only a strict superset: every baseline input and fact must
   # be present and byte-identical in the candidate run.
   sup={}
   for filename,key in [('pdf_inputs.json','files'),('pdf_facts_snapshot.json','facts')]:
    old_payload=json.loads((Path(pdf_before['artifact_dir'])/filename).read_text())[key]
    new_payload=json.loads((Path(pdf_after['artifact_dir'])/filename).read_text())[key]
    missing=[k for k in old_payload if k not in new_payload]
    changed=[k for k in old_payload if k in new_payload and old_payload[k]!=new_payload[k]]
    assert not missing and not changed,(window,filename,'PDF evidence is not a strict superset',len(missing),len(changed))
    sup[filename]={'baseline':len(old_payload),'candidate':len(new_payload),'missing':0,'changed':0}
   item['identical_pdf_evidence_verified']=False
   item['pdf_evidence_superset_verified']=sup
 for name,tag,run in [('before',a.before_tag,before),('after',a.after_tag,after)]:
  actual=(left if name=='before' else right)['source_hashes']
  actual={k:v for k,v in actual.items() if k.startswith('recon/')}
  for file,expected in actual.items():
   content=subprocess.check_output(['git','show',tag+':'+file],cwd=root)
   assert hashlib.sha256(content).hexdigest()==expected,(window,name,file,'tag/source mismatch')
 report['windows'][window]['tag_source_hashes_verified']=True
out=lane/(a.task+'_report.json');out.write_text(json.dumps(report,indent=2)+'\n');print(out)
