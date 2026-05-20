# Interactive Worker Q&A and Transcript Panel Requirements v3

日期：2026-05-19
状态：需求已确认，可进入 Planning
依据：用户对下一阶段 worker 追问、active intent detail、worker output 渲染的需求；`docs/specs/v2-command-blackboard-requirements.md`；`docs/plan/v2-command-blackboard-execution-plan.md`；本轮读取的 `cairn/src/cairn/server/routers/runs.py`、`cairn/src/cairn/dispatcher/runtime/run_logs.py`、`cairn/src/cairn/dispatcher/workers/adapters/{codex,claudecode,pi}.py`、`cairn/src/cairn/server/static/index.html`。

## 1. 核心判断

下一阶段应做成两个相连但边界清楚的能力：

1. `Transcript Panel`：把 run log 中的 JSONL / stream-json / stdout 解析为统一事件流，并在 UI 中渲染为对话、工具调用、状态与原始输出。
2. `Worker Q&A`：用户可从 fact、intent、run 进入一次临时追问；系统优先向产生该上下文的 worker/session 提问，并允许远端工具补充 context。

第一版不应把“fork 当时 session”写死为通用前提。当前代码里三类 worker 只共同抽象出 `session`、`build_execute`、`build_conclude`、`extract_session`、`extract_response_text`；Codex/Claude/Pi 的 resume 与输出协议不同，fork 能力尚未在仓库中被证明。因此规格应要求 worker adapter 显式声明能力与动作契约：`resume`、`fork`、`tool_use`、`event_parser`、`build_question(...)`。

更硬的前提是：Cairn 必须把每次 worker execution 与其 remote session provenance 记录下来。否则 UI 只能“拿 fact/intent 的摘要重新问一个 fresh context”，不能声称 fork/resume 到了当时那个 worker session。

进入 Planning 前必须承认一个当前代码事实：`run_log_id` 现在由 `RunLogWriter` 内部生成，调用方拿不到；remote session 又是在 worker 进程返回后才由 adapter 从 stdout/stderr 提取，而 run log 此时已经 `finish()`。因此 v3 不只是“多存一个字段”，还需要调整 execution result / run log 生命周期。

推荐首版策略：

- 已结束 intent / fact：允许追问，优先使用产生该 fact 的确切 producing run 的 session。旧数据若没有 DB provenance / session id，则直接标为 `missing`，只允许 `fresh_context`，不做 stdout 考古推断。
- 运行中 intent：首版只读实时 transcript，不做 fork 追问。
- fork / fresh_context 问答默认只供当时查看，关闭聊天后销毁 Cairn 侧 transcript；用户可显式 promote 为 hint、fact 或新 intent。
- resume 问答允许作为“继续原 worker 会话”的显式动作，但必须警告用户它会影响原 session，并应按 run log 记录。
- Fact 不直接拥有 remote session；Fact 只记录 producing run provenance。session 的 source of truth 是 run。

## 2. 背景与现状

已确认现状：

- `runs.py` 已提供 `/projects/{project_id}/runs/latest?intent_id=...`，返回 summary、events、stdout、stderr、combined、truncated。
- 当前 truncation 规则混合 `MAX_EVENTS = 600` 与 `MAX_TEXT_CHARS = 120000`；UI 仍主要把 combined 文本塞进 `<pre>`。
- `RunLogWriter` 以 JSONL 写入 `run_started`、`stream`、`run_finished`，stream text 已做 secret redaction。
- worker adapter 已分别解析 Codex、Pi、Claude Code 的 JSONL 输出以提取 session 与最终回复，但解析逻辑只用于 dispatcher 内部，不向 UI 暴露规范化事件。
- UI side panel 现有 `Detail / Settings / Hints / Log`。intent detail 内嵌 Worker Output，与 intent 元信息混在同一面板。
- fact metadata 已可包含 `report_path`，fact 与 producing intent 的关系可由 intent `to` 推回。
- 当前 `run_started.metadata` 在 Codex/Pi 样例中没有 `remote_session_id`；session 是 worker stdout/stderr 结束后由 adapter 提取的结果，尚未作为 run provenance 稳定持久化。
- 当前 fact metadata 中的 `run_id` 实际是 report path 后缀使用的短 `report_run_id`，不是 `RunLogWriter` 生成的 Cairn run log id。v3 必须显式区分 `run_log_id` 与 `report_run_id`；旧数据若没有 DB provenance，不追溯 session。
- 当前 `run_worker_process(...)` 只返回 `ProcessResult`，不把 `RunLogWriter.run_id` 交给 explore/bootstrap 写 fact metadata；当前 `RunLogWriter.finish()` 后也会拒绝继续写事件。Planning 必须解决 `run_log_id` 外逸与 `session_resolved` 写入时机。

