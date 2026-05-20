# Command Blackboard v2 Execution Plan

依据：`docs/specs/v2-command-blackboard-requirements.md`

日期：2026-05-19

状态：待执行计划。本文把 v2 intent 拆为可实施阶段，优先保证协议清晰、代码可维护、测试覆盖充分。目标是交付一个可用的人工指挥黑板闭环，而不是只做 UI 表单。

## 0. 目标边界

### 必须达成

- 新 project 默认 `auto_reason = false`。
- 人类可手动创建 intent，并可指定 `requested_worker = worker.name`。
- `Intent.worker` 继续只表示当前 lease holder，不复用为调度请求。
- dispatcher 选择 worker 时尊重 `requested_worker`；指定 worker 忙时保持 pending，不 fallback。
- project 级 `allowed_auto_workers` 约束自动 reason 生成 intent 的调度范围。
- intent/project timeout override 按优先级生效。
- running intent 可被请求 `conclude_now`，dispatcher 进入 conclude fallback。
- fallback 失败时写失败 report，并把 intent 放回 pending。
- 每次成功执行 intent 前，worker 必须在 workspace 写 report。
- fact 保持短 description，并在 `metadata_json.report_path` 记录 report。
- SSH 与 Docker backend 都提供非空 `CAIRN_WORKSPACE`。
- WebUI 暴露 project auto reason/worker 范围、intent worker 指派、timeout、conclude now、pending 修改/撤回。
- 覆盖 unit、API、dispatcher scheduler、prompt/report、UI smoke、Docker Kali 手动 E2E。

### 明确不做

- 不做多用户 RBAC。
- 不做完整 admin 资源治理面板。
- 不做远端 report 在线预览作为首版阻塞项。
- 不做 terminal attach。
- 不做 worker 长期会话常驻。
- 不让普通 explore worker 生成新 intent。
- 不把 report 正文塞进 fact description。
- 不把 dispatcher worker config 全部迁入 DB；worker 定义仍归 dispatcher YAML。

### 兼容原则

- 现有 project/fact/intent 数据必须通过 migration 可读。
- 新字段默认值必须保持旧行为可解释：
  - `auto_reason=false` 是 v2 新默认，但旧数据 migration 后若需要保持老项目自动推进，可在迁移策略中设为 `true`；首版建议显式写文档并由用户选择，不做自动猜测。
  - 旧 fact `metadata_json=NULL`。
  - 旧 intent `requested_worker=NULL`、`control_state='normal'`。
- `Intent.worker` 的 lease 语义不得改变。

## Phase 0: Contract First 与测试基线

### 目的

先冻结协议，再动实现。避免后续把 `worker`、`requested_worker`、`model_profile`、`allowed_auto_workers` 混成一团。

### 步骤

1. 建立 v2 测试文件骨架。

```text
cairn/tests/server/test_command_blackboard_v2_schema.py
cairn/tests/server/test_command_blackboard_v2_api.py
cairn/tests/dispatcher/test_command_blackboard_v2_selection.py
cairn/tests/dispatcher/test_command_blackboard_v2_workspace.py
cairn/tests/dispatcher/test_command_blackboard_v2_reports.py
```

2. 先写失败测试：
   - project create 默认 `auto_reason=false`。
   - create intent 支持 `requested_worker`，但 `worker` 仍为 `None`。
   - conclude 支持 fact metadata。
   - old DB migration 后旧 fact/intent 可读。
   - requested worker 忙时不 fallback。
   - Docker environment handle workspace 非空。

3. 加一份 contract note 到测试顶部或 fixture 名：
   - `worker` means lease holder。
   - `requested_worker` means scheduling request。
   - `allowed_auto_workers` only constrains automatic dispatch, not manual requested worker。

### 测试命令

```bash
uv run --project cairn python -m pytest cairn/tests/server/test_command_blackboard_v2_schema.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_selection.py -q
```

### 验收

- 这些测试在实现前失败，失败原因指向缺失字段/行为，而非 fixture 错误。
- 测试命名能读出 v2 协议边界。

### 审查点

- 是否仍有人尝试用 `Intent.worker` 表达人工指定 worker？若有，退回重写。
- 是否把 worker type 当作 UI/调度约束？v2 只用 `worker.name`。

## Phase 1: Server DB Migration 与模型

### 目的

让 server 成为 v2 控制面真相源。字段设计宁可显式，也不要在 description 中塞约定。

### 步骤

1. 修改 `cairn/src/cairn/server/db.py`。

在 `projects` 增加：

```text
auto_reason INTEGER NOT NULL DEFAULT 0
allowed_auto_workers_json TEXT
default_timeout_seconds INTEGER
default_conclude_timeout_seconds INTEGER
```

在 `facts` 增加：

```text
metadata_json TEXT
```

在 `intents` 增加：

