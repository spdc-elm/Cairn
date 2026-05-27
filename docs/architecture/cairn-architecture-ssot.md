# Cairn Architecture SSOT

日期：2026-05-21
状态：当前架构单一事实源
适用范围：Cairn server、dispatcher、worker driver、Web UI、spec、plan、测试与迁移。

## 1. 作用

本文是 Cairn 架构决策的 single source of truth。任何新 spec、plan、实现、迁移、测试、README 或 agent 指令，只要触及核心数据模型、运行链路、Output/Conversation、worker session、UI 投影、兼容策略，都必须以本文为准。

旧文档可作为历史背景，但不得覆盖本文。若旧 spec/plan/code 与本文冲突，应优先修正旧内容或清理旧兼容残余，而不是把系统带回旧路径。

本文主体章节描述“已实现或当前承诺的架构事实”，不得把尚未实现的目标架构写成现状。正在设计但尚未落地的架构变化，只能放入本文的 Pending Architecture Deltas，或写在对应 spec/plan 的 Proposed Architecture Delta 中。

## 1.1 重构与 SSOT 更新顺序

架构重构必须区分三种状态：

- Current SSOT：当前已实现或当前承诺的架构事实。plan 和 implementation 默认以此为基线。
- Proposed Architecture Delta：spec 中提出的目标架构变化，尚未成为事实。
- Pending Architecture Delta：已确认要做、但尚未实现完成的架构变化标记；它解释为什么某个 spec/plan 会有意偏离 Current SSOT。

顺序规则：

1. Specification Mode：先读 Current SSOT。若 spec 目标会改变架构，不得直接把 SSOT 主体改成未来状态；必须在 spec 中写 `Proposed Architecture Delta`，说明当前事实、目标事实、受影响章节、行为不变项、迁移/清理范围和验收口径。
2. Spec 被用户确认后，若后续 plan/implementation agent 需要理解该偏离，应在本文 `Pending Architecture Deltas` 中新增一条标记，链接 spec，并明确状态为 `proposed` 或 `accepted`。该标记不是当前事实。
3. Planning Mode：读取 Current SSOT、Pending Architecture Deltas 和目标 spec。若 spec 的 Proposed Delta 与 Pending 标记一致，不把它当成错误冲突；plan 必须显式安排“实现目标 delta、更新测试、最终合并 SSOT”。
4. Implementation Mode：实现期间可以保留 Pending 标记作为导航；代码、schema、API、UI 与测试通过后，必须把目标架构合并进 SSOT 主体，并移除或归档对应 Pending 标记。
5. Commit 前：若代码已经改变架构，SSOT 主体必须同步为新事实；不得只留下 Pending 标记。只有纯 spec/plan/docs 提案提交，才可以只新增 Proposed/Pending 标记。

## 1.2 Pending Architecture Deltas

当前无 pending architecture delta。

v3.7 执行事件流可靠性改进已合并到主体（§3, §5）。

## 2. 产品心智模型

Cairn 把一次探索表达为 `Fact/Intent DAG`：

- `Fact` 是进入黑板的探索结果节点，面向用户表达“这一步已经得到什么有用结论”。
- `Intent` 是从已有 facts 出发的探索工作订单，描述下一步要探索什么。
- 一次成功结束并被采纳的 exploration intent 默认产出一个 primary result fact。

`Fact` 不是 atomic assertion，不承载 live runtime state。`Intent` 也不承载 worker lease、heartbeat、session、stdout/stderr、cancel/retry 等运行事实。

## 3. 运行层主链路

v3.2 起，运行层唯一主链路是：

```text
Intent / Branch
  -> ExecutionRun
  -> ExecutionEvent
  -> Artifact / EvidenceLink
  -> Fact projection when concluded
```

核心约束：

