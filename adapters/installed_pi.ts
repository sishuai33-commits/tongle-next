/** Installed entry: keep unrelated Pi extensions, route this project locally. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import adapter from "./pi.ts";
export default function(pi: ExtensionAPI) {
  const path=process.env.TONGLE_NEXT_INSTALL || join(homedir(),'.config/tongle-next/active.json');
  try {
    const installed=JSON.parse(readFileSync(path,'utf8'));
    if (!installed.enabled) return;
    const route=(cwd: string)=>{
      const result=spawnSync(installed.python,[join(installed.root,'adapters/production.py'),cwd],{encoding:'utf8',timeout:5000});
      if(result.status!==0) {
        delete process.env.TONGLE_NEXT_HOME;
        throw new Error('project routing failed; capture disabled');
      }
      const cfg=JSON.parse(result.stdout);
      Object.assign(process.env,{TONGLE_NEXT_HOME:cfg.home,TONGLE_NEXT_ROOT:cfg.root,TONGLE_NEXT_PYTHON:cfg.python,TONGLE_NEXT_PROJECT:cfg.project,TONGLE_VAULT:cfg.vault});
    };
    // Register ahead of adapter callbacks so ctx.cwd wins on resumed/new sessions.
    pi.on('session_start',async (_event,ctx)=>route(ctx.cwd));
    pi.on('before_agent_start',async (_event,ctx)=>route(ctx.cwd));
    const child=spawn(installed.python,[join(installed.root,'scripts/maintain.py'),'--home',installed.home,'start'],{stdio:'ignore',detached:true});
    child.on('error',()=>console.error('[tongle-next] maintenance startup failed'));child.unref();
    adapter(pi);
  } catch(e) { console.error('[tongle-next] not activated: '+String(e)); }
}
