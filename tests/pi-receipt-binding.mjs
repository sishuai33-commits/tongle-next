// Native TypeScript callback contract against a real isolated Python core.
// No model: this covers persistence/branch logic, not semantic usefulness.
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import assert from 'node:assert/strict';
import adapter from '../adapters/pi.ts';
const root=resolve('.'), temp=mkdtempSync(join(tmpdir(),'tongle-receipts-')), home=join(temp,'home');
const python=process.env.PYTHON_EXECUTABLE || spawnSync(process.platform === 'win32' ? 'python' : 'python3', ['-c', 'import sys; print(sys.executable)'], {encoding:'utf8'}).stdout.trim();
process.env.TONGLE_NEXT_HOME=home; process.env.TONGLE_NEXT_ROOT=root;
process.env.TONGLE_NEXT_PROJECT='probe'; process.env.TONGLE_NEXT_PYTHON=python;
delete process.env.TONGLE_NEXT_CLI;
function core(args,input){const r=spawnSync(python,['-m','tongle','--home',home,...args],{cwd:root,input,encoding:'utf8'});assert.equal(r.status,0,r.stderr+r.stdout);return JSON.parse(r.stdout);}
function rows(){const r=spawnSync(python,['-c','import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;print(json.dumps([dict(x) for x in c.execute("select * from knowledge_consumption order by created_at")]))',join(home,'state.sqlite3')],{encoding:'utf8'});assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout);}
let callbacks={},branch=[];
function install(){callbacks={};adapter({on:(n,f)=>callbacks[n]=f});}
const ctx={cwd:join(temp,'workspace'),sessionManager:{getSessionId:()=> 'native-fixture',getLeafId:()=>branch.at(-1)?.id,getLeafEntry:()=>branch.at(-1),getBranch:()=>branch},hasUI:false,ui:{notify:()=>{}}};
async function prompt(id){const parentId=branch.at(-1)?.id??null;const r=await callbacks.before_agent_start({prompt:'继续项目'},ctx);assert.ok(r?.message);branch.push({id,parentId,type:'message',message:{role:'user',content:[{type:'text',text:'继续项目'}]}});branch.push({id:'ctx-'+id,parentId:id,type:'custom_message',...r.message});return r.message.details.receiptId;}
async function promptInsertedSystem(id){const anchor=branch.at(-1)?.id??null;const r=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(r?.message);const sysId='sys-'+id;branch.push({id:sysId,parentId:anchor,type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});branch.push({id,parentId:sysId,type:'message',message:{role:'user',content:[{type:'text',text:'继续项目方案'}]}});branch.push({id:'ctx-'+id,parentId:id,type:'custom_message',...r.message});return r.message.details.receiptId;}
async function answer(id,reason='stop'){await callbacks.message_end({message:{role:'assistant',responseId:id,stopReason:reason,content:[{type:'text',text:'本次只做方案。'}]}},ctx);}
try{
 core(['init','--vault',join(temp,'vault'),'--evolving']);
 writeFileSync(join(home,'config.json'),'{"knowledge_mode":"evolving"}');
 const seed=spawnSync(python,['-c', 'from tongle.engine import Engine;from tongle.evolution import Evolution;from tongle.knowledge_files import KnowledgeFiles;import sys;e=Engine(sys.argv[1]);ev=Evolution(e);ev.cards.seed("probe","scope","memory","任务","当前范围","本次只做方案。");KnowledgeFiles(e).publish();e.store.close()',home],{cwd:root,encoding:'utf8'});assert.equal(seed.status,0,seed.stderr);install();
 const first=await prompt('u1'), firstBranch=[...branch];
 const second=await prompt('u2'), secondBranch=[...branch];
 // Cancellation leaves u2 unbound; a branch switch to u1 binds exactly u1.
 await answer('cancelled','aborted');branch=firstBranch;install();await answer('a1');
 assert.equal(rows().find(r=>r.id===first).binding,'receipt');
 assert.equal(rows().find(r=>r.id===second).output_source,null);
 branch=secondBranch;await answer('a2');const saved=rows().find(r=>r.id===second).output_source;
 await answer('a2');assert.equal(rows().find(r=>r.id===second).output_source,saved);
 // New user without a context cannot inherit an earlier injection.
 const extra=core(['context','--project','probe','--session','native-fixture','--text','继续']).receipt_id;
 branch.push({id:'u3',type:'message',message:{role:'user',content:'继续项目'}});await answer('a3');
 assert.equal(rows().find(r=>r.id===extra).output_source,null);
 // Multiple different contexts for a user are conservatively ambiguous.
 branch.push({id:'ambiguous',type:'custom_message',customType:'tongle-next-context',details:{previousEntryId:null,promptHash:(await import('node:crypto')).createHash('sha256').update('继续项目').digest('hex'),receiptId:extra}});
 const extra2=core(['context','--project','probe','--session','native-fixture','--text','继续']).receipt_id;
 branch.push({id:'ambiguous2',type:'custom_message',customType:'tongle-next-context',details:{previousEntryId:null,promptHash:(await import('node:crypto')).createHash('sha256').update('继续项目').digest('hex'),receiptId:extra2}});
 await answer('a4');assert.equal(rows().find(r=>r.id===extra2).output_source,null);
 // Actual installed Pi ordering: before_agent_start precedes user persistence,
 // then a system message is inserted before the user entry (nonroot anchor).
 const anchor=branch.at(-1).id;
 const r=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(r?.message);
 branch.push({id:'sys-u4',parentId:anchor,type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});
 branch.push({id:'u4',parentId:'sys-u4',type:'message',message:{role:'user',content:[{type:'text',text:'继续项目方案'}]}});
 branch.push({id:'ctx-u4',parentId:'u4',type:'custom_message',...r.message});
 await answer('a5');assert.equal(rows().find(x=>x.id===r.message.details.receiptId).binding,'receipt');
 // Root-anchor variant: anchor null with only a system chain before the user.
 branch=[];
 const rootCase=await promptInsertedSystem('u-root');
 await answer('a-root');
 assert.equal(rows().find(x=>x.id===rootCase).binding,'receipt');
 // Missing anchor equal to user parent (null previousEntryId) rejects.
 const missingAnchorR=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(missingAnchorR?.message);
 branch.push({id:'missing-anchor-user',parentId:branch.at(-1)?.id??null,type:'message',message:{role:'user',content:'继续项目方案'}});
 branch.push({id:'ctx-missing-anchor',parentId:'missing-anchor-user',type:'custom_message',...missingAnchorR.message,details:{...missingAnchorR.message.details,previousEntryId:null}});
 await answer('a-missing-anchor');
 assert.equal(rows().find(x=>x.id===missingAnchorR.message.details.receiptId).output_source,null);
 // Missing details anchor (undefined previousEntryId) rejects.
 const undefAnchorR=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(undefAnchorR?.message);
 branch.push({id:'undef-anchor-user',parentId:branch.at(-1)?.id??null,type:'message',message:{role:'user',content:'继续项目方案'}});
 branch.push({id:'ctx-undef-anchor',parentId:'undef-anchor-user',type:'custom_message',...undefAnchorR.message,details:{...undefAnchorR.message.details,previousEntryId:undefined}});
 await answer('a-undef-anchor');
 assert.equal(rows().find(x=>x.id===undefAnchorR.message.details.receiptId).output_source,null);
 // Intervening non-system node (assistant) between anchor and user rejects.
 const interAnchor=branch.at(-1).id;
 const interR=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(interR?.message);
 branch.push({id:'sys-inter',parentId:interAnchor,type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});
 branch.push({id:'asst-inter',parentId:'sys-inter',type:'message',message:{role:'assistant',content:[{type:'text',text:'blah'}]}});
 branch.push({id:'u-inter',parentId:'asst-inter',type:'message',message:{role:'user',content:[{type:'text',text:'继续项目方案'}]}});
 branch.push({id:'ctx-inter',parentId:'u-inter',type:'custom_message',...interR.message});
 await answer('a-inter');
 assert.equal(rows().find(x=>x.id===interR.message.details.receiptId).output_source,null);
 // Intervening custom_message node (not a system message) rejects.
 const customAnchor=branch.at(-1).id;
 const customR=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(customR?.message);
 branch.push({id:'sys-custom',parentId:customAnchor,type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});
 branch.push({id:'other-custom',parentId:'sys-custom',type:'custom_message',customType:'other',details:{}});
 branch.push({id:'u-custom',parentId:'other-custom',type:'message',message:{role:'user',content:[{type:'text',text:'继续项目方案'}]}});
 branch.push({id:'ctx-custom',parentId:'u-custom',type:'custom_message',...customR.message});
 await answer('a-custom');
 assert.equal(rows().find(x=>x.id===customR.message.details.receiptId).output_source,null);
 // Missing system parent (non-null but unresolvable) rejects instead of binding.
 const missingParentAnchor=branch.at(-1).id;
 const missingParentR=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(missingParentR?.message);
 branch.push({id:'sys-missing-parent',parentId:'does-not-exist',type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});
 branch.push({id:'u-missing-parent',parentId:'sys-missing-parent',type:'message',message:{role:'user',content:[{type:'text',text:'继续项目方案'}]}});
 branch.push({id:'ctx-missing-parent',parentId:'u-missing-parent',type:'custom_message',...missingParentR.message});
 await answer('a-missing-parent');
 assert.equal(rows().find(x=>x.id===missingParentR.message.details.receiptId).output_source,null);
 // Cycle in the parent chain rejects and must terminate traversal.
 const cycleAnchor=branch.at(-1).id;
 const cycleR=await callbacks.before_agent_start({prompt:'继续项目方案'},ctx);assert.ok(cycleR?.message);
 branch.push({id:'sys-cycle-a',parentId:'sys-cycle-b',type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});
 branch.push({id:'sys-cycle-b',parentId:'sys-cycle-a',type:'message',message:{role:'system',content:[{type:'text',text:'ok'}]}});
 branch.push({id:'u-cycle',parentId:'sys-cycle-a',type:'message',message:{role:'user',content:'继续项目方案'}});
 branch.push({id:'ctx-cycle',parentId:'u-cycle',type:'custom_message',...cycleR.message});
 await answer('a-cycle');
 assert.equal(rows().find(x=>x.id===cycleR.message.details.receiptId).output_source,null);
 void missingParentAnchor; void cycleAnchor;
 // A changed anchor must not bind even when text matches.
 const wrong=core(['context','--project','probe','--session','native-fixture','--text','继续']).receipt_id;
 branch.push({id:'u5',parentId:'different',type:'message',message:{role:'user',content:'继续项目方案'}});
 branch.push({id:'wrong-anchor',type:'custom_message',customType:'tongle-next-context',details:{...r.message.details,receiptId:wrong}});
 await answer('a6');assert.equal(rows().find(x=>x.id===wrong).output_source,null);
 // A same-text USER leaf is the previous turn, never the current one.
 branch.push({id:'previous-user-only',parentId:branch.at(-1).id,type:'message',message:{role:'user',content:'继续项目'}});
 const leafCase=await prompt('same-text-new-user');await answer('a7');
 assert.equal(rows().find(x=>x.id===leafCase).binding,'receipt');
 // Same-text sibling users share an anchor but not their descendant receipts.
 const common=[...branch];const siblingA=await prompt('sibling-a');const branchA=[...branch];
 branch=[...common];const siblingB=await prompt('sibling-b');await answer('sibling-b-answer');
 assert.equal(rows().find(x=>x.id===siblingA).output_source,null);
 branch=branchA;await answer('sibling-a-answer');
 assert.equal(rows().find(x=>x.id===siblingA).binding,'receipt');
 assert.notEqual(rows().find(x=>x.id===siblingA).output_source,rows().find(x=>x.id===siblingB).output_source);
 console.log('PASS: exact receipt, same-text turns, cancellation, branch switch, restart, replay, system-insertion ancestry and unknown boundaries');
}finally{rmSync(temp,{recursive:true,force:true});}