```text
requested_worker TEXT
timeout_override_seconds INTEGER
conclude_timeout_override_seconds INTEGER
control_state TEXT NOT NULL DEFAULT 'normal'
control_requested_at TEXT
control_requested_by TEXT
control_reason TEXT
```

2. `_migrate()` 对旧 DB 做幂等 `ALTER TABLE`。

3. 修改 `cairn/src/cairn/server/models.py`。

新增或扩展：

```text
Fact.metadata: dict | None
Intent.requested_worker
Intent.timeout_override_seconds
Intent.conclude_timeout_override_seconds
Intent.control_state
ProjectMeta.auto_reason
ProjectMeta.allowed_auto_workers
ProjectMeta.default_timeout_seconds
ProjectMeta.default_conclude_timeout_seconds
CreateProjectRequest v2 fields
CreateIntentRequest v2 fields
UpdateIntentRequest
RequestConcludeRequest
ConcludeRequest.metadata
```

4. 新增 helper，集中处理 JSON 字段。

建议放在 `server/services.py`：

```text
loads_json_object(raw) -> dict | None
loads_json_list(raw) -> list[str] | None
dumps_json(value) -> str | None
validate_worker_name_text(value)
validate_positive_timeout(value)
```

5. 更新 row -> model 转换：
   - `intent_to_model()`
   - `project_meta_from_row()`
   - fact row conversion helper，避免各 router 手写 `Fact(**dict(row))` 后漏 metadata。

### 测试

新增：

```text
cairn/tests/server/test_command_blackboard_v2_schema.py
```

覆盖：

- fresh DB schema 含全部 v2 columns。
- old DB migration 添加 columns 且不破坏旧 rows。
- `allowed_auto_workers_json` round-trip。
- fact metadata round-trip。
- invalid timeout 被 Pydantic 拒绝。
- invalid `control_state` 被拒绝。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/server/test_command_blackboard_v2_schema.py -q
```

### 验收

- 旧 project/fact/intent API 响应仍可反序列化。
- 新字段默认值符合 spec。
- JSON helper 有单点测试，不散落 ad hoc parsing。

### 审查点

- 不允许把 `metadata_json` 当字符串透传给前端。
- 不允许 API response 中出现未解析 JSON 字符串。

## Phase 2: Server API 语义

### 目的

让人工指挥动作成为一等 API：创建 intent、修改 pending、撤回 pending、请求收尾、写带 metadata 的 fact。

### 步骤

1. 修改 `cairn/src/cairn/server/routers/projects.py`。
   - `POST /projects` 接收 v2 project fields。
   - list/get project 返回 v2 fields。
   - project status 变化仍清理 lease；不得清掉 `requested_worker`。

2. 修改 `cairn/src/cairn/server/routers/intents.py`。
   - `POST /projects/{project_id}/intents` 接收：
     - `requested_worker`
     - timeout overrides
   - 保留 `worker` create claim 兼容路径，但推荐 UI 不再用 `Declare & Claim` 表示 worker 指派。
   - `PATCH /projects/{project_id}/intents/{intent_id}`：
     - 仅允许 open intent。
     - 若 intent 当前 `worker IS NOT NULL`，只能改非调度字段或直接拒绝；首版建议拒绝修改 `requested_worker/timeout`。
     - 可改 description、requested_worker、timeout overrides。
   - `DELETE /projects/{project_id}/intents/{intent_id}` 保持撤回 open intent；若 running 则拒绝或要求先 request conclude。首版建议 running 拒绝。
   - `POST /projects/{project_id}/intents/{intent_id}/request-conclude`：
     - 仅 active project。
     - 仅 open intent。
     - 设置 `control_state='conclude_requested'`、actor、reason、time。
   - `POST /projects/{project_id}/intents/{intent_id}/conclude`：
     - 请求体新增 `metadata`。
     - 写 fact 时保存 `metadata_json`。
     - conclude 后可把 intent control state 归档或保留；建议保留原值用于审计。

3. 修改 `cairn/src/cairn/server/routers/export.py`。
   - YAML export 包含 fact metadata 中的 report path。
   - intent export 包含 `requested_worker` 与 timeout/control state。
   - 保持 snapshot 可读，不输出空字段噪声过多。

4. 修改 `cairn/src/cairn/server/services.py`。
   - `validate_intent_creator_worker()` 保持旧语义：`worker` 只能等于 creator 或 null。
   - 新增 `validate_requested_worker()`，不要求等于 creator。
   - `get_claimable_open_intent_or_404()` 不因 `requested_worker` 拒绝；dispatcher 负责选择匹配 worker 后 claim。

5. 修改 `cairn/src/cairn/dispatcher/protocol/client.py`。
   - `create_intent(..., requested_worker=None, timeouts=None)`。
   - `conclude(..., metadata=None)`。
   - `request_conclude(...)`。
   - `patch_intent(...)` 可供后续 dispatcher 或测试使用。

### 测试

新增：

```text
cairn/tests/server/test_command_blackboard_v2_api.py
```

覆盖：

- create project 默认 `auto_reason=false`。
- create project 显式保存 `allowed_auto_workers` 与 timeout。
- create intent with `requested_worker` 后 `worker is None`。
- legacy create intent with `worker=creator` 仍可 claim。
- `requested_worker != creator` 合法。
- PATCH pending intent 修改 worker 与 timeout。
- PATCH running intent 修改 requested worker 被拒绝。
- DELETE pending intent 成功。
- DELETE running intent 被拒绝。
- request conclude 写 control fields。
- conclude 写 fact metadata。
- YAML export 带 report path。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/server/test_command_blackboard_v2_api.py -q
uv run --project cairn python -m pytest cairn/tests/server -q
```

