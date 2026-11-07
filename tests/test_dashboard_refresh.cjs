const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
const document={visibilityState:'visible',getElementById(id){if(!elements.has(id))elements.set(id,{dataset:{},textContent:''});return elements.get(id)}};
const sandbox={document,console,Intl,URL,Blob,AbortController,setTimeout:()=>1,clearTimeout:()=>{},fetch(){throw Error('unexpected network')}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('dashboard/app.js','utf8').replace(/start\(\);\s*$/,''),sandbox);
const run=source=>vm.runInContext(source,sandbox);
function reset(){run(`data={pipeline:{run_id:'old',sha256:'hash-old'},hosting:{auto_refresh:true}};publishedVersion=snapshotVersion(data);refreshBusy=false;refreshFailed=false;renderCount=0;fetchCount=0;document.visibilityState='visible';renderSnapshot=next=>{data=next;renderCount++};`)}
(async()=>{
  run(`testRows=Array.from({length:19},(_,i)=>({id:'T'+i,party:'Customer',entity:'ES01',description:'Invoice '+i,refs:{},allocations:[],exception_codes:[],outcome:'matched'}));testSnapshot={transactions:testRows};`);
  assert.equal(run(`queueState(testSnapshot,'','all','T16',2,true).page`),2);
  assert.equal(run(`queueState(testSnapshot,'','all','T16',2,true).selected`),'T16');
  assert.equal(run(`queueState({transactions:testRows.slice(0,10)},'','all','T16',2,true).page`),1);
  assert.equal(run(`queueState({transactions:testRows.slice(0,10)},'','all','T16',2,true).selected`),'T8');
  assert.equal(run(`queueState(testSnapshot,'not found','all','T16',2,true).rows.length`),0);
  assert.equal(run(`queueState(testSnapshot,'','unidentified','T16',2,true).selected`),undefined);
  reset();run(`data.hosting.auto_refresh=false;fetchJSON=async()=>{fetchCount++;};`);await run(`checkForUpdate()`);assert.equal(run('fetchCount'),0);
  reset();run(`document.visibilityState='hidden';fetchJSON=async()=>{fetchCount++;};`);await run(`checkForUpdate()`);assert.equal(run('fetchCount'),0);
  reset();run(`fetchJSON=async()=>{fetchCount++;return {run_id:'old',hash:'hash-old'}};`);await run(`checkForUpdate()`);assert.equal(run('fetchCount'),1);assert.equal(run('renderCount'),0);assert.equal(elements.get('refresh-status').dataset.state,'current');
  reset();run(`fetchJSON=async url=>{fetchCount++;return url==='/api/version'?{run_id:'new',hash:'hash-new'}:{schema_version:1,pipeline:{run_id:'new',sha256:'hash-new'},transactions:[],hosting:{auto_refresh:true}}};`);await run(`checkForUpdate()`);assert.equal(run('fetchCount'),2);assert.equal(run('renderCount'),1);assert.equal(run('publishedVersion.run_id'),'new');
  reset();run(`fetchJSON=async url=>url==='/api/version'?{run_id:'new',hash:'hash-new'}:{schema_version:1,pipeline:{run_id:'other',sha256:'other-hash'},transactions:[]};`);await run(`checkForUpdate()`);assert.equal(run('renderCount'),0);assert.equal(run('publishedVersion.run_id'),'old');assert.equal(elements.get('refresh-status').dataset.state,'stale');
  reset();run(`fetchJSON=async()=>{throw Error('offline')}`);await run(`checkForUpdate()`);assert.equal(run('data.pipeline.run_id'),'old');assert.equal(run('publishedVersion.run_id'),'old');assert.equal(elements.get('refresh-status').dataset.state,'stale');
  reset();run(`fetchJSON=async url=>url==='/api/version'?{run_id:'new',hash:'hash-new'}:{schema_version:1,pipeline:{run_id:'new',sha256:'hash-new'},transactions:[]};renderSnapshot=()=>{throw Error('render failed')};`);await run(`checkForUpdate()`);assert.equal(run('publishedVersion.run_id'),'old');assert.equal(run('data.pipeline.run_id'),'old');assert.equal(elements.get('refresh-status').dataset.state,'stale');
  reset();run(`fetchJSON=()=>{fetchCount++;return new Promise(resolve=>releaseFetch=resolve)};`);const first=run(`checkForUpdate()`);await run(`checkForUpdate()`);assert.equal(run('fetchCount'),1);run(`releaseFetch({run_id:'old',hash:'hash-old'})`);await first;assert.equal(run('refreshBusy'),false);
  console.log('Passed: filter/selection/page preservation, local and hidden gating, same-version skip, update commit, publication race, network failure, render failure, overlap suppression.');
})().catch(error=>{console.error(error);process.exitCode=1});
