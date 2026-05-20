# Interactive Worker Q&A v3 Architecture Recovery Plan

依据：`docs/specs/v3-interactive-worker-qna-requirements.md`、`docs/plan/v3-interactive-worker-qna-execution-plan.md`、2026-05-19 未提交实现审计
日期：2026-05-19
状态：架构修正提案，待确认后执行

## 0. 结论

当前未提交实现只能算“局部功能赶出来了”，不能算高质量满足 v3 spec。

应保留：

- `run_provenance` DB source of truth 方向。
- `run_worker_process` 外逸 `run_log_id` 方向。
- produced fact 写 `metadata.provenance.producing_run_log_id` 方向。
- transcript 从完整 run JSONL 重建 stdout/stderr 再 normalization 的方向。
- resume 并发锁与 promotion provenance 的雏形。

必须返工：

- Q&A 执行不应由 server 直连远端。现实现 `cairn/src/cairn/server/questions/executor.py:60` 起在 server 内解析 worker/env，并于 `:73` 直接 import `SshEnvironment`，`:75-80` 直接准备远程 project 与执行 worker。这破坏 server/dispatcher 边界，也使 Docker、本地未来 runtime、dispatcher lease/cancellation/healthcheck/pool 统一性全部绕开。
- fork capability 被硬编码为 false，并被测试锁死。`cairn/src/cairn/dispatcher/workers/base.py:67-76` 默认 `can_fork_session=False`；Codex/Pi/Claude/Mock adapters 也照此声明；`cairn/tests/dispatcher/test_v3_question_capabilities.py:40-49` 反而断言“无 fork”为正确结果。此处不是“未实现”，而是把坏体验固化为 contract。
- Q&A API 是同步长请求。`cairn/src/cairn/server/routers/questions.py:143-155` 在 HTTP request 内直接 `execute` worker；真实 worker 可运行数十秒到数分钟，无法稳定支持交互、状态、取消、重试与 dispatcher 统一调度。
- 多轮问答语义不成立。`cairn/src/cairn/server/questions/context.py:10-37` 构造 prompt 时只包含 anchor/provenance/当前 user message，未纳入前文 messages，也没有持续 fork session 的上下文策略。
- server 为了执行 worker 新增 `worker_inventory.model`、`model_context_window` 与 endpoint secret 重建路径，这是执行面上移导致的补丁。server 可知道 worker capability，但不应负责 worker 命令运行与凭据注入。
- UI 有 Output tab，但 mode 选择仍是 fresh/resume checkbox；没有 fork first、run selector、async job 状态、失败原因分层，也未从 capability 动态展示动作。

本提案的核心修正：

> Server 只做控制面：保存 question runtime 状态、解析 anchor、展示 transcript、接收用户消息与 promotion。Dispatcher 只做执行面：claim question job，使用 driver/environment 执行 fork/resume/fresh_context，回写事件与结果。

## 1. 目标边界

必须达成：

- v3 spec 的 provenance、transcript、Q&A、promotion 语义全部落到可维护边界。
- fork 不再被静态 false 否定；能力应由 adapter runtime capability 暴露。能 fork 则优先 fork，不能 fork 才 fresh/resume。
- server 不 import 具体 environment，不 build worker process，不拼 worker secrets。
- Q&A request 不阻塞执行；server 创建短期 runtime job，dispatcher claim/run/report。
- fork/fresh_context 问答仍是短期态；可用 DB runtime table 或 temp artifact 承载 active 会话，但 close/TTL 后清除，不作为长期历史。
- resume 继续原 session，必须确认、加锁、写 run log/run provenance/timeline。
- 多轮问答必须有明确策略：fork/resume 复用 question session/source session；fresh_context 每轮必须带 thread history 摘要。

明确不做：

- 不在本轮把 running intent fork 做进来。
- 不为了统一而伪造 fork。若 adapter 未被证明确有 fork，则显示 `fresh_context` 或需确认的 `resume`。
- 不引入大型前端 chat framework。
- 不把完整 fork/fresh_context transcript 长期归档。

## 2. 目标架构