### 验收

- API 可表达所有人工控制动作。
- 旧 heartbeat/release/conclude lease 语义未被破坏。

### 审查点

- `requested_worker` 不得参与 server claim 冲突判断。
- server 不应校验 requested worker 是否真实存在；worker inventory 来自 dispatcher，server 可做 best-effort 缓存，但不应成为部署强耦合。

## Phase 3: Worker Inventory 与可见性

### 目的

WebUI 不能硬编码 worker names，也不能让用户猜 YAML。需要一个受控 inventory。

### 推荐方案

首版由 server 暴露 dispatcher config 的只读视图，但数据来源仍是 dispatcher。

实现路径二选一：

1. 简单路径：新增 dispatcher-side API 不现实，因为当前 dispatcher 不是 HTTP server，不选。
2. 推荐路径：dispatcher 周期性向 server 上报 worker inventory，server 缓存并供 UI 读取。

### 步骤

1. server DB 新增 `worker_inventory` 表，或 settings-like 单行 JSON。

建议独立表：

```text
name TEXT PRIMARY KEY
type TEXT NOT NULL
model_profile TEXT
endpoint TEXT
task_types_json TEXT NOT NULL
max_running INTEGER NOT NULL
priority INTEGER NOT NULL
allowed_environments_json TEXT
updated_at TEXT NOT NULL
```

2. server models：

```text
WorkerInventoryItem
WorkerInventoryUpsertRequest
```

3. server router：

```http
GET /workers
PUT /workers
```

`PUT /workers` 首版可无鉴权，因本项目当前无 RBAC；文档标记 local/trusted deployment。

4. dispatcher startup 与 config refresh 后调用 `PUT /workers`。

5. UI 使用 `GET /workers` 作为 worker 下拉来源。

### 测试

新增：

```text
cairn/tests/server/test_worker_inventory_v2.py
cairn/tests/dispatcher/test_worker_inventory_publish_v2.py
```

覆盖：

- PUT 后 GET 可见。
- 第二次 PUT 删除不存在于新 payload 的旧 worker，避免 UI 显示过期兵力。
- inventory 不包含 worker env secret。
- worker task types/allowed environments round-trip。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/server/test_worker_inventory_v2.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_worker_inventory_publish_v2.py -q
```

### 验收

- WebUI worker 选择来自 `/workers`。
- secret 不进入 inventory。

### 审查点

- 不要把 `worker.env` 整包发给 server。
- 不要把 provider API key 或 resolved env 发给 UI。

## Phase 4: Workspace 抽象补齐

### 目的

report 协议依赖 workspace。必须先统一 SSH/Docker workspace，后续 report 检查才可靠。

### 步骤

1. 修改 `cairn/src/cairn/dispatcher/runtime/environments/base.py`。
   - `EnvironmentHandle.workspace: str` 改为非可选语义。
   - 新增 protocol 方法：

```text
report_path(handle, intent_id, run_id=None) -> str
is_path_in_workspace(handle, path) -> bool
read_text_file(handle, path) -> str
exists(handle, path) -> bool
```

若不想扩大 Protocol 太多，至少新增：

```text
project_workspace(project_id) -> str
```

但从可维护性看，文件检查应留在 environment 抽象内，避免 dispatcher 对 Docker/SSH 分支判断。

2. 修改 SSH backend。
   - 保持 `{workspace_root}/{project_id}`。
   - 实现 exists/read/is_path_in_workspace。
   - `graph_snapshot_path()` 改到 `$workspace/.cairn/prompts/{phase}`。

3. 修改 Docker backend。
   - `prepare_project()` 返回 workspace：

```text
/home/kali/workspace/.cairn/projects/{project_id}
```

   - ensure container running 后创建 workspace 目录。
   - build process 注入 `CAIRN_WORKSPACE`。
   - `graph_snapshot_path()` 改到 `$workspace/.cairn/prompts/{phase}`。
   - startup workspace 使用独立路径：

```text
/home/kali/workspace/.cairn/startup/{uuid}
```

4. 修改 `ContainerManager`。
   - 增加 `exec_mkdir(container, path)` 或用 `write_text_file()` 间接建目录。
   - 增加 `file_exists(container, path)`。
   - 增加 `read_text_file(container, path)`。

5. 修改 run log metadata。
   - workspace 不再为 null。
   - Docker 也记录 project workspace。

### 测试

新增：

```text
cairn/tests/dispatcher/test_command_blackboard_v2_workspace.py
```

覆盖：

- Docker `prepare_project("proj_001").workspace` 为 `/home/kali/workspace/.cairn/projects/proj_001`。
- Docker build process env 包含 `CAIRN_WORKSPACE`。
- SSH graph snapshot path 在 workspace 下。
- Docker graph snapshot path 在 workspace 下。
- `is_path_in_workspace()` 拒绝 `..`、其它绝对路径、同前缀假路径。

可用 fake ContainerManager 做 unit，不必每个测试都跑 Docker。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_workspace.py -q
```