实际样例确认：

- 当前前端 `Worker Output` 调用 `/projects/{project_id}/runs/latest?intent_id=...` 或 `/projects/{project_id}/runs/{run_id}`，后端读取 `run_log_root()/project_id/run_id.jsonl`。本地样例路径：
  - Codex: `/Users/littlefairy/.local/share/cairn/runs/proj_005/run_15941dccca644e629a99d719eafe14c0.jsonl`
  - Pi: `/Users/littlefairy/.local/share/cairn/runs/proj_007/run_4fbf5c47a41f4ad89adfc1acb72b0c4b.jsonl`
- 外层 Cairn run log 是统一 JSONL：`run_started` -> 多条 `stream` -> `run_finished`。`stream.text` 是原始 stdout/stderr chunk，不保证按 worker 内层 JSONL 行边界切开。
- Codex 样例外层只有 4 条记录；stdout 拼接后是内层 JSONL：`thread.started`、`turn.started`、`item.started` / `item.completed` 的 `command_execution`、`item.completed` 的 `agent_message`、`turn.completed`。
- Pi 样例外层有 19574 条记录，绝大多数是 4096 字节 stdout chunk。stdout 拼接后才可解析为 Pi 内层事件，样例事件数：`session=1`、`agent_start=1`、`turn_start=17`、`message_start=40`、`message_update=7427`、`message_end=40`、`tool_execution_start=22`、`tool_execution_update=24`、`tool_execution_end=22`、`turn_end=17`、`agent_end=1`。
- Pi `message_update` 数量很大，且工具输出/assistant message 可非常长；UI 不应逐 chunk 渲染，而应按 message/tool id 合并、折叠、虚拟滚动。

外部参考：

- Codex CLI 官方帮助与 OpenAI Codex 仓库说明了 CLI / non-interactive 运行形态；仓库代码也使用 `codex exec --json` 与 `codex exec resume ... --json`。
- Claude Code 官方 CLI reference 支持 resume session、`--output-format stream-json`，并已有 `--fork-session`。这说明 Claude backend 可优先评估 fork，但 Cairn 当前 `ClaudeCodeDriver` 尚未接入该能力。
- Pi 官方 JSON Event Stream Mode 已定义 session header、agent/turn/message/tool execution 事件；RPC mode 也提供 JSON stdin/stdout 协议，面向 IDE 或自定义 UI。
- OpenCode 暴露 headless server API，包含 session/message/part、session fork、prompt_async 等接口；插件事件含 message、tool、permission、session 等类别。
- DeepSeek TUI README 显示其有 `exec --output-format stream-json`、session resume/fork、HTTP/SSE runtime API 与 ACP stdio adapter，但需在 planning 前继续读其 docs/source 确认事件 schema。
- ACP（Agent Client Protocol）是当前最贴近 Cairn renderer 目标的统一协议参考：JSON-RPC 2.0、session/update、message chunk、tool_call/tool_call_update、permission、terminal、session/load、capabilities。Cairn 不必首版完整实现 ACP，但 normalized transcript 字段应尽量向 ACP 可映射。
- UI 渲染可参考开源方向：assistant-ui 的 tool call components、CopilotKit、LangGraph/LangSmith 风格 traces、OpenAI Agents tracing viewer。但 Cairn 首版不应直接引入大框架；先定义自己的 normalized event model，再决定是否替换渲染层。

## 3. 目标

