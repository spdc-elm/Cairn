# Interactive Worker Q&A and Transcript Panel Execution Plan

依据：`docs/specs/v3-interactive-worker-qna-requirements.md`

日期：2026-05-19

状态：待执行计划。本文把 v3 拆为可独立验收的工程阶段：先建立 run/session provenance 的事实源，再做 transcript normalization，最后接入临时 Q&A 与 UI。

## 0. 目标边界

### 必须达成

- 每次 worker execution 都有 DB 持久化的 `RunProvenance`，DB 是 run/session provenance 的 source of truth。
- `run_log_id` 与 legacy/report 用的 `report_run_id` 明确分离；不再裸用 `run_id` 指两个概念。
- 新 produced fact 记录 `metadata.provenance.producing_run_log_id`；Fact 不直接保存 `remote_session_id`。
- 旧数据没有 DB provenance / remote session 时，API 与 UI 明确显示 `missing`，只允许 `fresh_context`，不从旧 stdout/JSONL 考古恢复 session。
- run JSONL 继续作为 raw transcript artifact；API 不从 JSONL 推断 session provenance。
- transcript parser 从完整 raw run JSONL 重建 stdout/stderr，再按 normalized event 限制返回。
- Codex / Pi / Claude Code 都有 normalized transcript parser fixture，Pi 高频 delta 必须 coalesce。
- Detail panel 与 worker output 分离；新增 `Conversation` / `Output` 面板展示 message、tool、raw。
- 已结束 fact / intent / run 可发起临时 Q&A；running intent 首版只显示 live transcript，不提供 Ask。
- `fork` / `fresh_context` 问答在 Cairn 侧短期存在，关闭后销毁，并给用户提示远程 worker 可能自留日志。
- `resume` 问答必须显式确认、写 run log、进入 run provenance，并对同一 remote session 做并发保护。
- Q&A 结果不会自动写黑板；promotion 为 hint/fact/intent 必须显式触发并保存足够 provenance。

### 明确不做

- 不做 running intent fork / running intent 并行追问。
- 不把 fork/fresh_context QuestionThread transcript 持久化到 DB。
- 不把所有 raw stdout 长期塞进 DB。
- 不在首版引入 assistant-ui、CopilotKit 等大 UI 框架作为硬依赖。
- 不做默认只读工具隔离；只通过 prompt/UI 允许用户声明只读约束。
- 不完整实现 ACP；只让 normalized event model 尽量可映射。
- 不从旧 Codex/Pi/Claude stdout 反推 remote session。

### 已核对的当前代码事实

- `RunLogWriter` 在 `cairn/src/cairn/dispatcher/runtime/run_logs.py` 内部生成 `run_id`，调用方当前拿不到。
- `run_worker_process(...)` 在 `cairn/src/cairn/dispatcher/tasks/common.py` 只返回 `ProcessResult`。
- `explore.py` / `bootstrap.py` / `reason.py` 都是在 process 返回后调用 `driver.extract_session(...)`。
- `RunLogWriter.finish()` 后 `_closed=True`，当前无法在 session 解析后追加 `session_resolved`。
- `runs.py` 当前只读 filesystem JSONL，`RunLogDetail` 用最后 `MAX_EVENTS=600` 与 `MAX_TEXT_CHARS=120000` 截断。
- UI 当前在 `server/static/index.html` 的 intent detail 内嵌 `Worker Output` `<pre>`，调用 `/projects/{project_id}/runs/latest?intent_id=...`。
- `metadata_for_report(...)` 当前写 `metadata.run_id`，它实际是报告路径后缀，不是 Cairn run log id。
- 新的 versioned migration runner 已存在：`server/migrations/0001_initial.py`、`0002_current_additive_schema.py`、`runner.py`。

## Phase 0: Contract First 与测试基线

### 目的

先冻结跨模块协议，避免后面把 run log 文件、DB provenance、fact metadata、question runtime 混成一团。

### 步骤

1. 建立测试文件骨架。

```text
cairn/tests/server/test_v3_run_provenance_schema.py
cairn/tests/server/test_v3_run_provenance_api.py
cairn/tests/dispatcher/test_v3_run_provenance_execution.py
cairn/tests/dispatcher/test_v3_transcript_parsers.py
cairn/tests/server/test_v3_questions_api.py
cairn/tests/dispatcher/test_v3_question_capabilities.py
```

2. 建立 fixture 目录。

```text
cairn/tests/fixtures/run_logs/codex_conclude.jsonl
cairn/tests/fixtures/run_logs/pi_large_stream.jsonl
cairn/tests/fixtures/run_logs/claude_stream_json.jsonl
```

fixture 应来自真实样例的脱敏最小切片，保留外层 chunk split 特性。Pi fixture 要至少覆盖：