### 验收

- 所有 backend `EnvironmentHandle.workspace` 非空。
- prompt snapshot 与 report path 同属 workspace。

### 审查点

- 不要在 task 层用字符串 `if environment.backend == "docker"` 拼路径。
- 路径检查必须处理 `/foo/bar2` 不是 `/foo/bar` 子路径的问题。

## Phase 5: Report 协议与 Prompt

### 目的

让 report 成为硬协议，而不是靠 worker 自觉。dispatcher 负责提供路径、检查存在、写 metadata。

### 步骤

1. 新增 report helper。

建议新文件：

```text
cairn/src/cairn/dispatcher/tasks/reports.py
```

职责：

```text
build_report_path(environment, handle, intent_id, run_id) -> str
report_instruction(report_path) -> str
validate_report_written(environment, handle, report_path) -> bool
write_failure_report(environment, handle, report_path, context) -> None
metadata_for_report(report_path, run_id, worker, intent_id) -> dict
```

2. 修改 prompt templates。

`explore.md` 增加：

- `{report_path}` placeholder。
- 明确必须写 report。
- 明确 report schema。
- JSON output 仍只需 short `description`，不要求模型输出大 JSON。

`explore_conclude.md` 增加：

- `{report_path}`。
- 若主阶段 report 已存在，应补全或引用。
- 收尾时不得继续探索，但可以读取已有 report/workspace 文件。

3. 修改 `validate_prompt_resources()`。
   - default `explore.md`、`explore_conclude.md` 必须包含 `{report_path}`。
   - mock prompt 也补齐或在 group-specific required tokens 中允许差异。

4. 修改 `run_explore_task()`。
   - prepare handle 后生成 run id 与 report path。
   - render prompt 时传 report path。
   - execute 成功解析 JSON 后，先检查 report，再 conclude。
   - report 缺失：进入 conclude fallback 补写。
   - conclude 成功解析 JSON 后，再检查 report。
   - 仍缺失：write failure report，release intent，返回 `failed_missing_report` 或 pending 语义。

5. 修改 `write_conclude_result()`。
   - 支持 metadata。
   - 调用 client.conclude metadata。

6. 失败 report 语义。
   - fallback 失败/超时时，dispatcher 写 failure report。
   - release intent，使其回 pending。
   - 不写成功 fact。
   - outcome 可为 `failed_report_written`，用于日志区分。

### 测试

新增：

```text
cairn/tests/dispatcher/test_command_blackboard_v2_reports.py
```

覆盖：

- report path 生成在 workspace 下。
- prompt 包含 report path。
- execute 成功但 report 缺失时不 conclude 成功。
- conclude fallback 补写 report 后 conclude 成功，metadata 包含 report path。
- fallback 失败时写 failure report 并 release intent。
- path outside workspace 被拒绝。
- run log metadata 含 report path，且无 secret。

Mock worker 增强：