- 用户能在选中 fact、intent 或 timeline run 时看到结构化 execution transcript，而不是粗糙 stdout 文本。
- 用户能对一个历史 fact / intent 发起临时问答，并能从对象稳定追溯到 producing run 与 remote session；缺失时必须显示 fallback 原因。
- 追问 agent 能在远端 workspace 中读文件、查报告、调用工具，以补充 context；用户可在问题中主动声明只读约束。
- fork 问答与黑板主流程解耦：不自动创建 fact/intent，不改变原 intent 状态，不污染原执行报告。
- resume 问答是显式继续原 session：可用于让 worker 继续相关工作，并进入可追溯 run log。
- UI 将 “对象详情” 与 “conversation/output” 分离，让 active intent detail 更清爽。

## 4. 非目标

- 不在首版实现跨后端强制统一 fork。
- 不把所有 worker stdout 原文长期塞进 DB。
- 不做完整 trace analytics 平台。
- 不让追问自动修改黑板事实、自动宣告 intent 完成、或自动创建后续 intent。
- 不做 running intent 的 fork/并行追问；首版只看 live transcript。
- 不在首版做默认只读工具隔离；可在 prompt 与 UI 文案中让用户声明只读约束。
- 不在首版要求集成第三方 UI 框架；可评估，但不阻塞自有渲染。

## 5. 用户流程

### 5.1 查看 transcript

用户选中一个 intent 或 producing fact：

1. side panel 显示对象 summary：标题、描述、状态、worker、时间、report path。
2. conversation/output 进入独立面板或独立 tab；intent 默认展示 selected/latest run，producing fact 默认展示 producing run，旧数据缺 DB provenance 时展示 `missing` 状态。
3. 用户可切换：
   - `Conversation`：user / assistant messages。
   - `Tools`：工具调用、参数摘要、结果摘要、错误。
   - `Raw`：原始 stream，作为兜底。
4. 默认保留最后 N 条事件，而不是按字符硬截断；若单条事件过大，则单条事件内部折叠。

### 5.2 对已结束对象追问

用户从 fact 或 intent 点击 `Ask`：

1. 系统解析 anchor：
   - fact -> producing run provenance -> run session provenance。
   - intent -> selected/latest run -> run session provenance。
   - run -> run session provenance。
   - 旧 fact 若只有 `metadata.run_id`/`report_path` 而没有 DB provenance，直接标为 `missing`；可用 fact/report/transcript 摘要走 `fresh_context`，不尝试恢复原 session。
2. 若 adapter 支持 fork：创建临时 question session fork；关闭聊天后 Cairn 销毁本地短期 transcript。
3. 若只支持 resume，或用户明确想让原 worker 继续干相关活：可创建一次 `resume_question` / `resume_followup`。UI 必须标明它会继续原 session，并要求确认。
4. agent 回答可多轮继续。
5. 用户可把回答 promote 为：
   - hint：策略建议或人工备注。
   - fact：已验证事实。
   - intent：新的执行意图。

### 5.3 对运行中 intent

首版行为：

- 可看 live transcript。
- 不显示 `Ask` / `Ask in Fork`。
- running intent fork 是后续版本议题；本 spec 不要求 adapter 支持 live source session resolution。

## 6. 功能需求

### 6.1 Normalized Transcript

新增统一事件模型，供 API 与 UI 使用：

```text
TranscriptEvent
- id
- ts
- seq
- source: "run_log" | "worker_stdout" | "worker_stderr" | "parser"
- kind:
  - "run_started"
  - "run_finished"
  - "message"
  - "tool_call"
  - "tool_result"
  - "thinking"
  - "error"
  - "raw"
- role: "user" | "assistant" | "system" | "tool" | null
- title
- text
- tool_name
- tool_args_preview
- status: "running" | "success" | "error" | "cancelled" | null
- raw
```

要求：