- `session`
- `message_start/message_update/message_end`
- `tool_execution_start/update/end`
- 多个 outer `stream` chunk 切断同一 inner JSON 行的情况

3. 先写失败测试，覆盖：

- fresh DB 有 `run_provenance` 或等价表。
- old DB migration 后旧 fact 仍可读，旧 `metadata.run_id` 不被当作 `run_log_id`。
- `run_worker_process` 返回对象包含 `run_log_id`。
- session 解析后 DB provenance 更新为 `available` 或 `missing`。
- transcript parser 先全量读 raw JSONL，再 limit normalized output。
- `runs/latest/transcript` 不复用当前截断后的 `RunLogDetail.stdout`。
- running intent 不出现 Q&A action。

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/server/test_v3_run_provenance_schema.py -q
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_v3_transcript_parsers.py -q
```

### 验收

- 初始 RED 失败点指向缺失 schema/model/API/parser，而不是 fixture 错误。
- 测试命名明确区分 `run_log_id`、`report_run_id`、`remote_session_id`。
- 所有后续 phase 都能引用这些 contract 测试扩展。

### 审查点

- 不允许在测试里从 legacy stdout 推断 session。
- 不允许把 `Fact.metadata.run_id` 当作 `run_log_id`。

## Phase 1: DB Schema 与 Provenance Service

### 目的

让 DB 成为 run/session provenance 真相源。run JSONL 只承载 raw/transcript artifact。

### 范围

文件：

```text
cairn/src/cairn/server/schema.py
cairn/src/cairn/server/migrations/0003_run_provenance.py
cairn/src/cairn/server/models.py
cairn/src/cairn/server/services.py
```

### 步骤

1. 新增 migration `0003_run_provenance.py`。

推荐表名：`run_provenance`。

```text
run_log_id TEXT PRIMARY KEY
project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE
intent_id TEXT
task_type TEXT NOT NULL
phase TEXT NOT NULL
worker_name TEXT NOT NULL
worker_type TEXT
environment_id TEXT
environment_backend TEXT
environment_target TEXT
workspace TEXT
model_profile_id TEXT
endpoint_id TEXT
timeout_seconds INTEGER
report_path TEXT
report_run_id TEXT
remote_session_id TEXT
remote_session_kind TEXT
remote_session_status TEXT NOT NULL DEFAULT 'unresolved'
remote_session_capture_method TEXT
parent_run_log_id TEXT
parent_remote_session_id TEXT
question_mode TEXT
question_anchor_type TEXT
question_anchor_id TEXT
source_run_log_id TEXT
source_remote_session_id TEXT
session_effect TEXT
started_at TEXT NOT NULL
finished_at TEXT
returncode INTEGER
timed_out INTEGER
cancelled INTEGER
cancel_reason TEXT
metadata_json TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

索引：

```text
(project_id, intent_id, started_at DESC)
(project_id, task_type, started_at DESC)
(project_id, source_run_log_id)
(remote_session_kind, remote_session_id)
```

2. 同步更新 `SCHEMA`，保证 fresh DB shortcut 与 migration 一致。

3. 在 `server/models.py` 增加 Pydantic model。

```text
RemoteSessionProvenance
RunProvenance
RunProvenanceUpsert
RunProvenancePatch
AnchorResolution
```

4. 在 `server/services.py` 新增 service helpers。

```text
create_run_provenance(conn, ...)
finish_run_provenance(conn, ...)
update_run_remote_session(conn, ...)
get_run_provenance_or_none(conn, project_id, run_log_id)
list_run_provenance(conn, project_id, intent_id=None, limit=...)
resolve_anchor(conn, project_id, anchor_type, anchor_id, selected_run_log_id=None)
```

5. `resolve_anchor` 规则固定：

- fact 新 metadata 命中 `metadata.provenance.producing_run_log_id` -> `exact`。
- legacy fact 只有 `metadata.run_id` / `report_path` -> `missing`，reason 写 `legacy_report_run_id_only`。
- intent 默认选最新 successful run；也允许显式 `run_log_id`。
- run 直接查 DB provenance；缺 DB row -> `missing`。

6. 对旧 fact metadata 做兼容读取，不批量回填：

```json
{
  "provenance": {
    "legacy_report_run_id": "run_xxx",
    "report_path": "...",
    "producing_run_log_id": null
  }
}
```

