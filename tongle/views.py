"""Rebuild small human navigation from published cards; never from event logs."""
import hashlib,json,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .knowledge_store import KnowledgeStore
from .knowledge_files import KnowledgeFiles
from .presentation import label

_BEIJING=timezone(timedelta(hours=8))

def topic_snapshot(engine):
 """Read-only counts and bounded coverage, not source bodies or model work."""
 exists=engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_topics'").fetchone()
 if not exists:return {'states':{},'records':[],'utility':'unknown'}
 rows=engine.db.execute('SELECT * FROM knowledge_topics ORDER BY project,topic,partition').fetchall()
 records=[]
 for row in rows:
  payload=json.loads(row['payload']); coverage=payload.get('snapshot',{}).get('coverage',{})
  records.append({'id':row['id'],'project':row['project'],'topic':row['topic'],'partition':row['partition'],
    'state':row['state'],'target_id':row['target_id'],'covered':len(coverage.get('covered',[])),
    'excluded':len(coverage.get('excluded',[])),'reason':payload.get('reason')})
 return {'states':dict(engine.db.execute('SELECT state,COUNT(*) FROM knowledge_topics GROUP BY state')),
         'records':records,'utility':'unknown'}


def learning_snapshot(engine):
 """Counts only; read status never creates learning schema or starts a model."""
 table=engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_learning'").fetchone()
 states=dict(engine.db.execute('SELECT state,COUNT(*) FROM knowledge_learning GROUP BY state')) if table else {}
 comparisons=[]
 if table:
  for row in engine.db.execute("SELECT id,state,payload FROM knowledge_learning WHERE state IN ('published','paused','no_gain','regression','attention') ORDER BY updated_at DESC LIMIT 10"):
   result=json.loads(row['payload']).get('comparison')
   if result:comparisons.append(dict(result,id=row['id'],state=row['state']))
 return {'states':states,'published':states.get('published',0),'paused':states.get('paused',0),
         'comparisons':comparisons[:3],
         'note':'受控对照与日常收益分开；没有结果保持未知，不把调用或使用计作增益。'}