- `ExecutionRun` 是运行层原子，表示某个 worker 在某个 environment/profile/endpoint/workspace/session 下实际执行一次。
- `ExecutionEvent` 是实时 Output、conversation transcript、tool/message stream、session/status 与语义事件的唯一主数据源。
- `stdout`/`stderr` execution events 是 bounded raw preview/ref/metric，不承诺保存完整 raw stream。
- Terminal status 必须通过 `POST /dispatcher/executions/{execution_id}/finish` 或等价持久化屏障与 terminal/final events 一起提交；dispatcher 主路径不得先丢 final events 再单独把 execution patch 成 succeeded。
- `event_key` 是 immutable idempotency key。同 execution 同 key 同 canonical event 可重放；同 key 不同 event 必须冲突，不得静默吞掉新内容。
- `ExecutionRun` single-writer：leased/running execution 由一个 dispatcher-owned sink writer 写入。`leased_by` 表示 dispatcher owner，`sink_token` 表示该次 sink writer generation。dispatcher append、finish、patch 必须校验 owner + sink token（当 sink_token 由 dispatcher 提供时）。Terminal 后只允许同 key 同 canonical 的 idempotent replay，不允许追加新事件。
- Dispatcher append guard：pending execution 拒绝 dispatcher append（必须先 lease/claim）；terminal execution 拒绝新事件；owner/sink_token mismatch 拒绝。
- Server-internal append（branch initial user event、manual conclude terminal event）不走 dispatcher append guard，直接调用 service 层 `append_execution_events`。
- `Artifact` 与 run log 文件保存大产物与证据，如 report、完整 raw stream/transcript、scan output、screenshot、文件。完整 raw stdout/stderr/worker JSONL 属于 run log/artifact 文件层，不进入主 DB。
- `EvidenceLink` 连接 fact 与 artifact/execution，用于表达证据关系。
- Manual conclude import 是自动 dispatcher conclude 失效后的人工投影路径：用户把外部 resumed worker session 产出的结论 JSON 导入 server，server 校验后写入 primary fact，并用 `produced_by_execution_id`/`EvidenceLink(derived_from)` 记录来源 execution。该路径仍属于 `ExecutionRun -> Fact projection`，不得让 `Intent` 承载 live runtime state。
- Manual conclude 成功后，仍处于 pending/leased/running 的同 intent executions 必须通过等价 terminal barrier 收尾：先写 terminal status execution event，再把 execution 条件式标记为 terminal。不得通过杀进程或恢复旧 dispatcher 路径来完成人工导入。
- Output UI、Conversation UI、fork/resume 历史、实时状态，必须从 `execution_runs` 与 `execution_events` 投影。

禁止从 `Fact`、`Intent`、run log 文件、旧 transcript parser 或旧 provenance table 反向拼出实时 Output/Conversation 主视图。

## 4. Conversation 与 Session

`Branch` 表达 conversation/session 连续线：

- `source` branch 表示原始 execution timeline。
- `resume` branch 表示对源 session 的永久继续，结果进入主 Output 历史。
- `fork` branch 表示从源 session 派生的临时/分支对话，默认不污染主 Output；关闭后前端临时态应消失。
- `fresh_context` 表示无源 session 的新上下文问答。

多轮 fork/resume 必须在同一 branch 下追加 executions，每轮消息创建新的 pending question execution，不复用已有 execution id。第二轮及后续轮次使用上一轮 branch execution 的 session 输出作为下一轮输入。Stale branch execution（lease expired）必须释放 session lock。

`execution_runs.session_action` 是 dispatcher/driver 的明确契约：

- `fresh_context`
- `fork_initial`
- `resume_continue`
- `branch_continue`

`branch_continue` 继续该 branch 最新 successful execution 的 available session；只有没有 branch-local successful session 时才回退到 source execution session。

Execution terminal status 与 remote session availability 是两个不同事实。`failed`/`cancelled` execution 只要 `remote_session_out_status='available'` 且 worker capability 支持对应模式，仍可作为 user-initiated fork/resume source。`worker_runtime_health=unhealthy` 可作为 warning/diagnostic 呈现，但不得阻止创建 fork/resume branch；后续 follow-up execution 若实际无法运行，由 dispatcher healthcheck/finish 记录失败。

## 5. Dispatcher / Server / Driver 边界

Server 负责：

- 持久化 projects、facts、intents、executions、events、branches、artifacts、environment config。
- 持久化 server-wide agent context templates 与 project-scoped agent context snapshots；MVP snapshot kind 为 `agents_md`。
- 暴露 API。
- 执行 schema/migration。
- 不直接运行远端 worker，不解析 backend-specific transcript，不绕过 dispatcher 执行 Q&A。

Dispatcher 负责：

- lease execution。
- 选择 worker driver 与 environment。
- 启动/取消/心跳/收尾 worker process。
- 在 worker process 启动前，从 server 获取 project agent context snapshot，并在 workspace 内 materialize Cairn-managed `AGENTS.md`。
- 将 worker stream 分发到 run log/artifact 文件、semantic projector 与 DB raw preview/ref sink。
- 将 message/tool/session/status 等语义事件，以及 bounded stdout/stderr preview/ref/metric 写入 `ExecutionEvent`。
- 通过 bounded EventSink 写入 events：timeout/backpressure 必须 retry、暴露 diagnostic，且队列不得无限增长。EventSink 持有 `sink_token` 用于标识单一 writer generation。
- EventSink 失败语义：409 event_key_conflict 必须 fail-fast（清空队列、标记 fatal、不继续滚雪球）；422 too_long 由 batch slicing（<= 250）防止；queue overflow 产生有界 bounded diagnostic；每类失败使用明确 `failure_kind` 分类。
- EventSink raw storage metric 使用 `execution_id:raw-storage:<stream>:final` 作为 idempotent key，确保每 stream 最多一个 final metric event。
- 通过 finish contract 提交 terminal events、session output、returncode、error 与 terminal status。finish 失败时不得把 execution 标记为 succeeded；最后手段只能暴露 failed diagnostic。