兼容读取可以在服务层输出中合成；不要迁移时猜 session。

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/server/test_v3_run_provenance_schema.py -q
```

覆盖：

- fresh schema / migrated schema 都包含表与索引。
- migration 幂等。
- old DB 迁移后旧 fact metadata 不变，但 anchor resolution 为 `missing`。
- 新 fact metadata 可以解析出 exact producing run。
- latest successful run 排除 timeout / cancelled / nonzero returncode。

### 验收

- DB status 显示 `0003_run_provenance` 已可用。
- 所有 provenance 查询不读 run JSONL。
- 旧数据缺 session 时结果稳定为 `missing`，无隐藏 backfill。

### 审查点

- `run_provenance.remote_session_status` 必须是 `available|missing|unresolved` 之一。
- `report_run_id` 只能来自 report path 后缀；不能参与 session 恢复。

## Phase 2: Dispatcher Run Lifecycle 与 Fact Provenance

### 目的

让 dispatcher 在真实执行路径里写入 DB provenance，并把 `run_log_id` 传回调用方。

### 范围

文件：

```text
cairn/src/cairn/dispatcher/runtime/run_logs.py
cairn/src/cairn/dispatcher/runtime/process.py
cairn/src/cairn/dispatcher/tasks/common.py
cairn/src/cairn/dispatcher/tasks/explore.py
cairn/src/cairn/dispatcher/tasks/bootstrap.py
cairn/src/cairn/dispatcher/tasks/reason.py
cairn/src/cairn/dispatcher/tasks/reports.py
cairn/src/cairn/dispatcher/protocol/client.py
cairn/src/cairn/server/routers/intents.py
```

### 步骤

1. 新增 execution result dataclass。

```text
WorkerProcessRun
- result: ProcessResult
- run_log_id: str | None
- run_log_path: str | None
```

`run_worker_process(...)` 返回 `WorkerProcessRun`，healthcheck 保持 `ProcessResult` 不变。

2. 在 `RunLogWriter` 增加可控 lifecycle：

- `run_id` 仍默认内部生成。
- 支持 `write_event("session_resolved", ...)` 在 finish 前写入。
- `finish(...)` 写 DB finish 状态前后顺序要固定。

推荐不做 post-finish append；让 `run_worker_process` 在 `finish()` 前调用一个可选 `finalize_metadata` callback 或让调用方在返回后只更新 DB，不追加 JSONL。首版可只更新 DB，`session_resolved` 镜像列为可选。

3. 给 `run_worker_process(...)` 增加可注入的 provenance recorder，而不是让 common layer 直接 import server DB。

推荐 contract：

```text
RunProvenanceRecorder
- start_run(run_log_id, metadata) -> None
- finish_run(run_log_id, result) -> None
```

实现上可以由 `CairnClient` 调 server API，也可以在测试中用 fake recorder。`run_worker_process(...)` 负责创建 `RunLogWriter` 并拿到 `run_log_id`，随后调用 recorder 写 DB row。

4. 在 run 启动时创建 DB `run_provenance` row。

DB 所需字段来自：

- `project_id`
- `intent_id`
- `task_type`
- `phase`
- `worker.name`
- `worker.type`
- `handle.workspace`
- `environment.id/backend/target`
- `extra_metadata.report_path/report_run_id/control_state_at_start`

5. 进程结束时写 finish 状态：

- `returncode`
- `timed_out`
- `cancelled`
- `cancel_reason`
- `finished_at`

6. 在 `explore.py` / `bootstrap.py` / `reason.py` 中适配新返回值。

模式：

```text
run = run_worker_process(...)
result = run.result
session = driver.extract_session(session, result.stdout, result.stderr)
client.update_run_session(project_id, run.run_log_id, ...)
```

7. 增加 server-internal API。

因为 dispatcher 当前通过 `CairnClient` 调 server API，provenance start/finish/session update 都走 HTTP endpoint；不要在 dispatcher 里直接打开 server DB。

建议新增：

```text
POST /projects/{project_id}/runs/provenance
PATCH /projects/{project_id}/runs/{run_log_id}/provenance
POST /projects/{project_id}/runs/{run_log_id}/provenance/session
```

输入：

```json
{
  "worker": "pi-GPT5.4",
  "remote_session": {
    "id": "...",
    "kind": "pi_session",
    "status": "available",
    "capture_method": "stdout_event"
  }
}
```

解析不到 session 时也要写：

```json
{"remote_session": {"id": null, "kind": null, "status": "missing", "capture_method": "unavailable"}}
```

8. Adapter 提供 session kind / capture method。

最小接口：

```text
session_kind() -> str
session_capture_method(session_before, stdout, stderr) -> str
```

或者在 `extract_session_provenance(...)` 一次返回：

```text
RemoteSessionResult(id, kind, status, capture_method)
```

9. 更新 fact metadata。

`metadata_for_report(...)` 改为：

```json
{
  "report_path": "...",
  "report_run_id": "run_30a439269490",
  "worker": "codex-GPT5.5",
  "intent_id": "i021",
  "provenance": {
    "producing_intent_id": "i021",
    "producing_run_log_id": "run_15941...",
    "report_run_id": "run_30a439269490",
    "report_path": "...",
    "worker_name": "codex-GPT5.5"
  }
}
```

保留 legacy `run_id` 只读兼容，但新写入不再使用裸 `run_id`。

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_v3_run_provenance_execution.py -q
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_reports.py -q
env PYTHONPATH=src python -m pytest cairn/tests/server/test_command_blackboard_v2_api.py -q
```