- 支持成功写 report。
- 支持故意不写 report。
- 支持 conclude 写 report。
- 支持 conclude 超时/失败。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_reports.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher -q
```

### 验收

- 无 report 不得产生成功 fact。
- 成功 fact 必有 `metadata.report_path`。
- 失败 report 可在 workspace 找到。

### 审查点

- 不要让模型在 JSON 里返回 report 正文。
- 不要用 fact description 尾巴拼 `[report: ...]` 作为机器协议。

## Phase 6: Dispatcher 调度语义

### 目的

把 v2 的人工指定、自动 worker 范围、auto reason 默认关闭接入主循环。

### 步骤

1. 修改 `DispatcherLoop._try_dispatch_project()`。
   - 已确认：v2 首版保留当前自动 bootstrap 行为。
   - 理由：bootstrap 是现有新项目启动闭环的一部分；直接关闭会破坏已有端到端路径与测试稳定性。
   - UI/文档需标明：`auto_reason=false` 只表示不自动 reason；不表示关闭 bootstrap。
   - 若后续需要“连 bootstrap 也完全人工”，应单独新增 `auto_bootstrap=false`，不要偷用 `auto_reason` 表达两个开关。

2. reason 调度。
   - `_dispatch_reason()` 前检查 `project.project.auto_reason`。
   - `auto_reason=false` 时不运行 reason。
   - `_reason_trigger()` 可保持，但只在 auto reason enabled 后调用。

3. explore intent 过滤。
   - unclaimed intents 仍取 open intent。
   - 优先 dispatch 有 `requested_worker` 的 pending intent，还是 newest？
   - 建议保持当前 newest 策略，但 worker selection 必须尊重每个 intent 的 request。
   - 若 newest 被 requested worker blocked，而其它 intent 可跑，是否跳过 newest 跑其它？
   - 为吞吐与可用性，建议 selection 遍历 unclaimed intents newest first，找到第一个可调度者；若某 intent blocked，记录 blocked reason，继续尝试其它 intent。

4. 修改 `_select_worker()` 签名。

```text
_select_worker(project, task_type, environment_id, intent=None, auto_scope=False)
```

或更可维护：

```text
WorkerRequest(
  project_id,
  task_type,
  environment_id,
  requested_worker,
  allowed_auto_workers,
)
```

建议新 dataclass，避免参数膨胀。

选择规则：

- 若 `requested_worker` 非空，只允许同名 worker。
- 否则若 intent 来自 auto reason 或 project policy 要求，则限制 `allowed_auto_workers`。
- 再执行现有 filters：
  - allowed environment
  - endpoint available
  - task type
  - max_running
  - unhealthy
  - rejected backoff

5. blocked reason。
   - `WorkerSelection` 增加：
     - `blocked_requested_worker`
     - `blocked_auto_worker_scope`
     - `blocked_missing_worker`
   - 日志中包含 requested worker。
   - 后续 UI 可复用。

6. timeout resolution。

新增 helper：

```text
effective_task_timeout(config, project, intent, task_type, phase)
```

规则：

```text
intent.timeout_override_seconds
> project.default_timeout_seconds
> config.tasks.explore.timeout
```

conclude 同理。

7. request conclude now。
   - dispatcher 每轮读取 project 后，若 running task 对应 intent `control_state=conclude_requested`，调用 `TaskCancellation.cancel("conclude_requested")`。
   - explore task 的 cancellation 分支要区分：
     - inactive project: 不 fallback。
     - conclude_requested: 进入 conclude fallback。
     - deleted/stopped/abort: 不 fallback。
   - 当前 `_try_conclude_fallback()` 会因 `cancellation.is_cancelled` 直接跳过；需改为允许 conclude_requested。

8. pending 语义。
   - requested worker 忙/不可用，不 claim intent。
   - fallback 失败 release intent。
   - release 后 control state 是否清回 normal？
   - 建议 fallback 失败后 server 清回 `normal` 或新增 `last_failure_metadata`。首版可由 dispatcher PATCH 清回 normal，并保留 failure report path 在 run log；若要显示给 UI，则需 intent metadata。为避免范围膨胀，首版只回 pending + log + failure report。

### 测试

新增：

```text
cairn/tests/dispatcher/test_command_blackboard_v2_selection.py
cairn/tests/dispatcher/test_command_blackboard_v2_conclude_now.py
cairn/tests/dispatcher/test_command_blackboard_v2_timeouts.py
```

覆盖：

- `auto_reason=false` 不 dispatch reason。
- `auto_reason=true` dispatch reason。
- `requested_worker` 只选择同名 worker。
- 同名 worker busy 时不 fallback。
- missing requested worker 记录 blocked reason。
- `allowed_auto_workers` 限制自动 intent。
- 人工 requested worker 不受 `allowed_auto_workers` 限制。
- timeout override 优先级。
- conclude_requested cancellation 进入 fallback。
- stopped project cancellation 不进入 fallback。
- fallback failed releases intent。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_selection.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_conclude_now.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_timeouts.py -q
```

### 验收

- 调度行为与 spec 一致。
- worker selection 仍是单一清晰入口。
- cancellation reason 不再粗暴一刀切。

### 审查点

- 不要在 `_try_dispatch_project()` 中堆复杂 if；抽 helper。
- worker selection 的返回值要足够解释 blocked reason，否则 UI 后续会补债。

## Phase 7: Reason/Bootstrap 策略调整

### 目的

让“默认人工决策”真正成立，同时保留可选自动模式。

### 步骤

1. Reason。
   - project `auto_reason=false` 时完全不 claim reason。
   - project `auto_reason=true` 时沿用现有 reason trigger。
   - reason 创建 intent 时不设置 `requested_worker`。
   - reason 创建 intent 的 creator 仍为 reason worker name，方便审计。

2. Bootstrap。
   - 已确认：v2 首版保留当前自动 bootstrap。
   - 原因：
     - 当前项目初始态依赖 bootstrap 快速产出第一个 fact。
     - 直接关闭 bootstrap 会改变很多现有端到端测试。
   - 但要在文档与 UI 标出：bootstrap 是兼容自动入口，后续可加 `auto_bootstrap`。

