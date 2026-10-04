# 知识框架试点（knowledge pilot）

`python3 -m tongle.pilot` 是一个**仅合成材料**的隔离试点：把一个自包含的
synthetic case 冻结后，在全新实例里物化“基线卡组”（current）与“基线 + 一张
新建 framework 卡”（candidate）两臂，用真实 Pi 消费者在新会话里逐题回答，再
从原始事件与账本**重算**证据，决定是否在另一个全新目录发布一个 pilot-only
实例。

本模块只做编排与复核，不改动 evaluate / evaluation_lifecycle / engine /
适配器或正式 main；物化、证据校验、依赖关系、发布全部复用真实核心
（`KnowledgeStore.seed/apply`、`Relations`、`KnowledgeFiles`、只读账本检查）。

## 试点 / 生产边界

- **只接收 `material: "synthetic"` 的自包含 case**。不读任何生产 home、Vault 或
  invest 数据；不快照、不 promote 真实材料；没有 grant/origin/authorization
  概念——case 顶层恰好是 `schema,id,version,project,material,sources,cards,
  candidate,review,tasks`，任何路径字段（home/origin/authorization 等）都会
  因未知字段被拒绝。`project` 只允许普通项目标识符（拒绝路径形态）。
- **所有输出都是全新目录**：`materialize`、`run` 与 `publish` 的输出经
  `evaluate._check_out` 检查（拒绝 symlink、既有实例、Vault 内部）。发布没有
  “生产路径参数”，只新建 `out/instance`。
- **无后台、无新表、无生产路由**。隔离实例配置 `model_command: null`，消费时
  禁用全局扩展/技能/上下文文件（复用 `evaluate.consume` 的既有隔离旗标）。
- **合成收益 ≠ 真实业务收益**。真实 case 的接入方式另行讨论，不在本模块。

## case 契约（schema 1）

`schema` 是整数 1；`id`、`version` 是非空字符串（如 `"v1"`）。

- `sources`：1..16 个，`{key, role(user|assistant), text}`；text ≤ 60000 字符。
- `cards`：1..16 张基线卡，`{key, kind(memory|knowledge|framework), topic,
  title, body, evidence, basis}`。
  - `evidence` 每项 `{source, quote}`，quote 必须在所引 source 文本里**逐字**
    出现；至多 12 项；无 basis 的卡 evidence 必须非空，有 basis 的卡可留空
    （与核心 `apply` 语义一致）。
  - `basis` 是本 case 内其他卡的 key 列表（≤12，可空），禁止自引用、重复与
    环；卡按 basis 拓扑序创建。所有初始卡逻辑版本为 1，闭包由整个不可变 case
    冻结——不支持也不模拟实时生产源更新。
- `candidate`：恰好一张新建 framework 卡，字段同 card 加 `reason`；key 不得与
  基线重复；`basis` **非空**且只引用基线卡（允许间接依赖）。内容审核：
  `review = {approved: true, findings: [], proposal_hash, reviewer}`，其中
  `proposal_hash` 必须等于 `evaluate.digest(candidate)`。审核只证明内容准入，
  正式使用前仍须通过效用门禁（assess）。
- `tasks`：1..6 题，`{id, prompt, expect}`；`expect` 是非空的 JSON 标量映射，
  硬判据只做精确类型+值比较（`evaluate.grade`），**不用子串匹配充当语义质量
  评分**；开放式分析可在答案里额外返回但不参与硬判据。case 总长 ≤ 200000。

### 最小示例

```json
{
  "schema": 1,
  "id": "pilot-demo-1",
  "version": "1",
  "project": "pilot-demo",
  "material": "synthetic",
  "sources": [
    {"key": "u1", "role": "user", "text": "我们决定周三发布，回退窗口两小时。"},
    {"key": "a1", "role": "assistant", "text": "已记录：周三发布，回退窗口两小时。"}
  ],
  "cards": [
    {"key": "fact-release", "kind": "knowledge", "topic": "发布",
     "title": "发布时间", "body": "发布定于周三，回退窗口两小时。",
     "evidence": [{"source": "u1", "quote": "周三发布，回退窗口两小时"}],
     "basis": []},
    {"key": "note-release", "kind": "memory", "topic": "发布",
     "title": "确认记录", "body": "助手已确认发布安排。",
     "evidence": [{"source": "a1", "quote": "已记录"}],
     "basis": ["fact-release"]}
  ],
  "candidate": {
    "key": "fw-release", "kind": "framework", "topic": "发布",
    "title": "发布决策框架", "reason": "把发布事实组织成可复用决策框架",
    "body": "发布决策框架：\n1. 时间：周三。\n2. 回退：两小时窗口。",
    "evidence": [{"source": "u1", "quote": "周三发布"}],
    "basis": ["fact-release", "note-release"]
  },
  "review": {"approved": true, "findings": [],
             "proposal_hash": "<见下方计算>", "reviewer": "independent-reviewer"},
  "tasks": [
    {"id": "t1", "prompt": "返回 JSON：发布定在哪天？回退窗口多久？字段 day、window_hours。",
     "expect": {"day": "周三", "window_hours": 2}}
  ]
}
```