覆盖：

- fake environment 执行后 `WorkerProcessRun.run_log_id` 非空。
- run start/finish 写 DB provenance。
- Codex/Pi/Claude session 解析成功后状态为 `available`。
- 解析失败写 `missing`。
- `metadata_for_report` 新写 `provenance.producing_run_log_id`，旧测试按新字段更新。
- conclude fallback 的 produced fact 指向 conclude run 的 `run_log_id`，不是 execute run 或 report run id。

### 验收

- 新执行产生的 fact 能 exact resolve 到 producing run。
- DB provenance 与 filesystem JSONL 的 run id 对齐。
- 旧 v2 行为测试仍通过。

### 审查点

- 不能在 dispatcher 里直接打开 server DB，除非当前架构已有明确同进程约束。优先通过 server API。
- session 更新失败要记录 warning，但不能让已成功执行的 fact 因 telemetry 写入失败而丢失；此时 provenance session 状态保持 `unresolved` 并在 UI 显示不可追问。

## Phase 3: Normalized Transcript Parser

### 目的

把不同 worker 的 stdout/stderr 转成统一事件流，替代 UI 当前粗糙 `<pre>`。

### 范围

新增：

```text
cairn/src/cairn/server/transcripts/__init__.py
cairn/src/cairn/server/transcripts/models.py
cairn/src/cairn/server/transcripts/run_log_reader.py
cairn/src/cairn/server/transcripts/parsers/base.py
cairn/src/cairn/server/transcripts/parsers/codex.py
cairn/src/cairn/server/transcripts/parsers/pi.py
cairn/src/cairn/server/transcripts/parsers/claudecode.py
cairn/src/cairn/server/transcripts/parsers/raw.py
```

可选复用 dispatcher adapter 的 parsing 逻辑，但不要让 server import Docker/runtime-heavy dispatcher modules。

### 步骤

1. 定义 `TranscriptEvent` 与 response model。

```text
TranscriptEvent
- id
- ts
- seq
- source
- kind
- role
- title
- text
- tool_name
- tool_args_preview
- status
- raw
- collapsed

TranscriptResponse
- run_log_id
- project_id
- provenance
- events
- events_omitted_before
- large_event_collapsed
- parser
- raw_available
```

2. `run_log_reader` 全量读取 raw run JSONL。

规则：

- `_read_records_full(path)` 不使用 `MAX_EVENTS`。
- 按 outer `seq` 拼接 stdout/stderr。
- 不假设 outer `stream.text` 与 inner JSONL 行边界对齐。
- malformed outer line 生成 parser `raw/error` 事件或计数，不中断。

3. parser registry 选择策略：

- 优先用 DB provenance `worker_type`。
- 缺 DB provenance 时尝试 run summary `worker` name 的已知前缀，只用于 transcript rendering，不用于 session provenance。
- 仍无法判断时走 raw parser。

4. Codex parser：

- `thread.started` -> `run_started` / raw metadata。
- `turn.started/completed` -> status/thinking 或 raw status。
- `item.completed agent_message` -> assistant message。
- `item.started/completed command_execution` -> tool_call/tool_result。
- unknown item -> raw。

5. Pi parser：

- `session` -> run/session marker。
- `message_start/update/end` coalesce by message identity / content index。
- `tool_execution_start/update/end` coalesce by tool execution id。
- 高频 delta 不直接逐条输出。
- 最终 event 数应随 message/tool 节点增长，而不是随 delta chunk 增长。

6. Claude Code parser：

- `system/init`、`assistant`、`user`、`tool_use/tool_result`、`result` 做 best effort 映射。
- unknown stream-json event 进 raw。

7. 大事件折叠：