Worker driver 负责：

- 将具体 CLI/API 输出转成统一 execution events。
- 实现 session resume/fork/fresh 语义。
- 暴露 backend context-file capability 开关，例如 Pi 是否允许读取 workspace `AGENTS.md`；driver 不拥有 agent context 内容来源。
- 上报能力与诊断信息。

## 6. Worker 能力与健康

`worker_inventory.question_capability_json` 仍是当前主路径的一部分，用于声明 worker 是否支持 fork/resume/fresh_context。它不是旧兼容残余。

`worker_runtime_health` 表达当前 environment/worker/profile/endpoint 的可用性。它服务于调度和 UI 提示，不替代 execution ledger。

## 7. UI 投影规则

Web UI 应只把以下内容作为 Output/Conversation 主路径：

- `GET /projects/{project_id}/executions...`
- `GET /projects/{project_id}/executions/{execution_id}/events...`
- branch/execution 相关 v3.2 API

UI 不得调用旧 `/questions` 或 `/runs/*/transcript` 作为 Output/Conversation 主路径。
UI Conversation 主路径不得解析 backend-specific stdout JSONL 生成语义 message/tool/session；raw stdout/stderr preview/ref 只属于 Raw/Debug 呈现，完整 raw 需通过 run log/artifact ref 定位。
Execution events 与 branch timeline 必须使用 endpoint-scoped cursor 完整读取；跨 execution/branch cursor 必须被 server 拒绝，前端不得依赖 project cursor 混用跳读。
Intent active/running UI state 必须来自 active execution projection，例如 `active_execution_id` 与 `runtime_status in ('pending','leased','running')`。`Intent.worker`、`worker_name` 或 latest terminal worker 只能作为历史/显示兼容字段，不得驱动 Request Conclude、heartbeat、release 等 active-worker action。

实时展示规则：

- streaming partial 应按 execution、role、message/content index 或稳定 event key 压缩。
- terminal assistant message 必须覆盖 running partial。
- 不同 assistant 回合不得错误合并。
- execution terminal status 不得让前端漏掉最后 assistant message。
- fork 的临时显示不得混入主 source output。
- resume 的结果必须进入主 output history。

## 8. 迁移与兼容策略

本项目当前不承诺旧版本数据库/API 的长期兼容。默认策略：

- fresh schema 只包含当前架构需要的表。
- migration 只服务当前仍支持的升级路径和当前测试需要。
- 不为已废弃路径新增 backfill、router guard、兼容 facade。
- 旧 raw stdout/stderr blob 的瘦身由显式 maintenance command 执行；普通 migration 不自动裁剪历史 raw blob，也不为旧 raw blob 引入长期兼容分支。
- 若发现旧兼容代码影响理解或可能被误用，应优先删除。

明确废弃且不得复活为主路径：

- `run_provenance` table/model/service/API。
- `/projects/{project_id}/runs/*` router。
- `/runs/*/transcript` API。
- `/questions` / `question_jobs` / `question_threads` runtime。
- `server/transcripts/*` backend-specific parser 作为 server runtime path。
- server 直接执行远端 worker 的旧 Q&A executor。

旧文档中对上述路径的描述只可作为历史记录或反例。新计划若触及这些路径，应主动包含清理或替换步骤。

## 9. 架构变更流程

任何架构级改动必须更新本文或明确说明本文无需变更：

- 新增/删除核心表、API、router、dispatcher contract、worker session 行为。
- 改变 Fact/Intent/Execution/Branch/Artifact 边界。
- 改变 Output/Conversation 实时投影路径。
- 引入兼容层、迁移策略或旧路径恢复。
- 修改 spec/plan skill、AGENTS、README 中的架构指导。

提交前检查：

1. 搜索是否触及架构关键字：`ExecutionRun`、`ExecutionEvent`、`Branch`、`Artifact`、`Fact`、`Intent`、`run_provenance`、`/runs`、`/questions`、`transcript`。
2. 若触及，检查本文是否仍准确。
3. 若本文不准确，先更新本文，再更新相关 spec/plan/README/AGENTS。
4. 若决定不更新本文，在提交说明或 PR/最终回复中说明原因。

## 10. 当前清理方向

旧兼容残余的默认处理方式：

- 运行路径未用、测试未需要、且会暗示旧主链路仍有效的代码，直接删除。
- 历史文档保留时，应在新 SSOT/spec/plan 中明确其历史属性，避免 agent 回归。
- 对仍有实质用途的字段或测试，应重命名或补注释，说明其当前职责。例如 `question_capability_json` 是 worker 能力声明，不是旧 question runtime。