- parser 必须先按 outer run log 顺序拼接同一 stream 的 text，再解析 worker 内层 JSONL；不得假设 `RunLogEvent.text` 正好是一条完整 worker event。
- 每个 worker adapter 提供 `parse_events(stdout, stderr) -> list[TranscriptEvent]` 或等价扩展点。
- 无法解析的行进入 `raw`，不能丢失。
- transcript parser 必须从原始 run JSONL 全量重建 stdout/stderr，再做 normalized event limit；不能复用当前 `RunLogDetail.stdout`，因为它已经按最后 600 条与 120k 字符截断，可能丢掉 Pi 的早期 `session` 事件。
- API 支持 `limit_events`，默认返回最后 N 条 normalized events；limit 只作用在 normalized output，不作用在 parser 输入。
- `truncated` 改为事件级：`events_omitted_before`、`large_event_collapsed`。
- secret redaction 保持在 run log 写入层；UI 不承担 secret 清洗。
- 对 Pi 这类高频 `message_update` / `tool_execution_update` 流，normalized transcript 应 coalesce 为稳定 message/tool 节点，并保留必要增量或最终态，不把 7000+ delta 直接铺到 UI。

### 6.2 Run Provenance

每次 worker execution 都必须产生可查询的 run provenance。它是 fork/resume Q&A 的硬前提，也是 transcript 选择的 source of truth。

最小语义字段：

```text
RunProvenance
- run_log_id: Cairn run log id, e.g. run_15941...
- project_id
- intent_id/null
- task_type: "bootstrap" | "explore" | "reason" | "question" | ...
- phase
- worker: { name, type }
- artifact: { report_path/null, report_run_id/null }
- environment: { environment_id, workspace }
- remote_session: {
    id/null,
    kind/null: "codex_thread" | "pi_session" | "claude_session" | ...,
    status: "available" | "missing" | "unresolved",
    capture_method/null: "prepared" | "stdout_event" | "stderr_regex" | "adapter_inferred" | "unavailable"
  }
- parent/null: { run_log_id/null, remote_session_id/null }
- question/null: { mode, anchor_type, anchor_id, source_run_log_id/null }
```

记录规则：

- `run_log_id` 与 `report_run_id` 必须分开命名。`run_id` 不再裸用，避免把报告编号误当成 run log 编号。
- dispatcher execution result 必须把 `run_log_id` 暴露给调用方，使 producing fact 可以记录确切 `producing_run_log_id`。
- run provenance 必须进入 DB，作为 run/session provenance 的 source of truth；run JSONL 仍是 transcript/raw event artifact，不负责承担唯一索引。
- `run_started.metadata` 可以先记录启动时已知字段；remote session 若只能在 stdout/stderr 后解析，应更新 DB provenance 中的 `remote_session`，并可追加 `session_resolved` 或等价 run event 供调试。
- `session_resolved` 若写入 JSONL，必须在 run logger 关闭前写入，或 run log 支持 post-finish append；但 API 查询以 DB provenance 为准。
- run summary API 必须合并 DB provenance 与 run JSONL 的 finished 状态，返回稳定的 `summary.provenance` 或等价结构。
- 不能每次为了找 session 都重扫巨大 stdout；解析 stdout 只服务 transcript rendering，不服务 session provenance backfill。
- adapter 提取不到 session 时必须显式记录 `remote_session.status=missing`，UI 才能正确 fallback 到 `fresh_context`。
- 旧数据若 DB 中没有 remote session provenance，则直接记录/返回 `remote_session.status=missing`；不从 Codex/Pi/Claude stdout 反推 session id。
- `source_fact_ids`、`produced_fact_id` 首版不必重复持久化在 run provenance 中；它们可由 intent sources、intent.to 与 fact metadata 反查。若 planning 认为需要不可变快照，再作为扩展字段加入。

Anchor resolution：

```text
AnchorResolution
- anchor_type: "fact" | "intent" | "run"
- anchor_id
- source_run_log_id/null
- status: "exact" | "missing"
- reason
```

规则：

- 新数据优先使用 fact metadata 的 `producing_run_log_id`，命中为 `exact`。
- 旧数据的 `metadata.run_id` 只能按 `legacy_report_run_id` 展示，不参与 session 恢复；禁止直接解释为 run log id。
- fact 缺少 `producing_run_log_id` 时，anchor resolution 为 `missing`。
- intent 可由用户选择具体 run；若所选 run 缺 DB provenance / remote session，则 session 为 `missing`。
- “successful run” 至少要求 `run_finished.returncode=0`、`timed_out=false`、`cancelled=false`。若缺 DB provenance，不用 `report_run_id` 猜测 producing run。

Fact provenance：