3. 防止 explore worker 派生 intent。
   - 当前 `explore` contract 只接受 description；保持。
   - prompt 加明确禁止生成 intent。
   - parser/validator 已阻止 explore 输出 intent；补测试。

### 测试

新增/更新：

```text
cairn/tests/dispatcher/test_command_blackboard_v2_reason_policy.py
cairn/tests/dispatcher/test_contracts.py
```

覆盖：

- auto_reason disabled 时 graph changed 也不 dispatch reason。
- auto_reason enabled 时正常 dispatch reason。
- reason-created intent `requested_worker is None`。
- explore payload 含 intent 字段不被当成新 intent。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_command_blackboard_v2_reason_policy.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_contracts.py -q
```

### 验收

- 默认人工决策成立。
- 自动 reason 可被显式开启。

### 审查点

- `auto_reason=false` 不应阻止 bootstrap；首版保留 bootstrap 自动行为是明确决策。
- 若未来要关闭 bootstrap，应新增 `auto_bootstrap`，不要偷用 `auto_reason` 表达两个开关。

## Phase 8: WebUI 控制面

### 目的

把 v2 能力从 API 暴露给实际使用者；但保持 UI 先实用，不追求完整远端 report viewer。

### 步骤

1. 读取 worker inventory。
   - app state 增加 `workers: []`。
   - load environments/project 时同步 load workers。
   - 只展示支持 `explore` 的 worker 作为人工 intent 可选项。

2. New Project modal。
   - 增加 `auto_reason` toggle，默认 off。
   - 增加自动可用 workers 多选。
   - 增加 default timeout input。
   - 仅对自动模式解释其作用；不要把它说成权限边界。

3. Create Intent modal。
   - 去掉或弱化 `Declare & Claim` 作为主按钮。
   - 增加 worker select：
     - `Auto / dispatcher chooses`
     - worker names
   - 增加 timeout override。
   - create payload 使用 `requested_worker`，不是 `worker`。

4. Intent detail。
   - 展示 requested worker。
   - 展示 timeout override。
   - pending 时允许 edit。
   - pending 时允许 delete。
   - running 时显示 `Request conclude`。
   - 若 control state conclude requested，显示状态。

5. Fact detail。
   - 展示 metadata report path。
   - 暂不需要打开远端 report 内容。
   - Docker report path 与 SSH report path 都原样展示。

6. Toast/blocked reason。
   - API 409/400 给清晰错误。
   - dispatcher blocked reason 后续可通过 intent status API 展示；若首版未做状态 API，则先在 run log/dispatcher log 可见。

### 测试

若已有前端测试基础不足，先做轻量 browser smoke 手测脚本记录；若可引入 Playwright，则新增：

```text
cairn/tests/server/test_static_ui_v2_smoke.py
```

或使用 Playwright：

```bash
npx playwright test
```

Smoke 覆盖：

- 新 project modal 中 `auto_reason` 默认 off。
- worker 多选来自 `/workers` mock response。
- create intent 发送 `requested_worker`。
- pending intent edit 发送 PATCH。
- running intent request conclude 发送 POST request-conclude。
- fact metadata path 可见。

### 验收

- 用户不需编辑 YAML 即可做 v2 人工指挥。
- UI 不再误导“claim = 指定 worker 执行”。

### 审查点

- 不要在前端硬编码 `pi-GPT5.5`。
- 不要把 `worker` lease 字段当 requested worker 显示。

## Phase 9: Run Logs、Export 与可观测性

### 目的

调度问题必须可诊断。v2 多了 pending/blocked/requested worker，日志要能说明为什么没出征。

### 步骤

1. run log metadata 增加：
   - `requested_worker`
   - `effective_timeout_seconds`
   - `effective_conclude_timeout_seconds`
   - `report_path`
   - `control_state_at_start`

2. dispatcher selection log 增加：
   - requested worker。
   - allowed auto workers。
   - blocked reason lists。

3. export YAML 增加：
   - fact metadata report path。
   - intent requested worker/timeouts/control state。
   - project auto reason/allowed workers。

4. 如果 UI 需要 blocked reason，新增只读 API：

```http
GET /projects/{project_id}/intents/{intent_id}/dispatch-status
```

首版可不做，避免过早把 dispatcher 内部瞬时状态持久化。

### 测试

新增/更新：

```text
cairn/tests/server/test_export_v2_blackboard.py
cairn/tests/dispatcher/test_run_log_v2_metadata.py
```

覆盖：

- export 包含 metadata。
- run log metadata 包含 report path/timeouts。
- secrets 不进入 metadata。

运行：

```bash
uv run --project cairn python -m pytest cairn/tests/server/test_export_v2_blackboard.py -q
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_run_log_v2_metadata.py -q
```

### 验收

- 人类能从 graph/export/run log 找到 report。
- pending 原因至少在 dispatcher log 中可查。

## Phase 10: 配置与文档

### 步骤

1. 更新 `dispatch.example.yaml` 注释。
   - 说明 `worker.name` 是 UI 可见兵种名。
   - 说明 `max_running` 表示并行容量。

2. 更新 README。
   - 新项目默认人工决策。
   - 如何开启 auto reason。
   - 如何手动指定 worker。
   - report 路径在哪里。

3. 更新 v2 spec 如实施中有取舍：
   - bootstrap 是否保留自动。
   - worker inventory 采用上报缓存还是其它路径。

4. 新增 manual test 文档段落到本 plan 末尾或 README。

### 检查

```bash
rg -n "Declare & Claim|worker must be null or equal|auto_reason|requested_worker|report_path|CAIRN_WORKSPACE" README.md docs cairn/src/cairn/server/static/index.html
```

### 验收

- 文档不再把 hint 说成唯一人类干预方式。
- 文档明确 fact/report 分层。

## Phase 11: 全量测试矩阵

### 单元与 API

```bash
uv run --project cairn python -m pytest cairn/tests/server -q
uv run --project cairn python -m pytest cairn/tests/dispatcher -q
```

### Config / startup healthcheck

```bash
uv run --project cairn cairn dispatch --config dispatch.example.yaml --startup-healthcheck-only
```

若 example 无真实 endpoint，可用 mock/dev config：

```bash
uv run --project cairn cairn dispatch --config dispatch.dev.yaml --startup-healthcheck-only
```

### Static checks

```bash
uv run --project cairn python -m compileall cairn/src
rg -n "api_key|OPENAI_API_KEY|ANTHROPIC_AUTH_TOKEN|PI_API_KEY" datas cairn/src/cairn/dispatcher/runtime/run_logs.py
```

第二条只作人工审查，避免误报；重点是 run log/report metadata 不泄露 secret。

### UI smoke

若引入 Playwright：

```bash
npx playwright test
```

否则人工执行 Phase 12。

## Phase 12: Docker Kali 手动验收

### 目的

用 Docker backend 验证 v2 的最小可用成品：人工创建 intent、指定 worker、报告落盘、fact metadata、timeout/conclude_now。

### 准备

1. 使用既有 v0 Docker/Kali 测试环境 `pentestVM`。
   - 进入方式：

```bash
docker exec -it pentestVM zsh
```

   - 该容器是手动测试目标环境；测试脚本、临时 workspace、report、probe 文件不得写入或修改 `/home/kali/ctf`。
   - 允许使用 `/home/kali/cairn-workspaces` 或 `/tmp/cairn-v2-*` 作为测试 workspace。
   - 测试前记录实际 DB 路径、dispatch config 路径、workspace 路径，便于验收后清理。
   - 不在 `pentestVM` 中保留临时 project、DB、workspace、report 或 probe 垃圾。

2. 若需要重新拉取基础 worker image：

```bash
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```

3. 准备 `dispatch.dev.yaml`。

建议使用 mock worker 或可控低成本 worker：

```yaml
runtime:
  interval: 2
  max_workers: 2
  max_running_projects: 1
  max_project_workers: 2
  healthcheck_timeout: 10
  prompt_group: "mock"

