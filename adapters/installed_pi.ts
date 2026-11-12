/** Installed entry: keep unrelated Pi extensions, route this project locally. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import adapter, { managedRuntime } from "./pi.ts";
export default function(pi: ExtensionAPI) {
  const path=process.env.TONGLE_NEXT_INSTALL || join(homedir(),'.config/tongle-next/active.json');
  const maintenanceAttempts=new Set<string>();
  const route=(cwd: string)=>{
    try {
      // Re-read for every host boundary; a loaded extension must follow upgrade,
      // disable and subsequent setup, rather than an initial installation copy.
      const installed=JSON.parse(readFileSync(path,'utf8'));
      if (typeof installed.home!=="string") throw new Error('invalid installation home');
      const binding=managedRuntime(installed.home);
      if (!installed.enabled) {delete process.env.TONGLE_NEXT_HOME;maintenanceAttempts.clear();return;}
      if (binding && !binding.enabled) {
        process.env.TONGLE_NEXT_HOME=installed.home;maintenanceAttempts.clear();return;
      }
      const python=binding?.python ?? installed.python, root=binding?.root ?? installed.root;
      const result=spawnSync(python,[join(root,'adapters/production.py'),cwd],{
        encoding:'utf8',timeout:5000,env:{...process.env,TONGLE_NEXT_INSTALL:path}});
      if(result.status!==0) throw new Error('project routing failed; capture disabled');
      const cfg=JSON.parse(result.stdout);
      if (!cfg.enabled) { process.env.TONGLE_NEXT_HOME=installed.home;maintenanceAttempts.clear();return; }
      Object.assign(process.env,{TONGLE_NEXT_HOME:cfg.home,TONGLE_NEXT_ROOT:cfg.root,
        TONGLE_NEXT_PYTHON:cfg.python,TONGLE_NEXT_PROJECT:cfg.project,TONGLE_VAULT:cfg.vault});
      const identity=JSON.stringify([cfg.home,cfg.root,cfg.python]);
      if (!maintenanceAttempts.has(identity)) {
        maintenanceAttempts.add(identity);
        const child=spawn(cfg.python,[join(cfg.root,'scripts/maintain.py'),'--home',cfg.home,'start'],{stdio:'ignore',detached:true});
        child.on('error',()=>console.error('[tongle-next] maintenance startup failed'));child.unref();
      }
    } catch(error) {
      delete process.env.TONGLE_NEXT_HOME;
      console.error('[tongle-next] routing unavailable; capture disabled: '+String(error));
    }
  };
  // Register ahead of adapter callbacks so cwd routing wins before any capture.
  pi.on('session_start',async (_event,ctx)=>route(ctx.cwd));
  pi.on('before_agent_start',async (_event,ctx)=>route(ctx.cwd));
  adapter(pi);
}