- `limit_events` 默认 200。
- 单条 text/tool result 超阈值时保留摘要、长度、展开标记。
- `events_omitted_before` 只表示 normalized event 级省略。

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_v3_transcript_parsers.py -q
```

覆盖：

- Codex fixture 产生 assistant message 与 command tool events。
- Pi fixture 合并 delta，事件数远小于 inner raw event 数。
- Pi `session` 不因 API limit 丢失，因为 parser 输入全量。
- Claude fixture 产生 assistant/result events。
- malformed inner line 进入 raw，不丢失后续事件。
- limit 只作用在 normalized events。

### 验收

- 对 proj_005 Codex 样例能看到 thread、tool command、assistant JSON result。
- 对 proj_007 Pi 样例不会输出 7000+ `message_update` 节点。
- Parser failure 时 Raw tab 仍可看原始输出。

### 审查点

- server transcript parser 不应 import `docker`。
- secret redaction 只依赖已有 run log 写入层；parser 不二次猜 secret。

## Phase 4: Runs API 2.0 与 Anchor Resolution API

### 目的

给 UI/Q&A 提供稳定 API：run summary 合并 DB provenance，transcript 有结构，anchor resolution 有明确失败原因。

### 范围

文件：

```text
cairn/src/cairn/server/routers/runs.py
cairn/src/cairn/server/routers/__init__.py
cairn/src/cairn/server/app.py
cairn/src/cairn/server/models.py
```

### 步骤

1. 扩展现有 run summary。

`RunLogSummary` 增加：

```text
run_log_id
provenance
has_provenance
remote_session_status
report_run_id
```

为了兼容旧 UI，短期保留 `run_id` alias，语义标注为 run log id。

2. 新增 endpoints。

```text
GET /projects/{project_id}/runs/{run_log_id}/provenance
GET /projects/{project_id}/runs/{run_log_id}/transcript?limit_events=200&view=conversation|tools|raw|all
GET /projects/{project_id}/runs/latest/transcript?intent_id=...&limit_events=200
GET /projects/{project_id}/anchors/resolve?anchor_type=fact&anchor_id=f001&run_log_id=...
```

3. 调整 `/projects/{project_id}/runs`：

- 优先从 DB provenance 列表读。
- 对没有 DB provenance 但存在 JSONL 的 legacy run，可作为 `has_provenance=false` 展示，但 session status 必须 `missing`。
- filesystem JSONL 仍用于 started/finished 兜底，不覆盖 DB provenance。

4. 保留 `/runs/{run_id}` 旧 detail endpoint。

短期内不删除 `combined/stdout/stderr`，但前端新面板使用 transcript endpoint。

5. Anchor resolution endpoint 返回：

```json
{
  "anchor_type": "fact",
  "anchor_id": "f021",
  "source_run_log_id": "run_...",
  "status": "exact",
  "reason": null,
  "provenance": {...},
  "available_modes": ["fork", "resume", "fresh_context"]
}
```

旧数据：

```json
{
  "status": "missing",
  "reason": "legacy_report_run_id_only",
  "available_modes": ["fresh_context"]
}
```

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/server/test_v3_run_provenance_api.py -q
```

覆盖：

- list runs 返回 DB provenance。
- latest successful 排除失败 run。
- legacy JSONL 存在但 DB provenance 缺失时 `has_provenance=false`、session `missing`。
- transcript endpoint 读取 full raw JSONL 并返回 normalized events。
- anchor resolution 对 new fact exact，对 legacy fact missing。

### 验收

- UI 可以不读 old `combined` 也能显示 conversation。
- Q&A 可以通过 anchor resolution 判断是否允许 fork/resume。
- 旧 run log 仍可 raw 查看。

### 审查点

- `runs.py` 不应在 provenance endpoint 中扫描巨大 stdout 找 session。
- `limit_events` 必须有上限，防止一次拉爆 Pi 大日志。

## Phase 5: Adapter Capability 与 Question Execution Contract

### 目的

让 Codex/Pi/Claude 的差异被 adapter 吸收，server/UI 不按 worker type 猜能力。

### 范围

文件：

```text
cairn/src/cairn/dispatcher/workers/base.py
cairn/src/cairn/dispatcher/workers/adapters/codex.py
cairn/src/cairn/dispatcher/workers/adapters/pi.py
cairn/src/cairn/dispatcher/workers/adapters/claudecode.py
cairn/src/cairn/dispatcher/workers/registry.py
```

### 步骤

1. 新增 contract dataclass。

```text
QuestionCapability
- can_resume_session: bool
- can_fork_session: bool
- can_use_tools: bool
- can_stream_events: bool
- resume_mutates_source: bool
- fork_creates_remote_log: bool
- question_modes: tuple[str, ...]
```

2. `WorkerDriver` 新增方法：

```text
question_capability(self, worker) -> QuestionCapability
build_question(self, worker, *, mode, prompt, source_session) -> DriverResult
extract_session_provenance(self, prepared_session, stdout, stderr) -> RemoteSessionResult
```

3. Codex 首版建议：

- `resume`: yes，基于 `codex exec resume ... --json`。
- `fork`: no，除非本地 CLI 已核实有稳定 fork 命令；不要凭外部推断。
- `fresh_context`: yes。

4. Pi 首版建议：

- `resume`: yes，基于 `--session <id>`。
- `fork`: no，除非 Pi CLI 当前版本有明确 fork 参数。
- `fresh_context`: yes。

5. Claude Code 首版建议：

