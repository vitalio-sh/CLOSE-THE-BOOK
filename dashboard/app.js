'use strict';
let renderRoot = null;
const $ = id => renderRoot ? renderRoot.querySelector('#'+id) : document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => new Intl.NumberFormat('en-GB').format(Number(value || 0));
const money = value => new Intl.NumberFormat('en-GB', {minimumFractionDigits:2, maximumFractionDigits:2}).format(Number(value || 0));
const date = value => value ? new Date(value+'T12:00:00').toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'}) : 'Not supplied';
const nice = value => String(value || '').toLowerCase().replaceAll('_',' ').replace(/^./, x=>x.toUpperCase());
const moduleNames = {batches:'Payment batches',documents:'Remittance advice',matching:'Invoice matching',category:'Bank categories',unidentified:'Unidentified'};
const exceptionText = {PARTIAL:'Part of the invoice is allocated. An unpaid balance may remain.',OVERPAYMENT:'The payment exceeds the allocated invoice balance.',SHORT_DISCOUNT:'The proposal treats the shortfall as an allowed payment discount.',SHORT_BANK_CHARGES:'The proposal treats the shortfall as bank charges under the policy limits.',SHORT_WITHHOLDING:'The proposal treats the shortfall as withholding.',SHORT_DISPUTE:'The proposal records a payment dispute.',DUPLICATE_PAYMENT:'The proposal marks a duplicate payment; it does not reduce the invoice balance again.',RETURNED:'The proposal reverses a previous payment and reopens the allocated amount.',FX_DIFFERENCE:'The bank and invoice amounts are handled under a foreign-exchange exception.',UNIDENTIFIED:'The engine could not establish a valid invoice allocation or accounting category.'};
let data, selected, filtered=[], page=0, guideIndex=0;
const pageSize=8;
function kind(t){return t.outcome==='unidentified'?'unidentified':t.outcome==='categorized'?'category':t.exception_codes.length?'exception':'matched'}
function status(t){return ({matched:'Invoice linked',exception:'Exception',category:'Category only',unidentified:'Unidentified'})[kind(t)]}
function party(t){return t.party || t.allocations.find(a=>a.party)?.party || (t.category ? nice(t.category) : 'Counterparty not supplied')}
function badge(t){return `<span class="status ${kind(t)}">${esc(status(t))}</span>`}
function summary(){const s=data.summary, p=data.pipeline; $('summary').innerHTML=[['Bank transactions',s.transactions,`${date(s.date_from)} – ${date(s.date_to)}`,''],['Invoice linked',s.outcomes.matched,'Includes proposals with exceptions','green'],['Category only',s.outcomes.categorized,'No invoice allocation',''],['Unidentified',s.outcomes.unidentified,'Requires further investigation','amber']].map(([label,value,detail,cls])=>`<div class="metric"><div class="metric-label">${label}</div><strong class="${cls}">${num(value)}</strong><small>${detail}</small></div>`).join(''); $('phase-name').textContent=p.phase.toUpperCase()+' dataset';$('snapshot-label').textContent=data.hosting?.public_snapshot?'Published run snapshot':'Local run snapshot';$('run-time').textContent=`Run ${Math.round(p.seconds)}s · ${new Date(data.sources.submission.modified_at).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'})}`;$('sample-note').textContent=`${num(data.sample.size)} representative examples from ${num(s.transactions)} operations. Search covers these examples.`}
function queueState(snapshot, query, outcome, currentSelection, currentPage, preservePage=false){
  const q=query.trim().toLowerCase();
  const rows=snapshot.transactions.filter(t=>{
    const match=outcome==='all'||(outcome==='matched'?t.outcome==='matched':kind(t)===outcome);
    return match&&(!q||[t.id,party(t),t.entity,t.entity_name,t.description,JSON.stringify(t.refs),...t.allocations.map(a=>a.invoice_number+' '+a.item_id)].join(' ').toLowerCase().includes(q));
  });
  const nextPage=preservePage?Math.max(0,Math.min(currentPage,Math.max(0,Math.ceil(rows.length/pageSize)-1))):0;
  const nextSelection=rows.some(t=>t.id===currentSelection)?currentSelection:rows[nextPage*pageSize]?.id;
  return {rows,page:nextPage,selected:nextSelection,hasFilters:!!q||outcome!=='all'};
}
function filter(options={}){
  const state=queueState(data,$('search').value,$('outcome').value,selected,page,options.preservePage);
  filtered=state.rows;page=state.page;selected=state.selected;
  renderQueue();renderDetail();$('clear-filters').hidden=!state.hasFilters;
}
function renderQueue(){const pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,pages-1);$('results-count').textContent=`${num(filtered.length)} of ${data.sample.size} examples`;$('transactions').innerHTML=filtered.slice(page*pageSize,(page+1)*pageSize).map(t=>`<tr class="${t.id===selected?'selected':''}"><td><button class="txn-select" data-id="${esc(t.id)}" aria-pressed="${t.id===selected}" aria-controls="detail"><strong>${esc(party(t))}</strong><small>${date(t.date)} <span aria-hidden="true">/</span> ${esc(t.entity)}</small><span class="id">${esc(t.id)}</span></button></td><td class="amount">${money(t.bank_amount)}<small>${esc(t.currency)}</small></td><td>${badge(t)}</td></tr>`).join('');$('empty').hidden=filtered.length>0;$('page-indicator').textContent=`Page ${page+1} of ${pages}`;$('previous').disabled=page===0;$('next').disabled=page>=pages-1;$('transactions').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.id;renderQueue();renderDetail()}))}
function allocation(a){return `<div class="allocation"><div><strong>${esc(a.invoice_number||a.item_id)}</strong><small>${esc(a.item_id)}</small><small>Invoice date ${date(a.issue_date)} · Due ${date(a.due_date)}</small></div><div class="amount"><strong>${money(a.amount_applied)} ${esc(a.item_currency)}</strong><small>Applied · Total ${money(a.total_amount)}</small>${a.exception?`<span class="status exception">${esc(nice(a.exception))}</span>`:''}</div></div>`}
function renderDetail(){const t=data.transactions.find(t=>t.id===selected);if(!t){$('detail').innerHTML='<div class="empty"><h3>Select an example</h3><p>Clear the filters to explore a transaction and its evidence.</p></div>';return}const refs=Object.entries(t.refs).filter(([,v])=>v);const exceptions=t.exception_codes.filter(c=>c!=='UNIDENTIFIED');const amounts=new Map();for(const a of t.allocations){const curr=a.item_currency||'Unknown currency';amounts.set(curr,(amounts.get(curr)||0)+Number(a.amount_applied))}const totals=[...amounts].map(([c,v])=>money(v)+' '+c).join(' + ');$('detail').innerHTML=`<div class="detail-kicker"><span>Selected transaction</span>${badge(t)}</div><h2>${esc(party(t))}</h2><div class="detail-amount">${money(t.bank_amount)} <small>${esc(t.currency)}</small></div><p class="detail-meta">${date(t.date)} · ${Number(t.bank_amount)<0?'Money out':'Money in'} · ${esc(t.entity)}</p><div class="explanation"><strong>${esc(moduleNames[t.module]||nice(t.module))}</strong><p>${esc(data.definitions.module_descriptions[t.module]||'No method recorded.')}</p>${exceptions.map(code=>`<p><strong>${esc(nice(code))}.</strong> ${esc(exceptionText[code]||'Exception recorded in the submission.')}</p>`).join('')}</div><section class="detail-section"><h3>Bank evidence</h3><dl class="evidence"><dt>Transaction ID</dt><dd>${esc(t.id)}</dd><dt>Legal entity</dt><dd>${esc(t.entity_name||t.entity)}</dd>${refs.map(([k,v])=>`<dt>${esc(nice(k))}</dt><dd>${esc(v)}</dd>`).join('')}${t.original_currency?`<dt>Original amount</dt><dd>${money(t.original_amount)} ${esc(t.original_currency)}</dd>`:''}</dl><div class="bank-text">${esc(t.description||'No bank description supplied.')}</div></section><section class="detail-section"><h3>${t.allocations.length?`Proposed allocations (${num(t.allocations.length)})`:'Proposed accounting treatment'}</h3>${t.allocations.slice(0,5).map(allocation).join('')}${t.allocations.length>5?`<details><summary class="detail-note">Show ${t.allocations.length-5} more allocations</summary>${t.allocations.slice(5).map(allocation).join('')}</details>`:''}${!t.allocations.length?`<div class="allocation"><div><strong>${esc(nice(t.category||'Unidentified'))}</strong><small>${t.outcome==='unidentified'?'Further evidence is needed to identify this payment.':'An accounting category was proposed without an invoice link.'}</small></div></div>`:`<p class="detail-note">Applied by invoice currency: ${esc(totals)}. These amounts are accounting allocations, not a converted bank total.</p>`}</section><details class="detail-section"><summary>Inspect source records and output</summary><dl class="evidence"><dt>Bank source</dt><dd>${esc(t.bank_source)}</dd><dt>Audit method</dt><dd>${esc(t.module)}</dd>${[...new Set(t.allocations.map(a=>a.source).filter(Boolean))].map(source=>`<dt>Invoice source</dt><dd>${esc(source)}</dd>`).join('')}</dl><div class="csv-preview"><pre>${esc('bank_txn_id,item_id,amount,category,exception\n'+csvRows(t).join('\n'))}</pre></div></details><p class="detail-note">Observed output from this run. The audit records the selected method; it does not provide a per-rule decision trace or a confidence score.</p>`}
function csvRows(t){return t.allocations.length?t.allocations.map(a=>[t.id,a.item_id,a.amount_applied,'',a.exception].join(',')):[[t.id,'','',t.category,t.exception_codes.join('|')].join(',')]}
function renderProcess(){const steps=[['Read the source records','Bank movements, open balances, invoice registers, payment batches and remittance advice are loaded. Only relevant historical identities are added.',['Opening balances carry forward what remains unpaid.','A later-dated invoice cannot be used for an earlier payment.']],['Try the matching methods in order','The first valid proposal wins: payment batches, remittance advice, accounting categories, then invoice matching.',['Batch and advice records can explain one payment covering many invoices.','Invoice matching considers references, counterparties, amounts and policy rules.']],['Check the proposal','Before applying a proposal, the engine checks item identity, dates, duplicate rows and the available balance.',['A rejected proposal lets the next method try.','If every method fails, the transaction stays unidentified.']],['Update balances in date order','Allocations reduce open balances. A returned payment reopens the applied amount; a duplicate payment does not reduce it again.',['This state is used when processing the next bank movement.']],['Validate and write the file','One CSV row is written per transaction–item allocation, or a category row when there is no invoice link.',['Independent and organizer format checks run before the file is published.','The file hash identifies the exact output under review.']]];$('process-content').innerHTML=`<div class="process-layout"><div>${steps.map(([title,desc,bullets],i)=>`<section class="flow-step"><div class="step-number">${i+1}</div><div><h3>${title}</h3><p>${desc}</p><ul>${bullets.map(b=>`<li>${b}</li>`).join('')}</ul></div></section>`).join('')}</div><aside class="process-aside"><h3>Which method produced the result?</h3><p>Counts across all ${num(data.summary.transactions)} operations. These are method counts, not correctness measurements.</p>${data.summary.modules.map(m=>`<div class="module-row"><span>${esc(moduleNames[m.module]||m.module)}</span><div class="module-bar"><span style="width:${m.transactions/data.summary.transactions*100}%"></span></div><b>${num(m.transactions)}</b></div>`).join('')}<div class="notice"><strong>Where Claude fits</strong><p>Claude Code reviewed the implementation. This run uses deterministic matching rules; it does not ask an LLM to decide every payment.</p></div><button class="button primary" id="process-example">Inspect a real example</button></aside></div><h2>Cash movements by currency</h2><p class="detail-note">Full dataset totals. Each currency stands alone; inflows and outflows are bank cash amounts.</p><div class="table-scroll"><table><thead><tr><th>Currency</th><th class="amount">Transactions</th><th class="amount">Money in</th><th class="amount">Money out</th><th class="amount">Net movement</th></tr></thead><tbody>${data.summary.currencies.map(c=>`<tr><td>${esc(c.currency)}</td><td class="amount">${num(c.transactions)}</td><td class="amount">${money(c.inflow)}</td><td class="amount">${money(c.outflow)}</td><td class="amount">${money(c.net)}</td></tr>`).join('')}</tbody></table></div>`;$('process-example').onclick=walkthrough}
function renderExport(){const p=data.pipeline,v=p.validation, checks=[['Every transaction represented',`${num(v.covered)} of ${num(v.transactions)} transactions covered.`,v.missing===0],['Independent format validator',`${num(v.rows)} rows checked for identifiers, amounts and required columns.`,v.ok],['Organizer format validator','The official parser accepted the file structure.',p.official_structure.ok],['File identity verified','The exported data matches the SHA-256 recorded for this file.',p.hash_verified],['Code stable during the run','No reconciliation source changed while the output was produced.',p.source_changed_during_run===false]];$('export-content').innerHTML=`<div class="export-grid"><div><div class="checks">${checks.map(([title,desc,ok])=>`<div class="check-row"><div><h3>${title}</h3><p>${desc}</p></div><span class="status ${ok?'matched':'unidentified'}">${ok?'Passed':'Needs review'}</span></div>`).join('')}</div><div class="notice"><strong>Local checks do not measure hidden ${esc(p.phase.toUpperCase())} accuracy</strong><p>Coverage and valid formatting are not a competition score. See Progress &amp; team runs for official submissions; match the file identity before associating a score with this candidate.</p></div></div><aside class="file-panel"><h3>Candidate submission</h3><code>${esc(data.sources.submission.path)}</code><dl class="evidence"><dt>Dataset</dt><dd>${esc(p.phase.toUpperCase())}</dd><dt>Rows</dt><dd>${num(v.rows)}</dd><dt>Size</dt><dd>${(v.uncompressed_bytes/1024/1024).toFixed(2)} MiB unpacked / 60 MiB limit</dd><dt>Runtime</dt><dd>${num(p.seconds)} seconds</dd><dt>Created</dt><dd>${esc(new Date(data.sources.submission.modified_at).toLocaleString('en-GB'))}</dd></dl><p class="detail-note">SHA-256</p><code>${esc(p.sha256)}</code>${data.hosting?.public_snapshot?'<p class="detail-note">The full candidate file is available in the team’s local workspace. This shared view contains summary data and representative examples.</p>':'<a class="button primary" href="/submission" download style="display:inline-block;text-decoration:none">Download candidate CSV.gz</a>'}<button class="button secondary" id="download-report">Download run report</button><p class="detail-note">Downloading does not submit a result. The team token and the competition platform are required for submission.</p></aside></div><section class="detail-section"><h3>Document parsing</h3><p class="detail-note">${num(p.document_parsing?.parsed)} of ${num(p.document_parsing?.files_total)} advice files parsed. ${num(p.document_parsing?.parse_errors)} parse errors; ${num(p.document_parsing?.unsupported_or_empty)} unsupported or empty files. Invoice-PDF enrichment: ${p.pdf_facts?'enabled for this run':'not enabled for this run'}.</p></section>`;$('download-report').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({pipeline:p,sources:data.sources,export_checks:data.export_checks},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='valtermia-run-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}}
function view(name){$('summary').hidden=name==='progress';document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!==`view-${name}`);document.querySelectorAll('.nav-item').forEach(b=>{b.classList.toggle('active',b.dataset.view===name);if(b.dataset.view===name)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});const titles={queue:['Reconciliation desk','Follow a bank movement through to its proposed accounting treatment.'],process:['How reconciliation works','Understand the checks, their order and the evidence behind the output.'],export:['Submission checks','Inspect the exact candidate file before uploading it to the competition.'],progress:['Team progress','Official competition results and the experiments behind the next improvement.']};$('page-title').textContent=titles[name][0];$('page-subtitle').textContent=titles[name][1];history.replaceState(null,'',`#${name}`)}
function walkthrough(){view('queue');$('search').value='';$('outcome').value='all';const examples=[data.transactions.find(t=>t.module==='documents'&&!t.exception_codes.length&&t.allocations.length<=5),data.transactions.find(t=>t.exception_codes.includes('PARTIAL')),data.transactions.find(t=>t.exception_codes.includes('RETURNED')),data.transactions.find(t=>t.outcome==='unidentified')].filter(Boolean);const t=examples[guideIndex++%examples.length];if(!t)return;selected=t.id;filter();page=Math.floor(filtered.findIndex(x=>x.id===selected)/pageSize);renderQueue();renderDetail();$('guided').textContent='Show the next case';$('detail').scrollIntoView({behavior:'auto',block:'nearest'})}
const refreshEvery=15000;
let refreshTimer=null, refreshBusy=false, refreshFailed=false, publishedVersion=null;
function snapshotVersion(snapshot){return {run_id:snapshot.pipeline.run_id??null,hash:snapshot.pipeline.sha256};}
function sameVersion(a,b){return !!a&&!!b&&a.run_id===b.run_id&&a.hash===b.hash;}
function automaticUpdates(){return data?.hosting?.auto_refresh===true;}
function validateSnapshot(snapshot){
  if(snapshot?.schema_version!==1||!Array.isArray(snapshot.transactions)||!snapshot.pipeline?.sha256)
    throw Error('Unsupported dashboard data schema');
}
function refreshStatus(state,text){
  $('refresh-status').dataset.state=state;
  $('refresh-status').textContent=text;
}
function showPublishedTime(){
  const stamp=data?.hosting?.published_at;
  const time=stamp?new Date(stamp):null;
  $('published-time').hidden=!stamp;
  $('published-time').textContent=time&&!Number.isNaN(time.getTime())
    ?'Published '+time.toLocaleString('en-GB',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})
    :'';
}
async function fetchJSON(url){
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),12000);
  try{
    const response=await fetch(url,{cache:'no-store',signal:controller.signal});
    if(!response.ok)throw Error(`Data request returned ${response.status}`);
    return await response.json();
  }finally{clearTimeout(timeout);}
}
function renderSnapshot(next){
  validateSnapshot(next);
  const previous={data,selected,filtered,page};
  const active=document.activeElement;
  const focusedId=active?.id;
  const focusedTxn=active?.classList.contains('txn-select')?active.dataset.id:null;
  // Stage the complete render offscreen. A malformed update cannot partly replace
  // the visible run. Search/select controls and the active view remain in place.
  const staged=document.body.cloneNode(true);
  staged.querySelector('#search').value=$('search').value;
  staged.querySelector('#outcome').value=$('outcome').value;
  try{
    renderRoot=staged;data=next;
    summary();filter({preservePage:true});renderProcess();renderExport();
  }catch(error){
    ({data,selected,filtered,page}=previous);
    throw error;
  }finally{renderRoot=null;}
  const ids=['summary','phase-name','snapshot-label','run-time','sample-note','results-count',
    'page-indicator','transactions','detail','process-content','export-content'];
  // All mutations are synchronous: the browser paints the coherent new run once.
  // Keep equal subtrees intact, including a focused transaction button.
  for(const id of ids){
    const current=$(id), replacement=staged.querySelector('#'+id);
    if(current.innerHTML!==replacement.innerHTML)current.replaceWith(replacement);
  }
  for(const id of ['empty','clear-filters'])$(id).hidden=staged.querySelector('#'+id).hidden;
  for(const id of ['previous','next'])$(id).disabled=staged.querySelector('#'+id).disabled;
  if(active&&!active.isConnected){
    const replacement=focusedTxn
      ?[...$('transactions').querySelectorAll('.txn-select')].find(button=>button.dataset.id===focusedTxn)
      :focusedId?$(focusedId):null;
    if(replacement)replacement.focus({preventScroll:true});
    else if(focusedTxn)$('search').focus({preventScroll:true});
  }
  showPublishedTime();
}
function scheduleRefresh(){
  clearTimeout(refreshTimer);refreshTimer=null;
  if(automaticUpdates()&&document.visibilityState==='visible')
    refreshTimer=setTimeout(checkForUpdate,refreshEvery);
}
async function checkForUpdate(){
  if(refreshBusy||!automaticUpdates()||document.visibilityState!=='visible')return;
  clearTimeout(refreshTimer);refreshTimer=null;refreshBusy=true;
  refreshStatus(refreshFailed?'stale':'checking',refreshFailed?'Connection issue · retrying…':'Checking for a newer run…');
  try{
    const version=await fetchJSON('/api/version');
    if(!version||typeof version.hash!=='string'||!version.hash)throw Error('Missing publication identity');
    if(document.visibilityState!=='visible')return;
    if(!sameVersion(version,publishedVersion)){
      const next=await fetchJSON('data.json');
      validateSnapshot(next);
      if(!sameVersion(snapshotVersion(next),version))throw Error('Publication changed during refresh; retry required');
      renderSnapshot(next);
      // A failed fetch or render never advances the version we have displayed.
      publishedVersion=snapshotVersion(next);
    }
    refreshFailed=false;
    refreshStatus('current',automaticUpdates()?'Up to date · checks every 15s':'Automatic updates disabled');
  }catch(error){
    refreshFailed=true;
    refreshStatus('stale','Update unavailable · showing the previous publication');
  }finally{refreshBusy=false;scheduleRefresh();}
}
function setupRefresh(){
  publishedVersion=snapshotVersion(data);showPublishedTime();
  if(!automaticUpdates()){
    refreshStatus('local','Local snapshot · automatic updates off');
    return;
  }
  document.addEventListener('visibilitychange',()=>{
    clearTimeout(refreshTimer);refreshTimer=null;
    if(document.visibilityState==='visible')checkForUpdate();
    else if(!refreshFailed)refreshStatus('paused','Updates paused while this tab is hidden');
  });
  if(document.visibilityState==='visible')checkForUpdate();
  else refreshStatus('paused','Updates paused while this tab is hidden');
}
// Competition progress has its own data path. Refreshing it never replaces the
// explorer, current page, search controls, or the selected transaction.
let progressData=null, progressBusy=false, progressTimer=null, progressUnavailable=false;
const teamRepo='https://github.com/vitalio-sh/CLOSE-THE-BOOK';
const teamPrompt=`In the Team 10 CLOSE-THE-BOOK repository, publish my checked run to the team dashboard.

Keep my solver work in my own solution/<name> branch. Do not merge into main or submit anything to the hackathon platform. Run the relevant pipeline and its validators; if local labels are available, evaluate the result on a named SAMPLE or TRAIN benchmark and record the actual score. Do not invent a score for an unlabelled LIVE run.

From the repository root, obtain the standalone sharing tool without merging solver code:
git fetch origin dashboard/runs
git show origin/dashboard/runs:scripts/share_dashboard_run.py > /tmp/share_dashboard_run.py

Publish my checked run:
python3 /tmp/share_dashboard_run.py --repo "$PWD" --report PATH_TO_RUN_REPORT --author MY_GITHUB_USERNAME --label "Short description of this experiment" --evaluation PATH_TO_EVALUATION_REPORT

Omit --evaluation if there is no compatible local evaluation report. Check the script's --help for supported report formats. The tool publishes a sanitized summary to dashboard/runs; GitHub Actions updates the shared dashboard. Include the solution branch, commit, benchmark and output hash so the run can be reproduced. Never publish the dataset, transaction-level records, environment files, credentials or tokens. Verify that the publication workflow succeeded and that my run appears in the dashboard.`;
function scoreValue(value){return typeof value==='number'&&Number.isFinite(value)?value.toFixed(2):'—'}
function percentValue(value){return typeof value==='number'&&Number.isFinite(value)?(value*100).toFixed(2)+'%':'—'}
function progressTime(value){
  if(!value)return 'Time unavailable';
  const stamp=new Date(value);
  return Number.isNaN(stamp.getTime())?'Time unavailable':stamp.toLocaleString('en-GB',{timeZone:'Europe/Madrid',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})+' Madrid';
}
function newestFirst(rows){return [...rows].sort((a,b)=>(Date.parse(b.created_at)||0)-(Date.parse(a.created_at)||0));}
function officialRows(snapshot){return newestFirst(snapshot?.official?.submissions||[]);}
function officialSummaryHTML(snapshot){
  const official=snapshot?.official;
  if(!official)return '<div class="official-heading"><strong>Official LIVE result</strong><span>No verified competition result is available yet.</span></div>';
  const latest=officialRows(snapshot).find(row=>row.status==='scored'&&typeof row.score==='number');
  const rank=Number.isInteger(official.rank)&&official.rank>0?'#'+official.rank:'—';
  const stamp=progressTime(official.checked_at);
  return `<div class="official-heading"><strong>Official LIVE · ${esc(official.team_name||'Team 10')}</strong><span>Leaderboard checked ${esc(stamp)}</span></div><div class="official-metrics"><div><span>Best score / 100</span><strong>${scoreValue(official.best_live)}</strong></div><div><span>Leaderboard place</span><strong>${rank}</strong></div><div><span>Leader / gap</span><strong class="small-number">${scoreValue(official.leader_score)} <small>/ ${scoreValue(official.gap)} pts</small></strong></div><div><span>Latest scored submission</span><strong class="small-number">${scoreValue(latest?.score)}</strong><small>${latest?esc(progressTime(latest.created_at)):'No scored submission yet'}</small></div></div>${official.error||progressUnavailable?'<p class="official-warning">Progress update unavailable. Showing the last verified result.</p>':''}`;
}
function officialHistoryHTML(snapshot){
  const rows=officialRows(snapshot);
  if(!rows.length)return '<div class="history-empty">No official submissions are available in this view yet.</div>';
  const scored=rows.filter(row=>row.status==='scored'&&typeof row.score==='number');
  const best=snapshot.official?.best_live;
  return `<div class="table-scroll"><table class="history-table"><caption class="sr-only">Official LIVE submission history</caption><thead><tr><th scope="col">Submitted · Madrid</th><th scope="col">File / submission</th><th scope="col">Status</th><th scope="col" class="amount">LIVE score</th><th scope="col" class="amount">Change from prior</th><th scope="col" class="amount">Precision / recall</th></tr></thead><tbody>${rows.map(row=>{
    const index=scored.findIndex(item=>item.sub_id===row.sub_id),previous=index>=0?scored[index+1]:null;
    const delta=previous?row.score-previous.score:null;
    const change=delta===null?'First scored run':`${delta>0?'+':''}${delta.toFixed(2)} pts`;
    const metric=row.metrics||{};
    return `<tr><td class="history-time">${esc(progressTime(row.created_at))}</td><td><strong>${esc(row.filename||'Submission')}</strong><small>${esc(row.sub_id)}</small></td><td><span class="status ${row.status==='scored'?'matched':row.status==='invalid'||row.status==='error'?'unidentified':'neutral'}">${esc(nice(row.status))}</span></td><td class="amount"><strong>${row.status==='scored'?scoreValue(row.score):'—'}</strong>${row.status==='scored'&&row.score===best?'<small>Current best</small>':''}</td><td class="amount ${delta>0?'positive':delta<0?'negative':''}">${row.status==='scored'?esc(change):'—'}</td><td class="amount">${percentValue(metric.allocation_precision)} / ${percentValue(metric.allocation_recall)}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}
