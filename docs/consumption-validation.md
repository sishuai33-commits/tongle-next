# 消费验证与受控改进

2026-09-20。一个现有 SQLite 核心、一个按需入口、一份 Case 和不可覆盖的 Run 目录。没有新数据库、调度器、后台 Agent 或第三方依赖。

## 日常核心

Pi 用隐藏上下文中的 receipt、提问前分支锚点和问题摘要关联最终回答。Codex hook 使用本机提供的 session_id + turn_id，在已有适配器状态文件里保存 receipt；旧会话、新会话没有可靠身份、重复钩子出现不同 receipt 时明确 unknown。CC 保留捕获兼容，尚未验证精确关联，标记 legacy_recent。

关联不代表卡片已读、已采用或任务获益。账本区分全文/路径、唯一卡片、关联级别与模型判断。模型判断的误用只进入现有 feedback.candidate，不直接变成新的事实或自动改卡。普通来源捕获和知识维护仍按原流程运行。

```sh
python3 -m tongle.evaluate summary --home /absolute/live-home --project PROJECT_ID
```

此汇总只读数据库，不调用维护、迁移或文件恢复。unknown 不当作零使用。没有对照证据时任务收益保持 unknown。

## 从实际问题到可复验案例

`tasks.json` 是独立确定的任务及精确 JSON 判据：`[{"id":"status","prompt":"…只返回指定 JSON…","expect":{"field":true}}]`。判据与候选生成分开；更改判据会产生新 Case 哈希，旧结果不能用于新判据。当前适合接续事实题，并非通用语义裁判。

```sh
python3 -m tongle.evaluate snapshot --home /absolute/live-home --project PROJECT_ID \
  --cards CARD_ID OTHER_CARD_ID --id CASE_NAME --tasks /absolute/tasks.json \
  --extra-evidence /absolute/selected-quotes.json --out /absolute/new-case.json
```

选择题材由人或协作 Agent 完成，产品不扫描整个知识库挑“容易通过”的题。只抽取指定卡片与其引用短摘录；额外更正也必须明确给出 source_id/version/quote。保留原始 user/assistant/document 身份，不把助手执行报告升级成用户验收。来源全量正文和未选资产不进入外发包。

Case 本地 origin 保存实际卡版本、文件摘要、来源版本与项目目录指纹。新增另一条更正即使未改旧卡，也会使 Case 暂停用于当前发布。运行和发布前重新核对；来源撤回、人工编辑、版本变化、依赖变化都拒绝旧快照。首版明确支持无 basis_cards 的 memory/knowledge 卡；有派生依赖的卡交给现有编译器维护，不静默拆掉依赖。

## 候选、对照与预算

真实材料必须有已明确获准的本地授权文件，字段为 case_hash、provider、model、revoked=false、有限 expires_at 时间戳、approved_by、scope。该文件记录授权，不会自行获得授权。每次请求先用 Pi 已解析的模型身份重新检查授权、撤回和最新来源；别名不能冒充获准通道。此检查发生在发出请求前。合成案例不需要真实材料授权文件。

```sh
python3 -m tongle.evaluate propose --case /absolute/case.json --out /absolute/new-proposal-dir \
  --grant /absolute/grant.json --pi /absolute/pi --provider PROVIDER --model MODEL
```

候选生成复用隔离模型调用和原有改卡数据形状，只看已选卡与出处，拿不到测试答案。独立复核不通过时，可用 `--findings /absolute/findings.json` 回传1至8条有界意见，另开候选目录；保存拒绝与修订依据，不自动循环或放宽评分。附加意见必须在授权包记录精确 `findings_hash`，生成前及原生请求前检查冻结文本；旧授权不能容纳任意新增意见。一次请求，输出上限4096，超时90秒，结果明确 unapproved。独立复核候选后，将 candidate 与 review 写入新的 Case；review 绑定候选精确哈希。人工修改卡还需明确保留检查，发布器不会自动捏造。新 Case 若改变外发内容，应核对原授权是否覆盖并更新其精确范围记录。

```sh
python3 -m tongle.evaluate run --case /absolute/reviewed-case.json \
  --out /absolute/new-run-dir --pi /absolute/pi --provider PROVIDER --model MODEL \
  --grant /absolute/reviewed-case-grant.json --max-calls 9 --timeout 90
```

每题分别运行关闭/当前/候选三组，新建独立 home、Vault 和 Pi 会话。消费者没有系统或文件工具，不加载全局扩展、技能、提示模板或项目上下文；只接收问题和该组上下文。JSON答案从不送给候选生成器或消费者。