def status_snapshot(engine,counts):
 """UTC-day accounting; completed unknown usage is distinct from in-flight."""
 work={r[0]:r[1] for r in engine.db.execute('SELECT state,COUNT(*) FROM knowledge_work GROUP BY state')}
 now=time.time();utc_day=time.strftime('%Y-%m-%d',time.gmtime(now))
 rows=[dict(r) for r in engine.db.execute('SELECT * FROM knowledge_calls WHERE day=?',(utc_day,))]
 complete=[r for r in rows if r['elapsed'] is not None or r['error'] is not None]
 failures=sum(r['error'] is not None for r in complete)
 measured=[r for r in rows if r['actual_input'] is not None and r['actual_output'] is not None]
 unknown=[r for r in rows if r not in measured]
 settled_tokens=sum(r['actual_input']+r['actual_output'] for r in measured)
 reserved_tokens=sum(r['reserved'] for r in unknown)
 # Gate figure the budget check actually uses: measured for settled calls,
 # full reservation while in flight, bounded nominal for finished-unknown.
 from .evolution import _int_or
 cfg_path=engine.home/'config.json'
 cfg=json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.is_file() else {}
 nominal=max(0,_int_or(cfg.get('knowledge_unknown_nominal_tokens'),40000))
 finished_unknown=[r for r in unknown if r['elapsed'] is not None]
 in_flight=[r for r in unknown if r['elapsed'] is None]
 gate_tokens=(settled_tokens+sum(r['reserved'] for r in in_flight)
              +sum(min(r['reserved'],nominal) for r in finished_unknown))
 breaker=_breaker_state(engine)
 redactions=_redaction_summary(engine)
 usage={'calls':len(rows),'input_tokens':sum(r['actual_input'] for r in rows if r['actual_input'] is not None) if any(r['actual_input'] is not None for r in rows) else None,
        'output_tokens':sum(r['actual_output'] for r in rows if r['actual_output'] is not None) if any(r['actual_output'] is not None for r in rows) else None}
 # A paused version's responsibility is still retained: the row is selected into
 # the operator-facing list (never silently dropped), so the pause and its
 # recovery condition are always visible. It is *not* done, obsolete or
 # responsibility-free, and no scheduler resumes it.
 errors=[dict(r) for r in engine.db.execute("SELECT project,state,error FROM knowledge_work WHERE state IN ('failed','publication_pending','operator_publication_pending','coverage_wait','blocked','protocol_paused') ORDER BY created_at DESC LIMIT 12")]
 oldest=engine.db.execute("SELECT MIN(created_at) FROM knowledge_work WHERE state NOT IN ('done','obsolete','publication_pending','failed')").fetchone()[0]
 nxt=engine.db.execute("SELECT MIN(next_at) FROM knowledge_work WHERE state IN ('retry','budget_wait') AND next_at>?",(now,)).fetchone()[0]
 return {'work':work,'usage':usage,'counts':counts,'calls':len(rows),'day_calls':len(rows),'failures':failures,'completed_calls':len(complete),
         'error_rate':failures/len(complete)*100 if complete else 0.,'errors':errors,'beijing_time':datetime.fromtimestamp(now,_BEIJING).strftime('%Y-%m-%d %H:%M'),
         'utc_day':utc_day,'in_flight':len(rows)-len(complete),'settled':len(measured),'settled_tokens':settled_tokens,'reserved_tokens':reserved_tokens,
         'reserved_unknown':len(unknown),'oldest_age':max(0.,now-oldest) if oldest is not None else None,
         'gate_tokens':gate_tokens,'unknown_nominal':nominal,
         'finished_unknown':len(finished_unknown),'breaker':breaker,'redactions':redactions,
         'budget_tokens':_int_or(cfg.get('knowledge_daily_token_budget'),300000),
         'budget_calls':_int_or(cfg.get('knowledge_daily_call_budget'),40),
         'next_retry':datetime.fromtimestamp(nxt,_BEIJING).strftime('%Y-%m-%d %H:%M') if nxt else None,
         'publication_pending':work.get('publication_pending',0),'obsolete':work.get('obsolete',0),'unbatched_parts':_count_unbatched_parts(engine),
         'done':work.get('done',0),'failed':work.get('failed',0),
         'auto_pending':sum(work.get(k,0) for k in ('pending','running','retry')),
         # ``operator_publication_pending`` is an operator exception completion
         # committed but not yet published: a distinct waiting cause that is
         # NEVER folded into done/obsolete and never treated as
         # no-responsibility. It is resumed only from a real operator intent.
         'waiting':sum(work.get(k,0) for k in ('budget_wait','blocked','coverage_wait','protocol_paused','operator_publication_pending')),
         'protocol_paused':work.get('protocol_paused',0),
         'operator_publication_pending':work.get('operator_publication_pending',0),
         # 'pending' is the actionable backlog only: a failed batch is *stopped*
         # (needs an operator condition or a corrected mechanism), never
         # silently counted as still-to-do, and a committed batch is separately
         # 'publication_pending'.
         'pending':sum(work.get(k,0) for k in ('pending','running','retry','budget_wait','blocked','coverage_wait','protocol_paused','operator_publication_pending'))}


def _breaker_state(engine):
 """Read the outage breaker without importing Evolution's instance state."""
 try:
  row=engine.db.execute("SELECT value FROM meta WHERE key='knowledge_breaker'").fetchone()
  state=json.loads(row[0]) if row else {}
  if not isinstance(state,dict):state={}
  until=float(state.get('until') or 0);streak=int(state.get('streak') or 0)
  return {'open':until>time.time(),'streak':streak,
          'category':state.get('category'),
          'until':datetime.fromtimestamp(until,_BEIJING).strftime('%Y-%m-%d %H:%M') if until>time.time() else None}
 except (ValueError,TypeError,KeyError):
  return {'open':False,'streak':0,'category':None,'until':None}

def _redaction_summary(engine):
 """Capture-boundary redaction audit; counts only, never the removed value."""
 try:
  total=engine.db.execute('SELECT COUNT(*),COALESCE(SUM(hits),0) FROM redactions').fetchone()
  rules=engine.db.execute('SELECT rules,SUM(hits) FROM redactions GROUP BY rules ORDER BY SUM(hits) DESC LIMIT 5').fetchall()
  return {'records':total[0],'hits':total[1],
          'rules':', '.join('%s×%d'%(r[0],r[1]) for r in rules)}
 except Exception:
  return {'records':0,'hits':0,'rules':''}