function teamHistoryHTML(snapshot){
  const runs=newestFirst(snapshot?.runs||[]);
  if(!runs.length)return '<div class="history-empty">No team runs have been shared yet. Use the publishing prompt below to add the first one.</div>';
  return `<div class="table-scroll"><table class="history-table team-history"><caption class="sr-only">Shared local run history, separate from official LIVE results</caption><thead><tr><th scope="col">Run / author</th><th scope="col">Branch / commit</th><th scope="col">Local benchmark</th><th scope="col" class="amount">Local score</th><th scope="col" class="amount">Precision / recall</th><th scope="col">Reported checks / output</th></tr></thead><tbody>${runs.map(row=>{
    const check=row.validation?.ok===true&&row.validation?.official_structure_ok===true;
    const branch=String(row.branch||'');
    const commit=String(row.commit||'');
    const commitLabel=/^[a-f0-9]{7,40}$/i.test(commit)?`<a href="${teamRepo}/commit/${encodeURIComponent(commit)}" target="_blank" rel="noreferrer">${esc(commit.slice(0,10))}</a>`:esc(commit||'Not recorded');
    return `<tr><td><strong>${esc(row.label||row.run_id)}</strong><small>${esc(row.author||'Author not recorded')} · ${esc(progressTime(row.created_at))}</small></td><td>${branch?`<a href="${teamRepo}/tree/${encodeURIComponent(branch)}" target="_blank" rel="noreferrer">${esc(branch)}</a>`:'Not recorded'}<small>${commitLabel}</small></td><td>${esc(row.benchmark||'No local evaluation')}<small>Run dataset: ${esc(String(row.phase||'Unknown').toUpperCase())}</small></td><td class="amount"><strong>${scoreValue(row.local_score)}</strong><small>${typeof row.local_score==='number'?'Local evaluation only':'Not evaluated locally'}</small></td><td class="amount">${percentValue(row.precision)} / ${percentValue(row.recall)}</td><td><span class="status ${check?'matched':'neutral'}">${check?'Reported passed':'Checks incomplete'}</span><small>${num(row.transactions)} transactions · ${Number.isFinite(row.seconds)?num(Math.round(row.seconds))+'s':'Runtime unknown'}</small><details><summary>Output identity</summary><code>${esc(row.sha256||'Hash not recorded')}</code><small>${esc(row.run_id)}</small></details></td></tr>`;
  }).join('')}</tbody></table></div>${snapshot?.runs_truncated?'<p class="footnote">Showing the most recent shared runs. The complete archive is available in the runs branch.</p>':''}`;
}
function renderProgress(){
  const blocks={'official-summary':officialSummaryHTML(progressData),'official-history':officialHistoryHTML(progressData),'team-history':teamHistoryHTML(progressData)};
  for(const [id,html] of Object.entries(blocks)){if($(id).innerHTML!==html)$(id).innerHTML=html;}
  const official=progressData?.official;
  const stale=progressUnavailable||!!official?.error;
  const status=$('progress-status');status.dataset.state=stale?'stale':'current';
  status.textContent=progressUnavailable
    ?(progressData?'Progress update unavailable · showing the last received results. Retrying automatically.':'Progress is temporarily unavailable. Retrying automatically.')
    :official?.error?'Official refresh unavailable · retained scores are labelled with their last check time.'
      :'Source: hackathon organizer API. Dashboard checks every 15 seconds while visible; official data refreshes at most once a minute.';
}
function scheduleProgress(){
  clearTimeout(progressTimer);progressTimer=null;
  if(document.visibilityState==='visible')progressTimer=setTimeout(checkProgress,refreshEvery);
}
async function checkProgress(){
  if(progressBusy||document.visibilityState!=='visible')return;
  clearTimeout(progressTimer);progressTimer=null;progressBusy=true;
  try{
    const next=await fetchJSON('/api/progress');
    if(!next||!Array.isArray(next.runs)||!('official' in next)||next.official!==null&&(typeof next.official!=='object'||!Array.isArray(next.official.submissions)))throw Error('Unsupported progress data');
    progressData=next;progressUnavailable=false;renderProgress();
  }catch(error){progressUnavailable=true;renderProgress();}
  finally{progressBusy=false;scheduleProgress();}
}
function setupProgress(){
  $('team-prompt').value=teamPrompt;
  $('copy-team-prompt').onclick=async()=>{
    try{
      if(!navigator.clipboard?.writeText)throw Error('Clipboard not available');
      await navigator.clipboard.writeText(teamPrompt);
      $('copy-prompt-status').textContent='Prompt copied. Paste it into your coding agent.';
    }catch(error){
      $('team-prompt').focus();$('team-prompt').select();
      $('copy-prompt-status').textContent='Prompt selected. Press Ctrl+C or ⌘C to copy it.';
    }
  };
  document.addEventListener('visibilitychange',()=>{
    clearTimeout(progressTimer);progressTimer=null;
    if(document.visibilityState==='visible')checkProgress();
  });
  checkProgress();
}
async function start(){
  try{
    data=await fetchJSON('data.json');validateSnapshot(data);
    $('app').hidden=false;summary();filter();renderProcess();renderExport();
    document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));
    document.querySelector('.brand').onclick=e=>{e.preventDefault();view('queue')};
    $('search').oninput=filter;$('outcome').onchange=filter;
    $('clear-filters').onclick=()=>{$('search').value='';$('outcome').value='all';filter()};
    $('previous').onclick=()=>{page--;renderQueue()};$('next').onclick=()=>{page++;renderQueue()};
    $('guided').onclick=walkthrough;
    view(['queue','process','export','progress'].includes(location.hash.slice(1))?location.hash.slice(1):'queue');
    setupRefresh();setupProgress();
  }catch(error){
    $('app').hidden=true;$('load-error').hidden=false;
    $('load-error').textContent='The run snapshot could not be loaded. Please try again shortly.\n'+error.message;
    $('run-time').textContent='Data unavailable';
  }
}
start();