- `resume`: yes，基于 `-r <session>`。
- `fork`: 可先声明 no，直到本地 CLI/测试确认 `--fork-session` 与输出 session id 行为；若确认则开启。
- `fresh_context`: yes。

6. 所有 `resume_mutates_source=true` 的 backend 都必须让 UI/server 走确认。

7. `fresh_context` 的 build 语义：

- 不传 source session。
- Prompt 由 Cairn 拼接 anchor summary、fact/report/transcript 摘要与用户问题。
- 仍允许 worker 在同 workspace 中用工具查证。

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_v3_question_capabilities.py -q
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_worker_driver_capabilities.py -q
```

覆盖：

- 三个真实 driver 都声明 capability。
- `build_question(mode="resume")` 对 Codex/Pi/Claude 生成对应 resume argv。
- unsupported mode 抛明确异常。
- `fresh_context` 不要求 source session。
- session provenance 提取返回 kind/status/capture_method。

### 验收

- UI/server 只读 capability，不硬编码 `if worker_type == ...`。
- 不支持 fork 的 worker 不显示 fork action。

### 审查点

- 不要为了“统一”把 fork 模拟成 resume。
- `question_session` 不能在 resume 模式伪造新 session。

## Phase 6: Question Runtime 与 API

### 目的

实现临时多轮 Q&A，严格区分 fork/fresh_context 的短期性与 resume 的持久 run log。

### 范围

新增：

```text
cairn/src/cairn/server/questions/models.py
cairn/src/cairn/server/questions/manager.py
cairn/src/cairn/server/questions/context.py
cairn/src/cairn/server/routers/questions.py
```

可能修改：

```text
cairn/src/cairn/server/app.py
cairn/src/cairn/dispatcher/tasks/common.py
cairn/src/cairn/dispatcher/protocol/client.py
```

### 步骤

1. 新增 in-memory `QuestionManager`。

管理：

- `QuestionThread`
- transient transcript
- TTL / close
- resume locks by `(remote_session_kind, remote_session_id)`

首版可进程内存储。server 重启后返回 `404` 或 `410`。

2. 新增 API。

```text
POST /projects/{project_id}/questions
GET /projects/{project_id}/questions/{question_id}
POST /projects/{project_id}/questions/{question_id}/messages
POST /projects/{project_id}/questions/{question_id}/close
POST /projects/{project_id}/questions/{question_id}/promote
```

3. `POST /questions` 流程：

- resolve anchor。
- 查询 source run provenance。
- 查询 worker capability。
- 选择 mode：
  - `auto`: fork > resume if `allow_resume_without_fork=true` > fresh_context
  - explicit mode: 不满足时 409，给 reason。
- running intent: 只允许 transcript；创建 question 返回 409 `running_intent_question_out_of_scope`。
- fork/fresh_context: 创建短期 QuestionThread。
- resume: 要求 `confirm_resume=true`，抢占 resume lock，创建 question run。

4. 多轮消息：

- 每次 user message 追加到 transient transcript。
- 调用 adapter `build_question(...)` 运行 worker。
- 解析 worker output 为 normalized question events。
- 返回 assistant/tool events。

5. Context builder：

`fresh_context` prompt 包含：

- anchor summary。
- fact description / intent description。
- report path 与可读摘要。
- source run transcript 摘要。
- 用户问题。
- 明确“不要修改黑板；写操作需用户明确要求”。

6. Resume run provenance：

resume 模式的 run 必须写：

```text
task_type="question"
question_mode="resume"
question_anchor_type
question_anchor_id
source_run_log_id
source_remote_session_id
session_effect="continued"
```

7. Close 语义：

- fork/fresh_context：删除 Cairn 侧 transcript，后续 GET 返回 `410 gone`。
- resume：QuestionThread 可关闭，但 run log/provenance 保留。

8. Promote 语义：

- `hint`: 调现有 hints API/service。
- `fact`: 需要新增人工 fact 创建 endpoint 或复用现有安全路径。
- `intent`: 调 create intent。
- metadata 必须保存 anchor、source run、mode、session effect、source session status、answer summary；不能只保存 `question_thread_id`。

### 测试

```bash
env PYTHONPATH=src python -m pytest cairn/tests/server/test_v3_questions_api.py -q
```

覆盖：

- exact anchor + capability resume + 未确认 -> 409 confirmation required。
- exact anchor + resume confirmed -> 创建 question run provenance。
- missing anchor -> fresh_context。
- legacy fact -> fresh_context 且 reason 清楚。
- running intent -> 409 out of scope。
- 同一 source session 并发 resume -> 第二个 409 或 queued 状态。
- close fork/fresh_context 后 GET 返回 410。
- promotion metadata 不只含 question_thread_id。

### 验收

- 对已结束 intent 可完成至少一轮 fresh/resume 问答。
- 对旧 fact 显示 missing/fresh_context，不误称恢复原 session。
- resume 模式写入 run log，timeline 可看见。

### 审查点

- 临时 transcript 不落 DB。
- resume lock 必须在失败/取消后释放。
- 后端错误要能区分无 session、adapter 不支持、workspace 不可达、worker 执行失败。

## Phase 7: UI Conversation Panel 与 Q&A UX

### 目的

让用户舒服地看 transcript，并在同一面板中发起历史对象追问。

### 范围

文件：

```text
cairn/src/cairn/server/static/index.html
```

必要时拆分仍保持单文件 app 现状，避免引入 bundler。

### 步骤

1. Side panel tab 调整：

```text
Detail
Conversation
Hints
Log
Settings
```

`Detail` 只保留对象元信息与主要动作，不再放大块 `<pre>`。

2. 新增 state：

```text
conversation: {
  anchor,
  runs,
  selectedRunLogId,
  transcript,
  loading,
  error,
  view: "conversation" | "tools" | "raw",
  limitEvents: 200
}