def _count_unbatched_parts(engine):
 """Count source parts with no knowledge_seen row, using real part boundaries.

 Reuses Evolution._source_parts so the count matches the queue's own units;
 this is computed only while generating status, never stored on a card, and
 never approximated by summing whole-source counts. Failures return unknown so
 a status page never crashes on an unavailable source.
 """
 try:
  from .evolution import Evolution
  evolution=Evolution(engine)
  total=0
  for row in engine.db.execute('SELECT id FROM sources').fetchall():
   if not engine.source_available(row['id']):continue
   try:source=engine.store.source(row['id'])
   except (ValueError,KeyError,OSError):continue
   for part in evolution._source_parts(source):
    if not engine.db.execute('SELECT 1 FROM knowledge_seen WHERE source_id=? AND version=? AND part=?',
                             (source['source_id'],source['current_version'],part['part'])).fetchone():total+=1
  return total
 except (ValueError,KeyError,OSError,RuntimeError):
  return None

# Stable, host-generated failure phrases mapped to short safe labels. Only
# these fixed phrases can be shown; arbitrary error text (which might carry
# reconstructed content) is never echoed. Order matters: the first match wins.
_STAGE_REASONS=(
 ('stage call limit','阶段调用次数达上限'),
 ('unread required','必需卡片未能完整投递'),
 ('could not be satisfied','必需卡片未能完整投递'),
 ('create duplicates existing card','新增与既有卡重复'),
 ('compiler emitted unread targets','目标卡未完整阅读'),
 ('cached proposal targets unread cards','缓存提案目标未阅读'),
 ('duplicate candidates changed','相关查重结果变化，需重建'),
 ('related duplicate candidates changed','相关查重结果变化，需重建'),
 ('related hits changed','相关检索结果变化，需重建'),
 ('SelectionChanged','相关卡在提交前出现或变化，需重建'),
 ('search results changed','检索结果变化，需重建'),
 ('mandatory cards exceed body budget','必需卡超出正文预算'),
 ('mandatory dependency unresolved','必需依赖未解析'),
 ('unresolved basis dependency','依据依赖未解析'),
 ('compiler dependency unresolved','依据依赖未解析'),
 ('review not approved','独立复核未通过'),
 ('read-set version changed','已读卡版本变化，需重建'),
 ('read-set body changed','已读卡正文变化，需重建'),
 ('read-set card vanished','已读卡消失，需重建'),
 ('source succession chain depth limit','来源连续变更超过后继链上限'),
 ('source superseded','来源版本已过期，已转移后继'),
 ('source version superseded','来源版本已过期，已转移后继'),
 ('source revoked','来源已被撤回，需人工处理'),
 ('source missing','来源暂缺，条件恢复后自动继续'),
 ('source unavailable','来源不可用'),
 ('authorized catalog exceeds fixed bound','候选目录超出固定上限（旧机制）'),
 ('retry limit reached','重试次数达上限'),
)

def _safe_error_text(error):
 from .model_errors import CATEGORIES,SAFE_CODES
 labels=[]
 for needle,label in _STAGE_REASONS:
  if needle in (error or ''):
   labels.append(label);break
 if labels:return labels[0]
 import re
 tokens=set(re.findall(r'[a-z_]+',error or ''))
 categories=[c for c in CATEGORIES if c in tokens]
 codes=[c for c in SAFE_CODES if c in tokens]
 return '; '.join(categories+codes) if categories else '历史错误，原因未分类'