workers:
  - name: mock-fast
    type: mock
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
    env:
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,0.1],"outcomes":{"fact":"1.0","rejected":"0.0","invalid_json":"0.0","invalid_payload":"0.0","command_fail":"0.0"},"write_report":true}'

  - name: mock-slow
    type: mock
    task_types: [explore]
    max_running: 1
    priority: 1
    env:
      MOCK_EXPLORE_EXECUTE: '{"delay":[30,30],"outcomes":{"fact":"1.0","rejected":"0.0","invalid_json":"0.0","invalid_payload":"0.0","command_fail":"0.0"},"write_report":true}'
```

若 mock adapter 不支持 `write_report`，实施时应补该能力，专供 report 协议测试。

4. 启动 server。

```bash
uv run --project cairn cairn server --db /tmp/cairn-v2-manual.db --host 127.0.0.1 --port 8000
```

5. 启动 dispatcher。

```bash
uv run --project cairn cairn dispatch --config dispatch.dev.yaml
```

### 手测 A: 默认人工决策

1. 打开 WebUI。
2. 创建 project：
   - environment: Docker Default
   - auto reason: off
   - allowed auto workers: empty 或 mock-fast
3. 观察：
   - project 不应自动 reason 派生新 intent。
   - bootstrap 保持当前自动行为；这属于 v2 首版确认保留的兼容入口。

验收：

- 没有 reason lease。
- UI 显示 `auto_reason=false`。

### 手测 B: 人工 intent 指定 worker

1. 选择 `origin` fact。
2. 创建 intent：
   - description: `检查 /etc/os-release 并写报告`
   - requested worker: `mock-fast`
   - timeout: 20s
3. 等 dispatcher 执行。

验收：

- intent 被 `mock-fast` claim。
- conclude 后新增 fact。
- fact metadata 含 `report_path`。
- Docker 容器中存在 report：

```bash
docker exec cairn-dispatch-proj_001 test -f /home/kali/workspace/.cairn/projects/proj_001/.cairn/reports/execution-i001-*.md
```

实际 project id/intent id 以 UI 为准。

### 手测 C: 指定 worker 忙时 pending 不 fallback

1. 创建两个 intent，均 requested worker `mock-slow`。
2. `mock-slow.max_running=1`。
3. 第一个运行后，第二个应 pending。

验收：

- 第二个不被 `mock-fast` 执行。
- dispatcher log 显示 requested worker busy。

### 手测 D: request_conclude_now

1. 对正在运行的 `mock-slow` intent 点击 request conclude。
2. dispatcher 应 cancel 主执行并进入 conclude fallback。

验收：

- run log 有 cancel reason `conclude_requested`。
- 若 conclude 成功，写 fact + metadata report path。
- 若 conclude 失败，写 failure report，intent 回 pending。

### 手测 E: timeout override

1. 创建 intent requested `mock-slow`，timeout override 3s。
2. mock-slow delay 30s。

验收：

- 约 3s 后进入 conclude fallback。
- run log metadata 中 effective timeout 为 3。
- fallback 失败时 intent pending，failure report 存在。

### 手测 F: report 缺失防线

1. 切换 mock worker 为“不写 report”模式。
2. 执行 intent。

验收：

- dispatcher 不写成功 fact。
- 尝试 conclude fallback。
- fallback 仍无 report 时写 failure report。
- intent 回 pending。

### 手测清理

验收完成后清理本轮产生的临时资源。

若使用临时 DB：

```bash
rm -f /tmp/cairn-v2-manual.db /tmp/cairn-v2-manual.db-shm /tmp/cairn-v2-manual.db-wal
```

若使用 Docker backend：

```bash
docker ps -a --filter "name=cairn-dispatch-"
docker ps -a --filter "name=cairn-startup-healthcheck-"
ids="$(docker ps -aq --filter "name=cairn-dispatch-") $(docker ps -aq --filter "name=cairn-startup-healthcheck-")"
if [ -n "$(printf '%s' "$ids" | tr -d '[:space:]')" ]; then
  docker rm -f $ids