```text
Fact.metadata.provenance
- producing_intent_id
- producing_run_log_id/null
- report_run_id/null
- legacy_report_run_id/null
- report_path/null
- worker_name
```

规则：

- Fact 不存 `remote_session_id`。Fact 通过 `producing_run_log_id` 找 run，再由 run 找 remote session。
- 若 legacy fact 只有 `metadata.intent_id`、`metadata.run_id`、`metadata.report_path`，`metadata.run_id` 只作为 `legacy_report_run_id` / 报告后缀展示；不可直接当作 run log id，也不用于恢复 remote session。
- 同一 intent 多次执行、conclude fallback、resume follow-up 都可能产生多个 run。fact 追问必须优先命中产生该 fact 的确切 run，而不是简单取 intent latest run。

### 6.3 Conversation/Output 面板

UI 调整：

- `Detail` 只放对象元信息与主要动作。
- 新增 `Conversation` 或 `Output` tab，承载 transcript 与 Q&A。
- intent 详情内不再嵌套大块 `<pre>`。
- fact detail 若有关联 report/run，应显示 `Open Conversation` / `Ask`。
- timeline log 仍保留 project 级历史，但点击 run/intent 可跳到对应 conversation。

展示要求：

- message 用角色分组渲染。
- tool call 折叠显示，默认展示工具名、状态、耗时/时间、参数摘要。
- raw 输出只作为兜底 tab。
- 保留最后 N 条事件；N 可配置，默认建议 200。
- 对超长单条 text/tool result 做折叠，不影响其他事件可见性。

### 6.4 Worker Q&A

新增运行态 `QuestionThread` 概念。首版默认不入 DB；只在打开的 UI 会话与临时文件中存在。若是 resume 模式，则以 run log 追溯，不以 fork 问答线程追溯。

```text
QuestionThread
- id
- project_id
- anchor_type: "fact" | "intent" | "run"
- anchor_id
- worker
- source_run_log_id
- anchor_resolution: { status, candidates }
- source_session: { kind, id, status }
- question_session: { kind, id, parent_id } | null
- mode: "fork" | "resume" | "fresh_context"
- session_effect: "forked" | "continued" | "fresh"
- status: "active" | "closed" | "failed"
- created_at
- updated_at
```

问答要求：

- 支持多轮。
- 每轮 user message 与 assistant/tool events 进入 question transcript。
- 若 source session 不可用，fallback 为 `fresh_context`：把 fact/intent/report/run transcript 摘要作为 prompt context，而非假装问到了原 session。
- `fork` 与 `resume` 都要求 `anchor_resolution.status="exact"` 且 `source_session.status=available`；`anchor_resolution.status="missing"` 或 `source_session.status="missing"` 时只能 fresh_context。
- `question_session` 只表示新建的远端 question session；resume 通常继续 `source_session`，因此用 `session_effect="continued"` 表达，不要伪造一个新 session。
- 工具权限默认遵循 worker adapter 现有执行权限，但 question prompt 必须强调不得修改黑板，写操作需用户明确要求。
- 首版不强制只读工具模式；若用户希望只读，可在单次 question prompt 中声明。
- 追问失败时 UI 给出原因：无 session、adapter 不支持、worker 不可用、远端 workspace 不可达、tool 权限不足等。
- 同一个 `source_remote_session_id` 同时只能有一个 active `resume` question；并发 resume 必须拒绝或排队。fork / fresh_context 不受此锁约束，除非 adapter 声明不支持并发。

留存：

- `fork` / `fresh_context` 问答默认仅供当时查看，关闭后 Cairn 删除本地短期 transcript 与 QuestionThread 运行态。
- UI 必须简洁提示：此为短期问答，Cairn 不保存；但底层 worker 工具或远程机器可能仍有自己的 session/log。
- `resume` 模式不按短期问答处理：它是对原 session 的继续，Cairn 应写 run log，并可进入 timeline。
- 临时 QuestionThread 的默认 TTL 是当前 UI 会话；用户关闭、server 重启或 TTL 到期后，后端可返回 `404` 或 `410 gone`。若用户需要保留信息，必须显式 promote。

持久化规则：