整个矩阵先检查预算，每个消费子进程最多一次 provider hook 请求、1200输出上限和指定超时；不会按分数选择性重跑。未知用量不伪装为零；内部网络库重试不可由 provider hook 计数器保证。模型worker也显式限制一次请求，输入过大和次数越界均同步终止；错误记录采用字段白名单。

原始事件、答案、模型身份、材料与代码哈希、用量未知项和每格结果保留。失败尝试不能被成功运行覆盖。报告分开列出完整分母、有效配对及基础设施失败。当前为非盲的已选案例验证，不推导普遍业务收益。

## 发布与中断恢复

```sh
python3 -m tongle.evaluate gate --case /absolute/reviewed-case.json --run /absolute/run
python3 -m tongle.evaluate promote --case /absolute/reviewed-case.json --run /absolute/run \
  --review /absolute/promotion-review.json --out /absolute/new-publication.json
```

`gate` 根据冻结任务清单重读完整矩阵、原始事件、每组实体卡和来源、文件、实际隐藏上下文与精确回执。候选必须纠正至少一个目标失败、通过全部规定回归，且改进题的实际注入文本包含被改卡的完整正文；仅账本写“全文已提供”不够。缺格、重复格、错误材料、未知身份、错误关联、答案被替换都不放行。

独立发布复核包含 approved=true、findings=[]、reviewer 和 gate 返回内容的 proof_hash（同 evaluate.digest 规范）；如有人工编辑，还需核心要求的 human_checks。生产操作仅允许测试候选的身份和版本映射，不能悄悄替换正文。

发布先只读核验再进入现有锁和 KnowledgeStore.apply / KnowledgeFiles.publish。只发布该批卡，回读文件后才报成功。同一证据产生相同批次身份，中断后重复执行不会多升版本；重试也重查撤回和人工修改，不能通过旧批次重放绕过。恢复日志还会检查当前版本、证据和渲染内容，不恢复失效旧正文。

知识回退采用新版本补偿，保留历史；不能整库恢复覆盖后来的用户资料。补偿也要按最新来源重新生成 Case、独立复核和验证，旧内容失效时不能机械恢复。代码回退另行处理，保留新增关联列与已捕获数据。

## 机制怎样改进自身

知识问题和机制问题共用“证据 → 固定案例 → 有范围的候选 → 独立验证 → 限定发布 → 新消费”的路径。知识候选走上述发布器；软件候选走现有 Git/Pi 协作，冻结旧失败和新代码哈希、增加针对失败的回归、独立审查后发布。案例正文与评分结果放在开发证据目录，不自动编译成用户知识。评测器、预算与发布权限不交给同一个自动递归开发循环。

本轮已把原生事件顺序错误、预算钩子异常会被吞掉、恢复失效候选、注入被截断、同文来源身份等实际发现转化为代码和回归。后续改机制以真实失败为入口；没有证据的“更强平台”不作为扩张理由。


## 实战失败怎样进入下一次改进

先区分三个位置：原卡是否缺少有效证据或仍把历史写成当前；实际隐藏上下文是否送达完整卡；消费者是否在拿到完整、准确材料后仍答错。分别保存原卡、回执/真实注入和回答，不能看到低分就直接改知识。任务解释若由用户原话推得，明确标为解释并保留原话；不要为答题把推论伪装成新的用户指令。

模型审查也可能误报。要求阻塞意见给出具体卡、候选原文、来源原文及冲突，按原件核对；角色、版本、逐字出处等能确定检查的部分先由本地验证。无依据的意见不作为新事实写进卡，也不能靠重复投票绕过真问题。保留原拒绝、核对理由和新的独立审查；改动后的候选重新冻结完整矩阵，旧失败不覆盖，不能只重跑失败格或修改期望答案提高分数。

发布后的验证复用现有入口：从实际生产卡重新 `snapshot`，核对新版本及授权摘录范围，以无 candidate 的新 Case 运行 `run`，此时自动采用关闭/当前两组，每题仍是全新实例和会话。不要把发布前的候选再套到旧快照上冒充发布后读取。比较实际答案、关联回执、卡与文件，记录测试时点；选定卡在新会话中有效，不代表整个知识库或所有宿主都已验证。发布版本若再次被修改，重新取样，旧报告保留历史含义。

使用方不需要维持新的人工进度系统：当前决定和进度仍回写项目既有接续文件，原始案例、失败和运行证据留在本次验证目录。没有需要处置的新证据时结束该批，不开启自动递归开发。