### 2.1 控制面与执行面

```text
Browser
  -> Server: POST /questions
  -> Server: create QuestionThread + QuestionJob(runtime)
Dispatcher
  -> Server: claim question jobs
  -> Driver: build_question(mode, source_session, prompt, history)
  -> Environment: run worker process
  -> Server: append question events / patch job result / patch run provenance
Browser
  -> Server: poll/SSE question thread events
```

server 负责：

- `resolve_anchor`。
- `QuestionThread` / `QuestionJob` runtime state。
- resume source session lock。
- short-lived event store 与 TTL cleanup。
- promotion 写 hint/fact/intent。
- transcript API。

dispatcher 负责：

- worker inventory + capability publish。
- claim pending question job。
- environment selection/preparation。
- driver `build_question(...)`。
- run log / run provenance lifecycle。
- cancellation、timeout、healthcheck、lease 复用。

driver 负责：

- 声明真实能力。
- 构造 fork/resume/fresh_context 命令。
- 解析 remote session provenance。
- 提供或注册 transcript parser。

## Phase 0: 架构护栏与 RED 测试

### 目的

先写能打破现实现状的测试，防止继续在错误边界上补丁。

### 步骤

1. 新增 architecture guard tests：

```text
cairn/tests/server/test_v3_question_architecture_contract.py
cairn/tests/dispatcher/test_v3_question_job_execution.py
cairn/tests/dispatcher/test_v3_question_fork_capabilities.py
cairn/tests/server/test_v3_question_runtime_api.py
cairn/tests/ui/test_v3_question_modes_smoke.py
```

2. RED 断言：

- `cairn.server.questions` 不允许 import `cairn.dispatcher.runtime.environments.*`。
- `POST /questions` 带 message 时返回 active/pending thread，不直接执行 worker。
- server 只创建 `QuestionJob`；dispatcher 通过 claim API 执行。
- adapter capability 不能全局 hard-code fork false；Claude Code 至少进入 fork spike/实现测试，Codex/Pi 明确为 runtime-detected 或 unsupported-with-reason。
- fresh_context 第二轮 prompt 包含 previous messages 或 compressed thread summary。
- UI 在 capability 有 fork 时显示 fork action，且 auto 选择 `fork > resume-with-confirm > fresh_context`。

### 测试

```bash
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_question_architecture_contract.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_question_job_execution.py -q
```

### 验收

- 当前未提交实现应 RED。
- RED 失败点指向 server 直连执行、fork false、同步 Q&A、多轮缺上下文。

## Phase 1: Question Runtime Contract

### 目的

把 Q&A 做成和 `request-conclude` 同类的控制协议，而非 server 执行函数。

### 范围

```text
cairn/src/cairn/server/models.py
cairn/src/cairn/server/schema.py
cairn/src/cairn/server/migrations/0004_question_runtime.py
cairn/src/cairn/server/services.py
cairn/src/cairn/server/routers/questions.py
cairn/src/cairn/dispatcher/protocol/client.py
```

### 设计

新增 runtime table。它是短期运行态，不是长期 archive。

```text
question_threads
- id
- project_id
- anchor_type
- anchor_id
- source_run_log_id
- source_remote_session_kind/id/status
- worker_name
- mode
- session_effect
- status: active|closing|closed|failed|expired
- notice
- expires_at
- created_at
- updated_at

question_jobs
- id
- thread_id
- project_id
- mode
- message
- prompt_context_json
- status: pending|claimed|running|succeeded|failed|cancelled
- claimed_by
- claimed_at
- result_text
- error_code
- error_detail
- run_log_id/null
- question_remote_session_kind/id/status
- created_at
- updated_at

question_events
- id
- thread_id
- job_id
- seq
- role/kind/text/raw_json
- created_at
```

保留 TTL cleanup：

- close 后删除 question events/jobs，thread 可短期返回 410。
- server restart 后 active runtime 可标记 expired。
- resume jobs 的 `run_log_id` 与 run provenance 永久保留；短期 question transcript 仍可删除。

### API