- `mode=fork`：不进 timeline，不入 DB；关闭即销毁 Cairn 侧 transcript。
- `mode=fresh_context`：不进 timeline，不入 DB；关闭即销毁 Cairn 侧 transcript。
- `mode=resume`：进入 run log；可在 timeline 中出现一条轻量事件，例如 `worker resumed from question`。

resume run provenance：

- `task_type="question"` 或等价类型。
- `question_mode="resume"`。
- `question_anchor_type` / `question_anchor_id`。
- `source_run_log_id`。
- `source_remote_session_id` / `source_remote_session_kind`。
- `session_effect="continued"`。
- 新产生的 run 也要记录自己的 `run_log_id` 与 remote session 解析结果。

### 6.5 Adapter Capability

worker driver 应公开能力：

```text
QuestionCapability
- can_resume_session
- can_fork_session
- can_use_tools
- can_stream_events
- resume_mutates_source
- fork_creates_remote_log
- question_modes: ["fork", "resume", "fresh_context"]
```

语义：

- UI 只展示 capability 允许的动作。
- server/dispatcher 不通过 worker type 猜能力。
- 若 backend 协议不一致，由 adapter 吸收差异。
- adapter 不只声明布尔能力，还必须提供等价的 question 执行动作：

```text
build_question(mode, source_session, prompt) -> DriverResult
```

- `mode="resume"` 表示继续原 remote session；若 `resume_mutates_source=true`，必须走用户确认与并发锁。
- `mode="fork"` 表示创建独立 remote question session；若 worker 自身也会保留 fork 日志，UI 的短期保存提示仍要说明这一点。首版只用于历史/已结束对象，不用于 running intent。
- `mode="fresh_context"` 不依赖 remote session，由 Cairn 提供 fact/intent/report/transcript 摘要。

### 6.6 Promotion

问答输出默认临时。用户可显式执行：

- `Promote to Hint`
- `Promote to Fact`
- `Promote to Intent`

promotion 必须记录 provenance：

```text
metadata.source = {
  "kind": "question_thread",
  "question_thread_id": "...",
  "anchor_type": "...",
  "anchor_id": "...",
  "source_run_log_id": "...",
  "mode": "fork|resume|fresh_context",
  "session_effect": "forked|continued|fresh",
  "source_remote_session": {"kind": "...", "id": "...", "status": "..."},
  "answer_summary": "short human-readable summary"
}
```

规则：

- `question_thread_id` 不能作为唯一 provenance，因为 fork/fresh_context QuestionThread 关闭后会销毁。
- promoted object 自身保存被提升后的内容；metadata 只保留必要来源、模式、session 状态与摘要，不保存完整短期问答 transcript。

## 7. 数据/API/界面需求

### 7.1 API

建议新增：

```text
GET  /projects/{project_id}/runs/{run_log_id}/transcript?limit_events=200
GET  /projects/{project_id}/runs/latest/transcript?intent_id=...
GET  /projects/{project_id}/runs/{run_log_id}/provenance
POST /projects/{project_id}/questions
GET  /projects/{project_id}/questions/{question_id}
POST /projects/{project_id}/questions/{question_id}/messages
POST /projects/{project_id}/questions/{question_id}/close
POST /projects/{project_id}/questions/{question_id}/promote
```

`POST /questions` 输入：

```json
{
  "anchor_type": "intent",
  "anchor_id": "i3",
  "mode": "auto",
  "message": "这里的失败原因到底是什么？",
  "allow_resume_without_fork": false
}
```

`mode=auto` 选择优先级：

```text
fork > resume（若用户允许） > fresh_context
```

### 7.2 数据

首版数据策略：

- run provenance 是必需持久化数据，DB 是 source of truth；Planning 应通过版本化 database migration 增加 run provenance / run index 表或等价结构。
- run JSONL 继续保存 raw stream / transcript artifact；可镜像 `session_resolved` 供调试，但 API 不依赖 JSONL 推断 session。
- produced fact 必须记录 `producing_run_log_id` 或等价字段。旧 fact 只有 `report_run_id` 时按 `missing` 兼容处理，不恢复 session。
- fork / fresh_context：不入 DB；临时 transcript 可落在 server temp dir 或内存中，关闭后删除。
- resume：使用 run log 作为事实来源，保留 worker、source session、run log id、anchor。
- remote worker 自身可能仍保存 session/log；Cairn 只承诺不保存自己的 fork/fresh_context 问答 transcript。