计算 `proposal_hash`（在仓库根目录）：

```bash
python3 - <<'EOF'
import json
from tongle.evaluate import digest
case = json.load(open('case.json'))
print(digest(case['candidate']))
EOF
```

## 命令

```bash
# 1) 结构校验（不外发、不触网）
python3 -m tongle.pilot validate --case case.json

# 2) 配对运行（需要用户已授权的真实通道；显式 provider/model/预算/超时）
python3 -m tongle.pilot run --case case.json --out runs/p1 \
    --pi /path/to/pi --provider <provider> --model <model> \
    --max-calls 12 --timeout 90

# 3) 复核重算（不读 report.json 的结论；不触网）
python3 -m tongle.pilot assess --case case.json --run runs/p1

# 4) 发布到全新 pilot-only 实例（仅 disposition == validated_in_scope）
python3 -m tongle.pilot publish --case case.json --run runs/p1 \
    --review review.json --out published/p1
```

- `run` 预算先验：题数 × 2 臂 ≤ `max_calls`（1..12，默认 12）才开始；逐题轮换
  臂顺序。每格独立实例、独立新会话；消费者只拿到 `project` 与 `id/prompt`，
  永远拿不到 `tasks/expect` 或整个 case。每格的原始 events、物化映射、行结果
  都以排他创建方式保存，不可覆盖；infra 失败连同原因记入分母。
- `review.json`（publish 用，独立于 case 内容审核）：

```json
{"approved": true, "findings": [], "reviewer": "second-reviewer",
 "proof_hash": "<assess 输出的 proof_hash>"}
```

- 发布产物：`out/instance/{home,vault}`（候选经核心 apply/publish/回读，保留
  framework 依赖）、`home/pilot.json`（synthetic/pilot_only 标记与
  proof_hash 绑定）、`out/publish-result.json`。新建过程失败时保留现场作为
  证据，本轮不实现通用恢复。**新会话消费由 ROOT 复用该实例 home 与
  `evaluate.consume` 进行，本模块此后不再生成或修改候选。**

## disposition 语义（assess 重算，不信任 report.json）

assess 重新读取冻结 case、manifest、每格原始 events 与账本/文件：缺格、多格、
篡改答案/材料/identity/context、非原生消费者（mock）一律拒绝。核对当前代码哈希、来源版本和可用状态、原始事件哈希，以及行结果与原始模型身份的一致性。合法空注入核对空 offered 回执和同会话的真实采集，不伪造知识送达。基础设施失败保留完整分母、分数为 null，并返回 needs_evidence。结论：

| disposition | 含义 |
| --- | --- |
| `validated_in_scope` | 候选全部硬判据通过；≥1 题 current 失败被修复；且该题候选**完整正文实际进入隐藏上下文**（路径提示不算送达） |
| `regression` | 任一 current 通过的题在候选臂失败 |
| `no_gain` | 全部题平局（含双双通过与双双失败）；严禁借此发布 |
| `needs_evidence` | 有基础设施失败、修复未送达，或修复伴随仍未通过的平局；错误与未知单独列出，不算基线答错 |

返回的 proof 含 `proof_hash`（对除去该字段的完整 proof 做
`evaluate.digest`，任何人可复算绑定）。publish 的独立 review 必须绑定该精确
hash。

## 未覆盖的自然收益（明确不在本次证据范围）

- 开放式回答的语义质量（无子串/相似度打分；模型或人工补充审阅须另留原证据）。
- runner 不独自证明生成者未见题目；需另保留训练/留出分离的分发证据。本批已保存分离证据，但仍是人工构造的一批任务，不能证明普遍生产收益。
- 真实用户收益：合成材料、一次性实例，不等于真实业务改善。
- 候选生成方行为：case 作者提供候选，运行不证明任何编译器/生成器会产生它。
- 多候选排序、长期漂移、成本/时延权衡、并发写入下的发布恢复。

## 限制与预算

sources ≤ 16、cards ≤ 16、tasks ≤ 6、evidence/basis ≤ 12、body/source ≤
60000、case ≤ 200000 字节、max_calls 1..12、timeout 1..180 秒。测试中的 mock
消费者只验证编排，manifest 标记 `native_consumer: false`，永远过不了
assess/publish 门禁。