```text
POST /projects/{project_id}/questions
GET  /projects/{project_id}/questions/{question_id}
POST /projects/{project_id}/questions/{question_id}/messages
POST /projects/{project_id}/questions/{question_id}/close
POST /projects/{project_id}/questions/{question_id}/promote

POST /dispatcher/question-jobs/claim
POST /dispatcher/question-jobs/{job_id}/start
POST /dispatcher/question-jobs/{job_id}/events
POST /dispatcher/question-jobs/{job_id}/finish
POST /dispatcher/question-jobs/{job_id}/fail
```

### 验收

- server question route 不执行 worker。
- dispatcher 可 claim 到 pending job。
- close/TTL 清理短期 events。
- resume lock 以 source remote session 为 key，job 结束/失败/close 均释放。

## Phase 2: Dispatcher Question Execution

### 目的

复用 dispatcher 既有执行能力，消除 server 直连远程。

### 范围

```text
cairn/src/cairn/dispatcher/scheduler/loop.py
cairn/src/cairn/dispatcher/tasks/questions.py
cairn/src/cairn/dispatcher/tasks/common.py
cairn/src/cairn/dispatcher/workers/base.py
cairn/src/cairn/dispatcher/workers/adapters/*.py
```

### 步骤

1. 新增 `run_question_task(...)`，与 explore/bootstrap/reason 同级。
2. scheduler 每轮 claim question job，按 worker/environment capability 调度。
3. `run_question_task` 使用 `run_worker_process(...)`：
   - `mode=fresh_context/fork`：不写长期 run provenance，除非需要 debug artifact；短期 events 回写 question runtime。
   - `mode=resume`：写 run log + run provenance，`task_type=question`，`session_effect=continued`。
4. 复用 cancellation/timeout/lease。不要在 server route 内等待 worker。
5. worker/env/secret 仍由 dispatcher config 与 endpoint loader 处理。

### 验收

- SSH/Docker environment 都不需 question server 特判。
- dispatcher 停止时，pending job 留在 server；claimed 超时可 requeue。
- resume run 出现在 run list/timeline；fork/fresh 不进入长期 timeline。

## Phase 3: Fork-First Capability

### 目的

把体验从“无 fork 只能 fresh/resume”修正为“真实能力优先 fork”。

### 规则

`QuestionCapability` 扩为带原因与检测来源：

```text
QuestionCapability
- can_resume_session
- can_fork_session
- can_use_tools
- can_stream_events
- resume_mutates_source
- fork_creates_remote_log
- question_modes
- detection: static|startup_probe|version_probe|config
- unavailable_reasons: dict[mode, reason]
```

### adapter 策略

- Claude Code：优先实现 `fork`。spec 已记录 CLI 有 `--fork-session`，应由 adapter 封装为 `build_question(mode="fork", source_session=...)`。
- Codex：不凭记忆断言支持 fork。做 startup/version probe；若当前 CLI 无 fork，则清晰返回 `fork_unavailable: codex_cli_no_supported_fork`.
- Pi：评估 RPC mode/session copy 能否表达 fork。若不能，保留 resume/fresh，但 UI 显示“backend lacks fork”，不是静默消失。
- Mock：实现 fake fork，用于 UI/dispatcher contract 测试。

### 验收

- 测试不再断言“所有真实 driver fork=false”。
- UI 能显示 `Ask in Fork`、`Ask with Fresh Context`、`Resume Source Session` 三类动作及不可用原因。
- auto mode 遵循 `fork > resume(需确认) > fresh_context`。

## Phase 4: Transcript 与 Provenance 收敛

### 目的

保留当前做对的部分，但把边界收紧。

### 保留

- `run_provenance` 表。
- `run_log_id` 与 `report_run_id` 分离。
- `Fact.metadata.provenance.producing_run_log_id`。
- transcript 从完整 raw run JSONL 重建。
- Pi delta coalesce。

### 修正