### 7.3 UI

side panel 调整为：

- `Detail`：对象元信息与主要动作。
- `Conversation`：run transcript 与 question thread。
- `Hints`
- `Log`
- `Settings`

`Conversation` 内部区块：

- 顶部 anchor summary。
- run selector：latest run / historical runs。
- transcript viewer。
- question composer：仅在 capability 可用时显示。
- question thread list：同一 anchor 的临时问答。

## 8. 关键决策点

### 8.1 是否强推 fork？

不强推。fork 是理想模式，且 Claude Code / OpenCode / DeepSeek TUI 都已有可考的 fork 入口，但它仍不是 Cairn 当前三类 backend 的共同事实。强推会让协议被某个 backend 拖歪。应定义 capability，能 fork 则用 fork；用户需要原 worker 继续相关工作时可选 resume，但必须确认风险；不能 fork/resume 时 fallback fresh context。

### 8.2 问答是否临时？

fork / fresh_context 问答默认临时，关闭即销毁 Cairn 侧记录。UI 要明确告知“短期问答不保存”，同时说明远程 worker 可能自有日志。若用户将回答 promote 为 hint/fact/intent，promoted 对象只记录必要 provenance，不保存完整问答。resume 模式例外：它继续原 session，应作为 run log 保留。

### 8.3 running intent 是否可追问？

首版不支持 running intent 追问。并行 resume 可能污染 session；并行工具调用也可能改 workspace。运行中 intent 只展示 live transcript，不提供 `Ask` / `Ask in Fork`。running fork 留作后续版本重新评估。

### 8.4 是否使用现成 UI 框架？

先不绑定。更重要的是协议层：优先评估 ACP 是否足以作为 Cairn normalized event model 的上层参照。assistant-ui / CopilotKit 等可借鉴 message/tool-call rendering，但 Cairn 的核心输入是 run JSONL + worker-specific event，不是普通 chat SDK stream。先做 normalized event model，后续可替换 renderer。

### 8.5 统一事件协议候选

| 候选 | 已确认价值 | 边界 |
| --- | --- | --- |
| ACP | 专为 agent-client/editor 通信设计；含 message chunk、tool call/update、permission、terminal、session load 与 capability。 | 是双向 JSON-RPC 协议，Cairn 首版只需借鉴事件模型，不必完整实现 agent server。 |
| Pi JSON events | 与 Cairn 当前 Pi adapter 最贴近；已有 session、turn、message、tool_execution 事件。 | 是 Pi 私有事件，不适合作为跨 backend 公共 API 原样暴露。 |
| OpenCode server/events | 有 session/message/part、fork、prompt_async，插件事件类型较完整。 | 需要继续读 SDK schema，确认 Part 类型和 event payload。 |
| Codex JSONL | 官方 `--json` 已覆盖 thread、turn、item、agent_message、tool/file/MCP/web/plan。 | 是 Codex 私有 public event，不含通用 editor permission/terminal 语义。 |
| DeepSeek TUI stream-json/SSE/ACP | 看起来同时有 NDJSON、HTTP/SSE、ACP adapter 与 fork/resume。 | 需继续读具体 docs/source；README 不足以定 schema。 |

## 9. 验收标准

