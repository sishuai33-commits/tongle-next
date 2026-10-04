// Real TypeScript adapter callbacks with an isolated real core, no model calls.
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import adapter from '../adapters/pi.ts';
const root=resolve('.'),temp=mkdtempSync(join(tmpdir(),'tongle-final-')),home=join(temp,'home');
process.env.TONGLE_NEXT_HOME=home;process.env.TONGLE_NEXT_ROOT=root;process.env.TONGLE_NEXT_PROJECT='probe';process.env.TONGLE_NEXT_PYTHON='/Library/Frameworks/Python.framework/Versions/3.12/bin/python3';
function core(args){const r=spawnSync(process.env.TONGLE_NEXT_PYTHON,['-m','tongle','--home',home,...args],{cwd:root,encoding:'utf8'});if(r.status)throw Error(r.stderr+r.stdout);return JSON.parse(r.stdout);}
try{
 core(['init','--vault',join(temp,'vault')]);let callbacks={};adapter({on:(name,fn)=>callbacks[name]=fn});
 const ctx={cwd:root,sessionManager:{getSessionId:()=> 'isolated',getLeafId:()=> 'leaf'},ui:{notify:()=>{}},hasUI:false};
 for(const reason of ['toolUse','error','aborted','length',undefined,'stop'])await callbacks.message_end({message:{role:'assistant',stopReason:reason,responseId:String(reason),content:[{type:'text',text:'最终答复测试'}]}},ctx);
 const r=spawnSync(process.env.TONGLE_NEXT_PYTHON,['-c','import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("select count(*) from events").fetchone()[0])',join(home,'state.sqlite3')],{encoding:'utf8'});
 if(r.stdout.trim()!=='1')throw Error('expected exactly final stop captured: '+r.stdout+r.stderr);console.log('PASS: final answer captured; tool-use/error/partial/unknown ignored');
}finally{rmSync(temp,{recursive:true,force:true});}