def refresh(engine):
 files=KnowledgeFiles(engine);files.sync();cards=KnowledgeStore(engine).list(include_inactive=True)
 from .evolution import Evolution
 evolution=Evolution(engine)
 with engine.locked(),engine.transaction():
  engine.db.execute('CREATE TABLE IF NOT EXISTS knowledge_views(path TEXT PRIMARY KEY,file_hash TEXT,state TEXT)')
 groups={}
 for c in cards:
  if not c['path'] or c['state']=='retired':continue
  if not c['path'].startswith(('知识/','项目/','资料/')):continue
  parts=Path(c['path']).parts
  if len(parts)<3:continue
  groups.setdefault('/'.join(parts[:2]),[]).append(c)
 pages={};counts={'active':0,'conflict':0}
 for folder,items in sorted(groups.items()):
  title=Path(folder).name;body='# '+title+'\n\n'
  body+='本页按已整理资产生成。正文保留来源、适用条件与历史；具体项目进度以项目原件为准。\n\n'
  for state,heading in [('active','可用资料'),('conflict','保留但需回源核对')]:
   selected=[c for c in items if c['state']==state]
   if not selected:continue
   body+='## '+heading+'\n\n'
   for c in sorted(selected,key=lambda c:(c['kind']!='memory',c['title'])):
    tracking=files._tracking(c['id']);suffix=''
    if not tracking or tracking['state']!='ok' or tracking['version']!=c['version']:suffix='（文件待同步）'
    elif c['state']=='active' and not evolution._valid_card(c):suffix='（依据已变化，暂不自动提供）'
    body+='- [['+c['path'][:-3]+'|'+c['title'].replace('|',' ')+']]'+suffix+'\n';counts[state]+=1
   body+='\n'
  pages[folder+'/总览.md']=body
 home='# 我的知识库\n\n项目决定有原件可查，知识有条件和出处；历史留存，但不自动当作现行结论。\n\n'
 for prefix,heading in [('项目','当前项目'),('知识','知识主题'),('资料','阅读资料')]:
  home+='## '+heading+'\n\n'
  for folder,items in sorted(groups.items()):
   if folder.startswith(prefix+'/'):home+='- [['+folder+'/总览|'+Path(folder).name+']] · '+str(len(items))+' 项\n'
  home+='\n'
 home+='[[随笔/总览|随笔与复盘]] · [[归档/总览|历史资料]]\n\n[[运行情况|运行与维护情况]] · [[使用说明|使用说明]]\n'
 pages['首页.md']=home
 for folder,title in [('随笔','随笔与复盘'),('归档','历史资料')]:
  root=files.root/folder
  body='# '+title+'\n\n'+('这里保留历史依据，不代表当前决定或有效规则。' if folder=='归档' else '讨论、观察与复盘保留在这里；有充分依据的结论再进入知识。')+'\n\n'
  group={}
  for p in sorted(root.rglob('*.md')) if root.is_dir() and not root.is_symlink() else []:
   rel=p.relative_to(files.root)
   if p.name=='总览.md' or any(x.startswith('.') for x in rel.parts) or any(x.is_symlink() for x in [p,*p.parents]):continue
   group.setdefault(rel.parts[1] if len(rel.parts)>2 else '记录',[]).append(rel)
  for key,paths in sorted(group.items()):
   body+='> [!note]- '+key+' · '+str(len(paths))+' 项\n'
   for p in paths:body+='> - [['+p.as_posix()[:-3]+'|'+p.stem.replace('|',' ')+']]\n'
   body+='\n'
  pages[folder+'/总览.md']=body
 pages['使用说明.md']=GUIDE
 snap=status_snapshot(engine,counts)
 oldest=('未记录' if snap['oldest_age'] is None else f"{int(snap['oldest_age']//3600)} 小时")
 next_retry=snap['next_retry'] or '无'
 _STATE_LABELS={'failed':'已停止自动重试（需运维处理）','publication_pending':'知识已提交，等待文件发布恢复','operator_publication_pending':'运维变更已提交、等待文件发布','coverage_wait':'条件等待：相关内容超过本批预算','blocked':'条件等待：来源暂不可用','budget_wait':'条件等待：额度恢复后继续','protocol_paused':'维护版本已暂停（责任仍保留）','pending':'自动推进','running':'自动推进','retry':'自动推进（将重试）'}
 _STATE_RECOVERY={'failed':'恢复条件：排除原因后由运维重排或修复机制','publication_pending':'恢复条件：磁盘/权限可用后自动补发布','operator_publication_pending':'恢复条件：磁盘/权限可用后按原运维意图自动补发布（不再调用模型、不重复写入）','coverage_wait':'恢复条件：材料或预算条件变化后自动继续','blocked':'恢复条件：来源恢复可用后自动继续','budget_wait':'恢复条件：次日额度恢复或提高后继续','protocol_paused':'恢复条件：恢复对应版本后按原暂停凭据继续'}
 errors_text='\n'.join('- '+e['project']+'：'+_STATE_LABELS.get(e['state'],e['state'])+('（'+_safe_error_text(e['error'])+'）' if e['error'] else '')+('；'+_STATE_RECOVERY[e['state']] if e['state'] in _STATE_RECOVERY else '') for e in snap['errors'])
 pre_conflicts=[]
 for rel0,body0 in pages.items():
  t0=files.root/rel0
  if any(p.is_symlink() for p in [t0,*t0.parents]):pre_conflicts.append(rel0);continue
  r0=engine.db.execute('SELECT file_hash FROM knowledge_views WHERE path=?',(rel0,)).fetchone()
  old0=t0.read_bytes() if t0.is_file() else None
  h0=hashlib.sha256(old0).hexdigest() if old0 is not None else None
  d0=hashlib.sha256(body0.encode()).hexdigest()
  if h0 is not None and h0!=d0 and ((r0 and h0!=r0[0]) or not r0):pre_conflicts.append(rel0)
 manual=engine.db.execute('SELECT c.title,c.path,r.old_version FROM knowledge_revisions r JOIN knowledge_cards c ON c.id=r.card_id WHERE c.manually_edited=1 ORDER BY r.created_at DESC LIMIT 8').fetchall()
 conflict_cards=engine.db.execute("SELECT title,path FROM knowledge_cards WHERE state='conflict' ORDER BY updated_at DESC").fetchall()
 extras=''
 breaker=snap['breaker']
 extras+='\n- 日预算门限：'+str(snap['gate_tokens'])+' / '+str(snap['budget_tokens'])+' tokens；调用 '+str(snap['calls'])+' / '+str(snap['budget_calls'])+'。（已结束但用量未知的 '+str(snap['finished_unknown'])+' 次按名义值 '+str(snap['unknown_nominal'])+' 计入门限，避免上游故障把额度烧成次日预算墙。）'
 if breaker['open']:
  extras+='\n- 上游故障断路器：已暂停队列至 '+str(breaker['until'])+'（北京时间），连续失败 '+str(breaker['streak'])+' 次，最近类别 '+str(breaker['category'])+'；上游恢复后会自行继续。'
 elif breaker['streak']:
  extras+='\n- 上游故障断路器：未触发（连续失败计数 '+str(breaker['streak'])+'，达阈值即暂停队列）。'
 else:
  extras+='\n- 上游故障断路器：正常。'
 red=snap['redactions']
 extras+='\n- 捕获期脱敏：'+str(red['records'])+' 条来源被改写，共 '+str(red['hits'])+' 处（规则 '+((red['rules']) or '无')+'）。只对真实凭证形态生效；被移除的值不落库，改写已记入 redactions 审计表。'
 if pre_conflicts:extras+='\n- 生成页冲突：'+'、'.join(pre_conflicts)+'（人工改动未自动覆盖；删除该文件可在下轮重建）'
 if manual:extras+='\n- 人工编辑卡近期自动改写（保真核验=模型自证，建议抽查）：'+'；'.join(m['title'][:36]+' v'+str(m['old_version'])+'→v'+str(m['old_version']+1) for m in manual)
 if conflict_cards:extras+='\n- 保留待核分歧：'+str(len(conflict_cards))+' 张卡不进注入，裁决方式见 docs/operations.md；近期：'+'、'.join((m['title'] or m['path'])[:24] for m in conflict_cards[:8])
 pages['运行情况.md']='# 运行与维护情况\n\n这是本地记录的状态，不能据此推定每个终端都已自动接入，也不能把提供资料次数当作采用次数。\n\n'+f"- 生成时间（北京时间）：{snap['beijing_time']}。\n- 当前整理资料：{snap['counts']['active']} 项；保留待核资料：{snap['counts']['conflict']} 项。\n- 已完成维护批次：{snap['done']}；自动推进/可重试：{snap['auto_pending']}；条件等待（额度/来源/预算/版本暂停）：{snap['waiting']}；已停止自动重试：{snap['failed']}；已作废（不待处理）：{snap['obsolete']}。\n- 其中维护版本已暂停（责任仍保留，不计入完成/作废）：{snap['protocol_paused']} 项；恢复条件：恢复对应版本后按原暂停凭据继续。\n- 待处理合计（不含已停止与已提交）：{snap['pending']}。\n- 未入批片段数：{snap['unbatched_parts']}（按来源片段计数，非来源数）。\n- 最老待处理年龄：{oldest}；下一次重试（北京时间）：{next_retry}。\n- 知识已提交、等待文件发布：{snap['publication_pending']} 项（单列，不计入完成）。\n- 运维例外变更已提交、等待文件发布：{snap['operator_publication_pending']} 项（按原运维意图自动补发布，不再调用模型、不重复写入，也不计为已完成）。\n- UTC 当天 {snap['utc_day']} 调用：{snap['calls']}；已结束 {snap['completed_calls']}，报错 {snap['failures']}；已结束调用错误率：{snap['error_rate']:.1f}%。\n- 已知完整用量：{snap['settled_tokens']} tokens（{snap['settled']} 次）；未知用量预留：{snap['reserved_tokens']} tokens（{snap['reserved_unknown']} 次）；在途：{snap['in_flight']}（不计成功）。\n- 已取得输入 tokens（不含未知项）：{snap['usage']['input_tokens'] if snap['usage']['input_tokens'] is not None else '未取得'}；已取得输出 tokens（不含未知项）：{snap['usage']['output_tokens'] if snap['usage']['output_tokens'] is not None else '未取得'}。\n- 订阅实际扣费无法从本地调用次数确认。\n\n"+(errors_text+('\n' if errors_text else ''))+extras
 learning=learning_snapshot(engine)
 if learning['states']:
  labels={'observed':'待分析','reviewing':'待复核','awaiting_cases':'待独立新任务','validating':'对照中','publication_pending':'待发布','published':'已发布','paused':'已暂停','attention':'需处理','closed':'已归因关闭','needs_evidence':'证据不足','no_gain':'未达发布门槛','regression':'发现退步'}
  pages['运行情况.md']+='\n## 知识改进\n\n'+'；'.join('%s：%s'%(labels.get(k,k),v) for k,v in sorted(learning['states'].items()))+'。\n\n'+learning['note']+'\n'
  for item in learning['comparisons']:
   pages['运行情况.md']+='\n- 对照 '+item['id'][:8]+'：当前知识 %s/%s，候选 %s/%s；%s。'%(item['current_pass'],item['tasks'],item['candidate_pass'],item['tasks'],labels.get(item['state'],item['state']))
 topics=topic_snapshot(engine)
 if topics['states']:
  names={'propose':'待综合','review':'待复核','publication_pending':'待发布','published':'已发布','waiting':'待有效依据','attention':'需处理'}
  pages['运行情况.md']+='\n## 主题知识综合\n\n'+'；'.join(names.get(k,k)+'：'+str(v) for k,v in sorted(topics['states'].items()))+'。内容通过复核不等于已证明实际收益。\n'
  for item in topics['records'][:20]:
   pages['运行情况.md']+='\n- '+item['project']+' / '+item['topic']+' / 分区 '+str(item['partition'])+'：'+names.get(item['state'],item['state'])+'，覆盖 '+str(item['covered'])+' 张基础卡，排除 '+str(item['excluded'])+' 张。'
 changed=[];conflicts=[];pruned=[]
 with engine.locked():
  for rel,body in pages.items():
   # Root views intentionally bypass card namespace requirement; they are
   # fixed generated paths, never model paths or metadata-controlled paths.
   target=files.root/rel
   if any(p.is_symlink() for p in [target,*target.parents]):conflicts.append(rel);continue
   row=engine.db.execute('SELECT file_hash FROM knowledge_views WHERE path=?',(rel,)).fetchone()
   old=target.read_bytes() if target.is_file() else None;h=hashlib.sha256(old).hexdigest() if old is not None else None
   desired=hashlib.sha256(body.encode()).hexdigest()
   if h==desired:pass
   # Missing file (h is None) always regenerates: a stale knowledge_views row
   # from a deleted directory must not permanently block its own page (the
   # old `row and h!=row[0]` branch turned deletion into a permanent conflict).
   elif h is not None and ((row and h!=row[0]) or not row):conflicts.append(rel);continue
   else:
    target.parent.mkdir(parents=True,exist_ok=True)
    # Same-directory exclusive or replace write, rechecked immediately.
    import tempfile,os
    fd,temp=tempfile.mkstemp(dir=target.parent,prefix='.tongle-view-')
    try:
     with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:f.write(body);f.flush();os.fsync(f.fileno())
     actual=hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
     if actual!=h:conflicts.append(rel);continue
     if old is None:os.link(temp,target)
     else:os.replace(temp,target)
     changed.append(rel)
    finally:
     if os.path.exists(temp):os.unlink(temp)
   with engine.transaction():engine.db.execute("INSERT INTO knowledge_views VALUES(?,?,'ok') ON CONFLICT(path) DO UPDATE SET file_hash=excluded.file_hash,state='ok'",(rel,desired))
  # Views are derived from cards, but a folder that loses its last card used to
  # leave its generated page behind forever, still linking at files that no
  # longer exist. Only a page refresh itself wrote and nobody touched is removed:
  # any other content hash or any symlink is left alone, so a hand-edited page is
  # never destroyed by a maintenance cycle.
  for rel,row in [(r[0],r[1]) for r in engine.db.execute("SELECT path,file_hash FROM knowledge_views WHERE state='ok'")]:
   if rel in pages:continue
   if not (rel.endswith('/总览.md') or rel in ('首页.md','运行情况.md','使用说明.md')):continue
   if not rel.split('/')[0] in ('项目','知识','资料','随笔','归档'):continue
   target=files.root/rel
   if any(p.is_symlink() for p in [target,*target.parents]):continue
   if not target.is_file():continue
   if hashlib.sha256(target.read_bytes()).hexdigest()!=row:continue
   os.unlink(target)
   with engine.transaction():engine.db.execute('DELETE FROM knowledge_views WHERE path=?',(rel,))
   pruned.append(rel)
 return {'updated':changed,'manual_views_preserved':conflicts,'pruned':pruned}


