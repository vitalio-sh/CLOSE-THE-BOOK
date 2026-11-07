import assert from 'node:assert/strict';
import test from 'node:test';
import {createProgressService, cleanRun, storeRun} from '../dashboard/progress.mjs';
import {createWorker} from '../dashboard/worker.mjs';

class Bucket {
  objects = new Map();
  async get(key) { const o=this.objects.get(key); return o ? {json:async()=>JSON.parse(o.body)} : null; }
  async put(key, body, options={}) {
    if(options.onlyIf?.get('If-None-Match')==='*' && this.objects.has(key))return null;
    this.objects.set(key,{body,customMetadata:options.customMetadata}); return {key};
  }
  async list({prefix}) {return {truncated:false,objects:[...this.objects].filter(([k])=>k.startsWith(prefix)).map(([key,v])=>({key,customMetadata:v.customMetadata}))};}
}
function run() {return {schema_version:1,run_id:'r-1',author:'Marina',branch:'solution/marina',commit:null,
  phase:'sample',created_at:'2026-11-07T08:00:00Z',local_score:81.56,benchmark:'SAMPLE',precision:.998,recall:.8,
  validation:{ok:true,official_structure_ok:true},sha256:'a'.repeat(64),transactions:2,rows:3,seconds:12,label:'Netting'};}
function fixture() {
  const bucket=new Bucket(), calls=[]; let fail=false;
  const service=createProgressService(async(url,options)=>{
    calls.push({url,options}); if(fail)throw Error('private-error-secret');
    const d=url.endsWith('/me')?{team_id:'t_527b070f3ee6f60a',team_name:'Team 10',members:['private-name']}:
      url.endsWith('/submissions')?{submissions:[
        {phase:'LIVE',sub_id:'s2',filename:'new.csv.gz',created_at:'2026-11-07T10:00:00Z',score:80,status:'scored',private:'secret'},
        {phase:'LIVE',sub_id:'s1',filename:'best.csv.gz',created_at:'2026-11-07T09:00:00Z',score:81.13,status:'scored'},
        {phase:'FINAL',sub_id:'hidden',score:99,status:'received'}]}:
      url.includes('/submissions/')?{metrics:{allocation_precision:.9874,allocation_recall:.7533}}:
      {rows:[{team_name:'Team 10',rank:2,best_live:81.13},{team_name:'Leader',rank:1,best_live:90.69}],updated_at:'2026-11-07T10:00:00Z'};
    return Response.json(d);
  });
  return {bucket, calls, service, env:{BUCKET:bucket,ORGANIZER_READ_TOKEN:'test-only-secret'}, fail:()=>fail=true};
}
test('official score is independent of latest submission; cache and allowlist protect credentials',async()=>{
  const f=fixture();const a=await f.service.read(f.env);const b=await f.service.read(f.env);
  assert.equal(a.official.best_live,81.13);assert.equal(a.official.rank,2);assert.equal(a.official.gap,9.56);
  assert.equal(a.official.submissions[0].score,80);assert.equal(a.official.submissions.length,2);
  assert.equal(b.official.checked_at,a.official.checked_at);assert.equal(f.calls.length,5);
  assert.ok(f.calls.every(x=>x.url.startsWith('https://hackathon.usetalky.com/api/')&&x.options.redirect==='manual'));
  const body=JSON.stringify(a);for(const bad of ['test-only-secret','private-name','hidden','private-error-secret'])assert.ok(!body.includes(bad));
});
test('organizer outage retains last checked score and does not report fresh checked_at',async()=>{
  const f=fixture();const first=await f.service.read(f.env);const key='dashboard/official-progress.json';
  const old=JSON.parse(f.bucket.objects.get(key).body);old.last_attempt_at='2020-01-01T00:00:00Z';
  await f.bucket.put(key,JSON.stringify(old));f.fail();const next=await f.service.read(f.env);
  assert.equal(next.official.best_live,81.13);assert.equal(next.official.checked_at,first.official.checked_at);
  assert.ok(next.official.error);assert.ok(!JSON.stringify(next).includes('private-error-secret'));
});
test('unconfigured official access is explicit and produces no upstream calls',async()=>{
  const f=fixture();delete f.env.ORGANIZER_READ_TOKEN;const a=await f.service.read(f.env);
  assert.equal(a.official.best_live,null);assert.ok(a.official.error);assert.equal(f.calls.length,0);
});
test('run identity immutable, duplicate requests idempotent and metadata allowlisted',async()=>{
  const f=fixture(),r=run();r.token='must-not-persist';r.local_path='/Users/secret';
  assert.equal((await storeRun(f.bucket,r)).status,201);assert.equal((await storeRun(f.bucket,r)).status,200);
  assert.equal((await storeRun(f.bucket,{...r,local_score:99})).status,409);
  const progress=await f.service.read(f.env);assert.equal(progress.runs.length,1);assert.equal(progress.runs[0].local_score,81.56);
  assert.ok(!JSON.stringify(progress).includes('must-not-persist'));assert.ok(!JSON.stringify(progress).includes('/Users/'));
});
test('invalid metrics and misleading LIVE local scores are rejected',()=>{
  for(const change of [{local_score:NaN},{precision:90},{phase:'live'},{benchmark:null},{validation:{ok:false,official_structure_ok:true}},
    {created_at:'2050-01-01T00:00:00Z'},{run_id:'../private'},{rows:0},{sha256:'bad'}])assert.equal(cleanRun({...run(),...change}),null);
  assert.ok(cleanRun({...run(),phase:'train',validation:{ok:true,official_structure_ok:false}}));
});

test('team publisher credential can add a run but cannot replace the main snapshot',async()=>{
  const worker=createWorker({},{}), env={BUCKET:new Bucket(),DASHBOARD_RUNS_TOKEN:'runs-only',DASHBOARD_PUBLISH_TOKEN:'snapshot-only'};
  const req=(path,token)=>worker.fetch(new Request('https://dashboard.test'+path,{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token},body:JSON.stringify(run())}),env);
  assert.equal((await req('/api/runs','wrong')).status,401);
  assert.equal((await req('/api/runs','runs-only')).status,201);
  assert.equal((await req('/api/runs','runs-only')).status,200);
  assert.equal((await req('/api/snapshot','runs-only')).status,401);
});