fi
```

清理要求：

- 删除本轮手测创建的 project DB 或临时 DB。
- 删除本轮创建的 `cairn-dispatch-*` 与 `cairn-startup-healthcheck-*` 容器。
- `pentestVM` 是既有测试容器，不得删除；只清理本轮在其中创建的临时 workspace、DB、report、probe 文件。
- 不删除基础镜像、不删除用户原有配置、不清理非本轮创建的容器。
- 清理前后各记录一次 `docker ps -a --filter "name=cairn-dispatch-"` 与 `docker ps -a --filter "name=cairn-startup-healthcheck-"`，便于确认无遗留测试垃圾。

## 最终交付清单

- v2 server schema migration。
- v2 server models/API/export。
- worker inventory API 与 dispatcher publish。
- Docker/SSH workspace 统一。
- report helper 与 prompt 更新。
- dispatcher requested worker/auto reason/timeout/conclude_now 调度。
- WebUI v2 控制面。
- README/spec/plan 同步。
- server tests、dispatcher tests、UI smoke、Docker Kali manual test 通过。

## 风险与取舍

### 风险 1: 一次性改动过大

对策：

- Phase 1-2 可先合 server contract。
- Phase 4-5 再合 workspace/report。
- Phase 6-8 最后接调度与 UI。

### 风险 2: report 检查把任务成功率拉低

对策：

- prompt 强约束 report。
- conclude fallback 补写 report。
- mock tests 覆盖缺失 report。
- failure report 保证可诊断。

### 风险 3: bootstrap 默认行为与“完全人工”冲突

对策：

- 已确认首版保留 bootstrap 自动行为，并在 UI/文档明确其与 `auto_reason` 分离。
- 若未来需要完全人工 bootstrap，本计划后续追加 `auto_bootstrap` phase。

### 风险 3.5: Docker 手测留下环境垃圾

对策：

- 使用既有 v0 Docker/Kali 测试容器 `pentestVM`，通过 `docker exec -it pentestVM zsh` 调试。
- 不触碰 `/home/kali/ctf`；只在 `/home/kali/cairn-workspaces` 或 `/tmp/cairn-v2-*` 创建可识别临时资源。
- 验收后按 Phase 12 清理清单删除本轮临时资源。
- 不删除 `pentestVM`、基础镜像、用户原有配置或非本轮创建的容器。

### 风险 4: worker inventory 与 dispatcher 生命周期

对策：

- dispatcher 启动即 publish。
- dispatcher config reload 或 environment refresh 后 publish。
- inventory 有 `updated_at`，UI 可显示 stale 状态。

### 风险 5: 路径安全

对策：

- 路径检查封装在 environment。
- 所有 report path 必须是 workspace 内绝对路径。
- 单测覆盖 prefix spoof 与 `..`。