GUIDE='''# 使用说明

从 [[首页]] 看项目和知识。当前业务以项目原件及明确决定为准；这里只提供接续入口和有出处的判断。

## 资产放在哪里

- **项目**：当前决定、理由、未决事项与原件入口。后续更正尽量修改同一份接续。
- **知识**：按主题维护概念、方法、条件、反例与框架。标注待核的内容可查看，但不作为自动注入的成熟结论。
- **资料**：资料入口及来源说明。原始来源版本还保存在本地运行库，便于回查。
- **随笔**：复盘和观察草稿。
- **归档**：已退出当前使用的版本与历史依据，不按年龄自动删除。

## 平时怎么用

在已接通的 Pi、Codex 等终端正常做项目、给出纠正。接入层提供相关的有效知识，阶段事件合并后才调用模型比较旧卡、提出修改并复核；没有增量价值时不新增卡。长卡放不进当次上下文时只提供位置，Agent 必须读取完整依据才可据此推理。

资料变化与对话走同一条维护队列。已经登记的本地材料修订后会重新比较；尚未登记的文件不会被静默扫入。知识星球等来源仍须通过现有获准采集入口提供，不能把拥有文件解释为允许向任意模型发送。

自动维护需要本地维护进程运行和已配置模型通道。断网、模型不可用或额度不足时保留待处理范围；涉及失效依据的内容暂停自动提供。每端实际接通情况以验收记录为准，安装配置本身不证明生效。

## 修改与纠错

可以在 Obsidian 修改正文或移动卡片。身份标记用于识别原卡，请保留文末隐藏标记。后台发现人工修改后保留新版本；与正在生成的改动冲突时不覆盖。后续涉及人工改过的卡，编译和复核必须读取最新全文，并明确核对人工补充及条件是否保留；旧版本的修改不能覆盖新编辑。不能通过复核的合并会保留待处理。

基础卡修订后，引用其旧版本的框架暂停自动使用，进入同一维护队列重新比较。来源缺失、专家分歧或业务责任未决时保留问题与出处，不伪造一致意见。

## 怎么知道有没有起作用

查看 [[运行情况]]。提供过知识、实际产物中正确应用、相对不用知识是否改善，是三个不同证据等级。不会因为检索命中或模型自称采用，就记成成功。

导出备份包含正文、身份、版本与来源账本。恢复后模型调用默认关闭，需核对目标端的授权与配置再恢复维护。实际订阅扣费以供应商账单为准。
'''