- parser contract 不应散落在 server 私有目录后让 driver 另有一套 session 解析。建立共享 `worker_events` contract：adapter 提供 `transcript_parser()` 或 parser registry 由 worker type 单源注册。
- `RunLogDetail` 与 `TranscriptResponse` 合并 provenance summary，避免 UI 分别走 latest transcript 与 anchor resolution 时拿到不一致状态。
- `resolve_anchor` 返回候选 runs，支持同一 intent 多 run 手选。
- session_resolved 可镜像到 JSONL，但 API 只信 DB。

### 验收

- legacy fact 不从 stdout 考古。
- fact -> producing run -> remote session 路径稳定。
- selected run 与 latest run 可在 UI 切换。

## Phase 5: UI 交互重做

### 目的

让 UI 反映真实模式，而非隐藏架构限制。

### 步骤

1. `Detail` 只放对象元信息与主动作。
2. `Output` tab 顶部显示 anchor、run selector、provenance/session 状态。
3. transcript viewer 保留 Messages/Tools/Raw，但 tool 默认折叠。
4. Question composer：
   - `Ask in Fork`：默认首选，若可用。
   - `Fresh Context`：永远可用，但显示会用摘要，不是原 session。
   - `Resume Source`：危险动作，需确认，显示会继续污染/推进原 remote session。
5. message submit 后显示 pending/running/succeeded/failed，不阻塞 UI。
6. promotion 按 hint/fact/intent 明确入口，记录 source metadata。

### 验收

- 有 fork capability 时，用户不需要先理解 resume 风险。
- 无 session 时，UI 明确显示 fallback reason。
- running intent 只显示 live transcript，无 Ask。

## Phase 6: 验证矩阵

### Unit / API

```bash
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_run_provenance_schema.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_run_provenance_api.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_question_runtime_api.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_question_job_execution.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_question_fork_capabilities.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_transcript_parsers.py -q
```

### Integration

- start server + dispatcher。
- create project。
- run explore/bootstrap 得到 fact。
- 打开 produced fact 的 Output，确认 exact producing run。
- `Ask in Fork`：返回 answer，关闭后短期 thread 不再可见。
- `Fresh Context`：无 session fact 仍可问，并显示 fallback。
- `Resume Source`：确认后执行，run provenance 中 `task_type=question`、`session_effect=continued`。
- 并发 resume 同一 source session：第二个请求 409 或排队。

### UI Smoke

- desktop/mobile 截图检查 Output tab、mode buttons、resume confirmation、tool collapse、missing provenance。
- 浏览器走一遍 fact -> output -> fork ask -> promote fact。

## 风险与开放问题

- Claude fork 命令细节需以当前安装版本实测确认；若版本不支持，capability 要给不可用原因。
- Codex/Pi fork 不应臆测；先 spike，后接入。
- runtime DB table 与“短期不保存”并不冲突，但必须有 TTL/close purge，文案也要准确。
- 若 dispatcher 不在线，Q&A 应显示 pending/dispatcher unavailable，而不是 server 自行降级直连。
- 当前本地测试环境缺 `docker` 包，dispatcher runtime 相关测试收集失败；后续需用项目正常依赖环境跑全量。

## 审计中已跑测试

通过：

```bash
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_question_capabilities.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_questions_api.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_transcript_parsers.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_run_provenance_schema.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/server/test_v3_run_provenance_api.py -q
```

未能收集：

```bash
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_v3_run_provenance_execution.py -q
env PYTHONPATH=cairn/src python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_reports.py -q
```

原因：当前本地 Python 环境缺 `docker` package，导入 `cairn.dispatcher.runtime.process` 时失败。此为环境缺口，不是证明代码正确或错误。

## 最终验收清单

- server question code 无直接 environment/process execution import。
- dispatcher 拥有所有 worker question execution。
- fork capability 不再被全局 false 与测试锁死。
- Claude fork 至少可用或有版本探测原因；Mock fork 用于 contract/UI 测试。
- fresh_context 多轮带历史摘要。
- resume 需确认、加锁、写 run provenance。
- produced fact 能定位确切 producing run。
- UI 能展示 fork/fresh/resume 三种模式与不可用原因。
- 定向测试与一次真实服务手动流程通过。