question: {
  activeThread,
  messages,
  composerText,
  mode,
  warning,
  loading,
  error
}
```

3. 选择 fact/intent/run 时：

- fact -> resolve anchor -> load producing run transcript if exact。
- intent -> list runs -> load latest selected run transcript。
- run/timeline click -> load exact run transcript。
- legacy/missing -> show missing state 与 `fresh_context` option。

4. Transcript viewer：

- role message 分组。
- tool call 可折叠。
- raw tab 显示 raw events/outer stream 摘要。
- 大事件默认折叠。
- 默认滚到底，但切换历史 run 不抢用户 scroll。

5. Q&A composer：

- ended exact anchor 且 capability 允许 -> 显示 Ask。
- fork 可用则默认 fork。
- 只支持 resume 时，默认不直接执行，显示确认文案。
- missing anchor 只显示 fresh_context。
- running intent 不显示 Ask。
- 关闭 thread 时提示短期问答不会保存在 Cairn；远程 worker 可能有自留日志。

6. Promotion UI：

- 每个 assistant answer 提供 promote actions。
- Promote modal 要让用户编辑内容，不直接原样写入。
- promotion 成功后刷新 project。

### 测试

当前没有前端 test harness，首版用 browser/manual smoke。若引入 Playwright，建议新增：

```text
cairn/tests/ui/test_v3_conversation_panel.spec.ts
```

最小手动/浏览器验收：

```bash
env PYTHONPATH=src python -m cairn.cli server --db ./cairn.local.db --host 127.0.0.1 --port 8000
```

在浏览器打开：

```text
http://127.0.0.1:8000
```

检查：

- 选中 running intent，只看到 live transcript，无 Ask。
- 选中 ended intent，Conversation tab 有结构化 events。
- 选中 legacy fact，显示 missing/fresh_context。
- Pi 大日志面板不冻结页面。
- 超长 tool result 折叠。

### 验收

- active intent detail 明显变轻。
- Worker output 不再与 intent metadata 混在一个 detail card 里。
- 用户能在 Conversation 面板完成一次问答并关闭。
- 关闭 fork/fresh_context 后 UI 不再展示 transcript。

### 审查点

- 不要把 UI 做成新的 marketing/解释页；它是工作面板。
- 不要再用字符截断作为主展示策略。
- 单文件 HTML 改动要控制函数大小；若 Alpine state 已明显膨胀，应在后续计划拆前端资源。

## Phase 8: Timeline、Compatibility 与 Final E2E

### 目的

收口兼容、timeline 行为、真实服务验证，确保实现可进入日常使用。

### 步骤

1. Timeline 规则：

- fork/fresh_context QuestionThread 不进 timeline。
- resume question run 作为轻量 run/timeline item 可见。
- existing Log tab 保留 project 历史，不替代 Conversation。

2. Compatibility pass：

- 旧 fact 的 `metadata.run_id` 在 UI 标为 legacy/report id。
- 旧 JSONL run 可 raw/transcript best effort，但 session provenance 一律 missing。
- `GET /runs/{run_id}` 旧 endpoint 继续可用。

3. Performance pass：

- Pi 100MB run log transcript endpoint 有合理响应时间上限。
- API 默认 limit 200，max limit 建议 1000。
- UI rendering 对大量 tool/message 使用折叠与增量渲染；若仍卡顿，再引入简单虚拟滚动。

4. Full test pass。

```bash
env PYTHONPATH=src python -m pytest cairn/tests/server/test_v3_run_provenance_schema.py cairn/tests/server/test_v3_run_provenance_api.py cairn/tests/server/test_v3_questions_api.py -q
env PYTHONPATH=src python -m pytest cairn/tests/dispatcher/test_v3_run_provenance_execution.py cairn/tests/dispatcher/test_v3_transcript_parsers.py cairn/tests/dispatcher/test_v3_question_capabilities.py -q
env PYTHONPATH=src python -m pytest cairn/tests/server/test_command_blackboard_v2_api.py cairn/tests/server/test_command_blackboard_v2_schema.py cairn/tests/server/test_db_cli.py -q
```

若本机缺 `docker` 依赖，全量 `cairn/tests/server` collection 可能失败；记录环境缺口，不把它误判为 v3 regression。

5. Manual E2E。

- 使用 `./cairn.local.db` 启 server 到 8000。
- 打开 proj005 Codex run，验证 structured conversation。
- 打开 proj007 Pi run，验证 parser coalesce 与 UI不卡死。
- 对新执行产生的 fact 做 anchor resolution exact。
- 对 legacy fact 做 missing/fresh_context。
- resume 问答确认弹窗、执行、run log、timeline 记录都正确。

### 验收

- v2 既有测试不破。
- v3 核心 API 与 parser 测试通过。
- 真实 UI 可看 Codex/Pi transcript。
- Q&A 风险提示清楚，模式选择符合 capability。

### 审查点

- 如果 Phase 6/7 复杂度过高，可以先发布 Transcript Panel + Provenance，Q&A 后接；但 DB provenance 不应再推迟。

## 风险与开放问题

### 风险

- **DB 与 run JSONL 双写不一致**：run log 已写但 provenance API 写失败。处理：run start DB 写失败时仍可写 JSONL，但 summary 标 `has_provenance=false`；session Q&A 禁用。
- **session 解析在 run finish 之后**：不强依赖 JSONL `session_resolved`，先更新 DB；JSONL 镜像只作调试。
- **Pi 大日志性能**：必须 coalesce + event limit；必要时加 parser cache，但首版不把 cache 当 source of truth。
- **resume 污染原 session**：UI confirmation + server resume lock + timeline/run provenance。
- **adapter fork 能力不稳定**：默认 capability 关闭 fork，直到本地 CLI 行为被测试固定。
- **单文件 UI 熵增**：v3 可以先落在 `index.html`，但要把 transcript/question helper 分段；若继续膨胀，下一轮单独规划前端拆分。

### 开放问题

- `session_resolved` 是否镜像到 JSONL：建议可选，不阻塞；API 必须用 DB。
- Claude Code fork 是否首版开启：需要本地 CLI 验证，不凭外部文档直接开。
- 是否为 transcript parser 增加缓存：首版先不做；如果 proj007 100MB 日志解析太慢，再加基于 run mtime/hash 的短期 cache。
- Promotion to Fact 是否需要单独人工 fact API：现有 conclude API 绑定 intent，不适合人工 promotion；Phase 6 需要明确新增路径。

## 最终验收清单

- [ ] DB migration `0003_run_provenance` 可 fresh/migrate/idempotent。
- [ ] 新 worker run 产生 DB provenance，含 `run_log_id`、worker、workspace、report、finish 状态。
- [ ] session 解析后 provenance 变为 `available`；解析不到变为 `missing`。
- [ ] 新 produced fact metadata 含 `provenance.producing_run_log_id`。
- [ ] legacy `metadata.run_id` 只显示为 report id，不用于 session。
- [ ] transcript endpoint 从 full raw JSONL 解析，再 limit normalized events。
- [ ] Codex/Pi/Claude fixtures 通过；Pi delta 被 coalesce。
- [ ] Conversation tab 替代 Detail 内大块 Worker Output。
- [ ] running intent 不显示 Ask。
- [ ] ended exact anchor 可 Q&A；missing anchor 只 fresh_context。
- [ ] fork/fresh_context 关闭后 Cairn 侧 transcript 不可再取。
- [ ] resume 需要确认，写 run log/provenance，并发受控。
- [ ] promotion 明确动作，metadata 保存 anchor/source run/mode/session effect/answer summary。
- [ ] Raw tab 仍能作为 parser failure fallback。
- [ ] 使用 `./cairn.local.db` 与 8000 端口完成手动 smoke。

## 建议实施顺序

1. Phase 0-2 先作为一个 PR：DB provenance + dispatcher run lifecycle + fact metadata。
2. Phase 3-4 第二个 PR：normalized transcript parser + runs/transcript API。
3. Phase 7 的只读 Conversation Panel 可与 Phase 3-4 同 PR，便于用户先用上输出渲染。
4. Phase 5-6 第三个 PR：adapter question contract + Q&A backend。
5. Phase 7 Q&A composer/promotion 与 Phase 8 收尾最后接入。

这个顺序的理由很朴素：没有 DB provenance，Q&A 的“问当时那个 worker”就是幻觉；没有 normalized transcript，UI 只能继续渲染粗 stdout。先把事实源和观察面做好，再让系统行动。