- 新 run log 或 run summary 能返回 `run_log_id`、`worker.type`、`environment.workspace`、`remote_session.id/status`；session 后解析的场景必须更新 DB provenance，并可选择镜像 `session_resolved` 到 JSONL。
- dispatcher execution result 能把 `run_log_id` 暴露给 explore/bootstrap，使新 produced fact 写入 `producing_run_log_id`。
- produced fact 能追溯到确切 `producing_run_log_id`；旧 fact 只有 `report_run_id` 时，UI 标为 `missing`，不静默当作 run log id。
- 选中 active intent 时，Detail 不再显示大块 raw stdout；Conversation tab 能看到结构化 transcript。
- Codex、Claude Code、Pi 至少各有一个 fixture，证明 JSONL/stream-json 可转成 normalized events。
- transcript parser 从原始 run JSONL 全量解析，再按事件数限制返回，并返回 omitted/collapsed 信息。
- Pi 样例的早期 `session` 事件不会因当前 `MAX_EVENTS` / `MAX_TEXT_CHARS` 截断规则丢失。
- 对已结束 intent 发起追问，若 session 存在且 adapter 支持，能完成至少一轮问答。
- 对 producing fact 发起追问，系统能自动定位 producing intent 与确切 producing run；旧数据缺 DB provenance 时显示 `missing/fresh_context`。
- 旧数据没有 DB remote session provenance 时，不从 worker stdout 反推 session id。
- 对无 source session 的 fact，系统使用 fresh_context，并在 UI 明确显示。
- running intent 只展示 live transcript，不显示 `Ask` / `Ask in Fork`。
- fork/fresh_context 问答关闭后 Cairn 不再展示 transcript，并提示用户短期问答未保存。
- resume 模式必须弹出确认，并写入 run log；timeline 只记录 resume 模式，不记录 fork 模式。
- 同一 source remote session 的并发 resume 会被拒绝或排队。
- 问答结果不会自动写入 fact/intent/hint；promotion 是显式动作，且 metadata 记录 provenance。
- promotion provenance 不只引用临时 `question_thread_id`，还保存 anchor、source run、mode、session effect、source session status 与回答摘要。
- Raw 输出仍可查看，作为 parser 失败时的调试兜底。

## 10. 待确认问题

已确认：

- fork/fresh_context 问答仅供当时使用，关闭后销毁 Cairn 侧 transcript；UI 要提示“短期问答不保存，但远程 worker 可能有日志”。
- 无 fork 的 resume 允许，但必须警告确认；它可用于让原 worker 继续干相关活，并正常进入 run log。
- 首版不做默认只读工具限制。
- QuestionThread 不进 timeline；resume 模式可进 timeline。
- Fact 不直接保存 remote session；Fact 只保存 producing run provenance，run 才是 session 的 source of truth。
- 现有 legacy `metadata.run_id` 应视作 report id/报告路径后缀；旧数据不做 session 恢复。
- `RunProvenance` 是更合适的实体名；remote session 是其子对象。
- run provenance 必须进入 DB，并通过 migration 管理 schema。
- running intent fork 暂不做。
- 进入 Planning 前的需求卡点已收敛为实现任务：DB provenance 表、`run_log_id` 外逸、session 解析后更新 DB、多轮问答 promotion provenance。

仍待调研：

1. ACP schema 中哪些字段应直接映射到 Cairn `TranscriptEvent`？
2. OpenCode SDK 的 `Message` / `Part` payload 是否可复用为 renderer model？
3. DeepSeek TUI 的 stream-json / HTTP-SSE / ACP adapter 事件 schema 是否稳定、可借鉴？
4. 是否需要为 Pi RPC mode 新增 backend，而不是继续只消费 `--mode json` stdout？
5. DB run provenance 表结构、索引与迁移拆分细节。
6. 是否在 JSONL 中额外镜像 `session_resolved` 事件，仅作调试与 raw log 可读性。

## 11. 参考链接

- OpenAI Codex CLI help: https://help.openai.com/en/articles/11096431
- OpenAI Codex non-interactive docs entry: https://github.com/openai/codex/blob/main/docs/exec.md
- OpenAI Codex non-interactive mode: https://developers.openai.com/codex/noninteractive
- Claude Code CLI reference: https://code.claude.com/docs/en/cli-reference
- Pi JSON Event Stream Mode: https://pi.dev/docs/latest/json
- Pi RPC Mode: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md
- OpenCode server API: https://opencode.ai/docs/server/
- OpenCode CLI: https://opencode.ai/docs/cli/
- OpenCode plugins/events: https://opencode.ai/docs/plugins/
- DeepSeek TUI README: https://github.com/Hmbown/deepseek-tui
- Agent Client Protocol overview: https://agentclientprotocol.com/protocol/overview
- Agent Client Protocol tool calls: https://agentclientprotocol.com/protocol/tool-calls
- assistant-ui tool rendering: https://www.assistant-ui.com/docs/ui/tool-group
- assistant-ui primitives: https://www.assistant-ui.com/docs/primitives
