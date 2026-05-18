# Worker Environment v1 执行计划

依据：`docs/specs/v1-worker-environment-requirements.md`

状态：待执行。

原则：不为 v0 临时设计保兼容。凡与 v1 边界冲突者，直接删除或重写。server 管任务与地点；dispatcher 管兵力与凭据。

## 0. 目标边界

### 必须达成

- server environment 只保存执行地点字段。
- server 不保存 LLM model/provider/base URL/API key。
- project 只绑定 `environment_id`，不再写 `environment_snapshot_json`。
- dispatcher 配置使用三类文件：
  - `dispatch.example.yaml`：唯一可提交样例，不含真实 key。
  - `dispatch.dev.yaml`：本机开发配置，gitignored。
  - `dispatch.local.yaml`：个人真实配置，gitignored。
- 不再使用 `dispatch.yaml` 主文件名。
- dispatcher config 新增 `profiles[]`，worker 引用 profile。
- profile 可在 gitignored config 中直接写 `api_key`。
- `allowed_environments` 引用 server-side environment 或内置 environment，校验发生在拉取 environment 后。
- dispatcher 支持 server environment 热刷新。
- Project detail 顶栏显示 workspace 路径。
- SSH Pi worker 仍能通过 dispatcher 拉起并完成端到端任务。

### 明确不做

- 不做 snapshot。
- 不兼容 v0 schema。
- 不做 server-side secret storage。
- 不做 `api_key_ref`、keychain、env-var secret resolver。
- 不做 terminal 交互 UI。

## Phase 0: 基线与护栏

### 目标

先立住“不能回头”的护栏：确认当前文件命名、secret 暴露面、测试入口。

### 步骤

1. 确认 root 配置文件只剩：
   - `dispatch.example.yaml`
   - `dispatch.dev.yaml`
   - `dispatch.local.yaml`

2. 更新 `.gitignore`：
   - ignore `dispatch.dev.yaml`
   - ignore `dispatch.local.yaml`
   - 不再需要 ignore `dispatch.yaml`，因为该文件不应存在。

3. 清理可提交文件中的真实 key：
   - `dispatch.example.yaml`
   - `docs/specs/*.md`
   - `docs/plan/*.md`
   - `docs/specs/*.html`

4. 建立 v1 失败用例清单：
   - server environment body 若含 `pi_api_key` 等字段，应拒绝或忽略，不进入 DB。
   - 新 project 创建后不写 snapshot。
   - `dispatch.yaml` 不存在。
   - dispatcher 不接受未声明 profile 的 worker。

### 测试

```bash
find . -maxdepth 1 -type f -name 'dispatch*.yaml' -print | sort
git check-ignore -v dispatch.dev.yaml dispatch.local.yaml
rg -n 'sk-[A-Za-z0-9]|pi_api_key|api_key_ref|environment_snapshot_json' dispatch.example.yaml docs/specs/v1-worker-environment-requirements.md docs/specs/v1-worker-environment-presentation.html
```

### 验收

- root 下无 `dispatch.yaml`。
- `dispatch.dev.yaml` 与 `dispatch.local.yaml` 被 gitignore 命中。
- 可提交样例和 v1 文档无真实 key。

### Self check

- 我是否把本机真实 key 写进了可提交文件？
- 我是否仍在文档中把 `dispatch.yaml` 当主配置？
- 我是否把 dev/local 误描述为可提交？

## Phase 1: Server schema 收口

### 目标

server DB 只保存任务与地点。删除 v0 中混入 environment 的 LLM/provider 字段与 snapshot 写入路径。

### 步骤

1. 修改 `cairn/src/cairn/server/db.py`。
   - 新建 schema 中 `work_environments` 只保留：
     - `id`
     - `label`
     - `backend`
     - `ssh_command`
     - `workspace_root`
     - `harness`
     - `cleanup_json` 或等价结构
     - `terminal_json` 或等价结构
     - `created_at`
     - `updated_at`
     - `last_health_status`
     - `last_healthcheck_json`
   - `projects` 不再新增或写入 `environment_snapshot_json`。
   - 由于不兼容 v0，迁移逻辑不需要保留旧列读取语义；测试 DB 可直接按新 schema 初始化。

2. 修改 `cairn/src/cairn/server/models.py`。
   - `WorkEnvironmentPublic` 删除：
     - `credentials_mode`
     - `pi_model`
     - `pi_base_url`
     - `pi_api_key`
     - `pi_provider_api`
   - `WorkEnvironmentUpsert` 同步删除上述字段。
   - 新增或保留：
     - `cleanup`
     - `terminal`
     - `workspace_path_pattern` 如需要展示。
   - `ProjectMeta` 只保留 `environment_id` 与 redacted/live environment；不返回 snapshot。

3. 修改 `cairn/src/cairn/server/services.py`。
   - `environment_row_to_public()` 不再处理 key redaction。
   - 删除 `environment_snapshot()`。
   - `project_meta_from_row()` 不再解析 snapshot。
   - `validate_environment_body()` 只校验地点：
     - SSH 必须有 `ssh_command`。
     - SSH `workspace_root` 不得是 `/`、`/home`、`/home/kali`、`/home/kali/ctf`，不得在 `/home/kali/ctf/` 下。
     - Docker 只允许内置 `docker-default` 或明确设计的 docker backend 字段。

4. 修改 `cairn/src/cairn/server/routers/projects.py`。
   - create project 只写 `environment_id`。
   - 不写 `environment_snapshot_json`。
   - `GET /projects` 与 `GET /projects/{id}` 返回 live environment。
   - 若 environment 被删除，project 仍可展示 `environment_id`，但 environment 字段可为 null 或错误状态；不要崩溃。

### 测试设计

新增/更新：

```text
cairn/tests/server/test_environment_schema_v1.py
cairn/tests/server/test_project_environment_binding_v1.py
```

覆盖：

- default `docker-default` 存在且不含模型/key 字段。
- `POST /environments` 创建 SSH 地点后，DB row 不含 `pi_*` 列语义。
- 提交带 `pi_api_key` 的 body 返回 422 或 400。
- 创建 project 只持久化 `environment_id`。
- project detail 不含 `environment_snapshot_json`。
- 删除 environment 后，历史 project detail 不崩溃，且不回退默认环境。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests/server -v
uv run --project cairn python -m compileall cairn/src/cairn/server cairn/tests/server
```

### 验收

- `rg -n 'pi_model|pi_base_url|pi_api_key|pi_provider_api|environment_snapshot_json' cairn/src/cairn/server` 无 v1 运行路径引用。
- server API 不返回 LLM/provider/key 字段。
- project 创建后 DB 中无 snapshot 写入。

### Self check

- 我是否只是隐藏了 key 字段，而不是删除边界？
- 我是否仍让 server 知道模型 endpoint？
- 删除 environment 后，我是否错误回退到了默认 environment？

## Phase 2: Environment API 与 UI 收口

### 目标

Web UI 只配置地点；Project 创建只选地点；Project detail 顶栏显示 environment 与 workspace。

### 步骤

1. 修改 `cairn/src/cairn/server/routers/environments.py`。
   - CRUD body 只接收地点字段。
   - 删除 `_worker_env_from_public()`。
   - healthcheck 调用不传 worker/model env。

2. 修改 `POST /environments/{id}/healthcheck`。
   - SSH 检查：
     - connect
     - workspace
     - runner
     - harness
     - stream
     - terminal optional
   - 不检查 model endpoint。
   - 不读取 API key。

3. 修改 `cairn/src/cairn/server/static/index.html`。
   - Environment panel 删除：
     - credentials mode
     - PI_MODEL
     - PI_BASE_URL
     - PI_PROVIDER_API
     - PI_API_KEY
   - Environment form 保留：
     - label
     - backend
     - ssh_command
     - workspace_root
     - harness
     - cleanup
     - terminal
   - New Project 只选择 environment。
   - Project detail 顶栏显示：
     - environment label/id/backend
     - workspace path
   - workspace path 规则：
     - SSH: `workspace_root/project_id`
     - Docker: backend 提供的 workspace 字段；若无则展示 container/project target。

4. 前端状态清理。
   - 删除 environment form 中的 `pi_*` 默认值。
   - 删除保存时 `delete body.pi_api_key` 逻辑。
   - 删除 UI 中任何 key input。

### 测试设计

Server tests：

- environment create body 不接受 `pi_api_key`。
- healthcheck response checks 不含 model endpoint check。
- healthcheck result 可写入 `last_healthcheck_json`。

Browser smoke：

- Environment panel 中找不到 `PI_API_KEY`、`PI_BASE_URL`。
- 创建 SSH environment。
- 点击 healthcheck，显示 connect/workspace/harness/runner/stream。
- New Project modal 可选 environment。
- Project detail 顶栏显示 workspace path。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests/server -v
uv run --project cairn cairn server --db /tmp/cairn-v1-ui.db --host 127.0.0.1 --port 8765
```

Browser 手工/自动检查：

```text
打开 http://127.0.0.1:8765
Environment panel -> create SSH env -> healthcheck
New Project -> choose env -> create
Project detail -> verify topbar workspace
```

### 验收

- UI 中无 LLM key/provider 输入。
- server healthcheck 不要求 model/base URL/API key。
- Project detail 可解释“此 project 将在哪个 workspace 跑”。

### Self check

- 我是否让用户在 Environment 面板中继续填模型？
- healthcheck 失败是否能区分地点失败，而非模型失败？
- workspace path 是否可能误导用户为真实已创建路径？若尚未创建，应标明 planned/current。

## Phase 3: Dispatcher config v1

### 目标

dispatcher config 从 worker env 散字段升级为 profiles + workers。profile 描述模型能力；worker 描述兵力与调度属性。

### 步骤

1. 修改 `cairn/src/cairn/dispatcher/config.py`。
   - 新增 `ProfileConfig`：
     - `id`
     - `type`
     - `model`
     - `base_url`
     - `provider_api`
     - `api_key`
     - `context_window`
   - `WorkerConfig` 新增必填 `profile: str | None`。
   - 对非 mock worker，要求 `profile` 存在。
   - mock worker 可无 profile。
   - profile `type` 必须与 worker `type` 匹配。
   - 删除 v0 的 server environment credential 逻辑相关校验。
   - 保留 `common_env` 仅用于非 secret 的通用环境变量；不要用它承载 API key。

2. 配置文件命名。
   - CLI 默认不再隐式找 `dispatch.yaml`。
   - 若用户未传 `--config`，给出明确错误并提示：
     - `--config dispatch.dev.yaml`
     - `--config dispatch.local.yaml`
   - `dispatch.example.yaml` 更新为 v1 schema，不含真实 key。

3. profile 到 driver env 的解析。
   - 建立 helper：
     - `resolve_worker_env(worker, profile) -> dict[str, str]`
   - Pi:
     - `PI_MODEL`
     - `PI_BASE_URL`
     - `PI_PROVIDER_API`
     - `PI_API_KEY`
     - optional `PI_MODEL_CONTEXT_WINDOW`
   - Codex:
     - `CODEX_MODEL`
     - `CODEX_BASE_URL`
     - `OPENAI_API_KEY`
     - optional `CODEX_HEALTHCHECK_STREAM`
   - ClaudeCode:
     - `ANTHROPIC_MODEL`
     - `ANTHROPIC_BASE_URL`
     - `ANTHROPIC_AUTH_TOKEN`

4. 删除 `api_key_ref` 概念。
   - 代码、docs、tests 中不得出现 v1 运行逻辑依赖 `api_key_ref`。

### 测试设计

新增/更新：

```text
cairn/tests/dispatcher/test_config_profiles_v1.py
cairn/tests/dispatcher/test_worker_profile_resolution.py
```

覆盖：

- `dispatch.example.yaml` 可加载。
- worker 引用缺失 profile -> 报错。
- worker/profile type 不匹配 -> 报错。
- pi profile 解析为正确 `PI_*` env。
- codex profile 解析为正确 `CODEX_*`/`OPENAI_API_KEY` env。
- mock worker 可无 profile。
- `api_key` 为空时非 mock worker 报错。
- `api_key` 不出现在 startup healthcheck command preview。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests/dispatcher -v
uv run --project cairn python -m compileall cairn/src/cairn/dispatcher cairn/tests/dispatcher
```

### 验收

- `DispatchConfig.load(Path("dispatch.example.yaml"))` 通过。
- `rg -n 'api_key_ref|CAIRN_TEST_PI_API_KEY' cairn/src/cairn/dispatcher docs/specs/v1-worker-environment-requirements.md` 无 v1 运行路径引用。
- 非 mock worker 不再直接要求用户写 `PI_*`/`OPENAI_API_KEY` 到 worker env。

### Self check

- 我是否把 profile 做成了第二个 worker？
- 我是否仍让 server environment override worker env？
- command preview 是否可能打印 key？

## Phase 4: Dispatcher environment registry 与热刷新

### 目标

server-side environment 是用户通过 Web UI 管理的地点。dispatcher 必须定期刷新，不靠重启。

### 步骤

1. 修改 `DispatcherLoop` environment registry。
   - 初始启动：
     - 构建内置 environments，例如 `docker-default`。
     - 从 server 拉取 environments。
     - 合并为 `environment_id -> WorkEnvironment`。
   - 周期刷新：
     - 每个 loop tick 或固定 refresh interval 拉取 `/environments`。
     - 检测新增、修改、删除。
     - 新增：构建并放入 registry。
     - 修改：关闭旧 idle environment handle/backend 对象，替换新对象；运行中 task 保持已有 handle。
     - 删除：从 registry 移除；不影响已运行 task handle；新任务跳过。

2. environment identity 与变更检测。
   - 使用 server environment 的 normalized dict hash。
   - 不包含 last_healthcheck，避免 healthcheck 更新触发无意义重建。

3. `allowed_environments` 校验。
   - YAML 加载时只校验格式。
   - dispatcher 拉取 server environments 后校验引用。
   - 引用缺失：
     - startup healthcheck 应显示 missing environment。
     - 对具体 project，应跳过并明确日志。
   - 不回退默认 environment。

4. 清理 v0 server worker env override。
   - 删除 `environment_worker_env_overrides`。
   - 删除 `_server_worker_env()`。
   - `_server_ssh_environment_config()` 只构造地点字段。

### 测试设计

新增：

```text
cairn/tests/dispatcher/test_environment_registry_refresh.py
cairn/tests/dispatcher/test_allowed_environments_server_side.py
```

覆盖：

- 启动时从 fake server 拉取 environment。
- 新增 environment 后无需重启即可被 dispatch 看到。
- 修改 `workspace_root` 后，新任务使用新 workspace。
- 已运行任务使用旧 handle，不被修改打断。
- 删除 environment 后，新任务跳过并记录原因。
- `allowed_environments` 可引用 server-side environment。
- 缺失 environment 不回退 docker-default。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests/dispatcher -p 'test_environment_registry_refresh.py' -v
uv run --project cairn python -m unittest discover -s cairn/tests/dispatcher -p 'test_allowed_environments_server_side.py' -v
```

### 验收

- Web UI 新增 environment 后，dispatcher 不重启即可调度后续 project。
- 删除 environment 不导致 project 跑到默认 Docker。
- registry refresh 不杀正在运行任务。

### Self check

- 我是否在 task 运行中替换了它的 handle？
- environment healthcheck 更新是否会误触发 backend 重建？
- 缺失 environment 是否被静默吞掉？

## Phase 5: Healthcheck 分层重写

### 目标

地点健康与模型健康分开。server 只测地点；dispatcher startup healthcheck 测完整矩阵。

### 步骤

1. 修改 `SshEnvironment.run_healthcheck()`。
   - 默认只测：
     - connect
     - workspace
     - runner
     - harness
     - stream
     - terminal optional
   - 删除通过 `PI_BASE_URL` curl model endpoint 的逻辑。
   - 返回结果字段不含 credentials mode。

2. 修改 server healthcheck router。
   - 不传 worker env。
   - 不需要 `include_secrets=True`。
   - healthcheck result 可直接展示地点状态。

3. 修改 dispatcher startup healthcheck。
   - 对每个可用 environment + worker/profile 组合执行 driver healthcheck。
   - 使用 profile resolved env。
   - model ping 失败归因到 profile/key/endpoint。
   - command preview 与 stderr preview 做 key redaction。

4. Redaction。
   - 建立统一 redaction helper：
     - redact exact API key
     - redact `Authorization: Bearer ...`
     - redact JSON `apiKey`
   - 用于 startup healthcheck report、run log metadata、error message preview。

### 测试设计

新增/更新：

```text
cairn/tests/dispatcher/runtime/test_ssh_healthcheck_v1.py
cairn/tests/dispatcher/runtime/test_startup_healthcheck_profiles.py
cairn/tests/dispatcher/test_redaction.py
cairn/tests/server/test_environment_healthcheck_v1.py
```

覆盖：

- server SSH healthcheck 不需要 profile/api_key。
- server healthcheck checks 中无 `model` check。
- dispatcher startup healthcheck 使用 profile env 做 model ping。
- API key 不出现在 command、stdout/stderr preview、failure summary。
- profile endpoint 错误不会标记 server environment failed。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests -p '*healthcheck*' -v
uv run --project cairn python -m unittest discover -s cairn/tests -p '*redaction*' -v
```

### 验收

- Environment 面板 healthcheck 可在无 LLM key 情况下运行。
- `--startup-healthcheck-only` 能显示 environment + worker/profile matrix。
- 所有 healthcheck 输出无 API key 明文。

### Self check

- 我是否又把模型 ping 塞回 server？
- healthcheck “ok” 是否只表示地点 ok，而不是模型 ok？
- failure summary 是否泄露 key 的片段？

## Phase 6: Worker drivers 与 Pi profile 接入

### 目标

worker driver 不关心配置文件格式，只接收 resolved worker env。Pi 继续支持 SSH 远端运行，但 key 来源改为 dispatcher profile。

### 步骤

1. 修改 worker 调度路径。
   - 在提交 task 前，将 worker + profile resolve 为 runtime worker。
   - `WorkerConfig.env` 可保留作为非 secret 额外 env，但 profile 生成的 key/env 优先级应明确：
     - profile env 覆盖 worker env 中同名模型/key字段。

2. 修改 Pi driver。
   - 不再依赖 server environment credential mode。
   - profile 有 `api_key` 时生成 `models.json`。
   - 如果未来支持 remote config，可另起 feature，不进入 v1 MVP。
   - `models.json` 写入 workspace `.cairn/pi/<worker>`，权限 `600`。

3. 修改 Codex/ClaudeCode driver。
   - 从 resolved profile env 获取 key/model/base_url。
   - healthcheck 命令不打印 key。

4. run log metadata。
   - 可记录：
     - worker name
     - profile id
     - model
     - backend
     - environment_id
     - workspace
   - 不记录：
     - api_key
     - full env
     - generated models.json content

### 测试设计

新增/更新：

```text
cairn/tests/dispatcher/workers/test_pi_profile_driver.py
cairn/tests/dispatcher/tasks/test_run_log_redaction.py
```

覆盖：

- Pi execute argv 不含 API key。
- 远端 env 包含 API key，但 run log 不写 key。
- generated `models.json` content 不进入 logs。
- `profile_id` 出现在 run metadata。
- `workspace` 出现在 run metadata。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests/dispatcher/workers -v
uv run --project cairn python -m unittest discover -s cairn/tests/dispatcher/tasks -v
```

### 验收

- Pi worker 用 profile 成功构造命令。
- run log 与 startup report 无 key。
- server 不参与 worker credential 注入。

### Self check

- 我是否把 profile 解析散落到每个 task 文件？
- API key 是否被作为命令行参数传给远端？
- `models.json` 是否落在 workspace 受控目录？

## Phase 7: Project workspace 展示与调度一致性

### 目标

用户在 Project 页面能看见实际执行地点与 workspace；dispatcher 使用同一规则准备 workspace。

### 步骤

1. 统一 workspace 计算。
   - 为 `WorkEnvironment` 增加只读方法或 helper：
     - `workspace_for(project_id) -> str | None`
   - SSH 返回 `workspace_root/project_id`。
   - Docker 返回容器内工作目录或 target label。

2. Server Project detail。
   - ProjectMeta 或 environment public 增加可展示 workspace path。
   - 若 workspace 尚未创建，字段命名可为 `planned_workspace`。

3. UI 顶栏。
   - 显示 environment label/backend。
   - 显示 workspace path。
   - path 支持复制。
   - path 长时不撑破布局。

4. Dispatcher run metadata。
   - 每次 task log metadata 写入同一 workspace path。

### 测试设计

Server/UI：

- Project detail response 包含 workspace path 或足够字段让 UI 计算。
- SSH project 顶栏显示 `/home/kali/cairn-workspaces/<project_id>`。
- 长 workspace_root 不造成 UI 重叠。

Dispatcher：

- `EnvironmentHandle.workspace` 与 UI 展示规则一致。

### 测试命令

```bash
uv run --project cairn python -m unittest discover -s cairn/tests/server -p '*project*' -v
uv run --project cairn python -m unittest discover -s cairn/tests/dispatcher -p '*environment*' -v
```

Browser 检查：

```text
打开 Project detail
确认顶栏存在 environment 与 workspace
缩窄 viewport，确认 path wrap 正常
```

### 验收

- 用户不看日志也知道 project 将在哪个 workspace 运行。
- UI 展示路径与 dispatcher 实际路径一致。

### Self check

- 我是否让 server 猜测了 dispatcher 私有路径？
- Docker workspace 是否表达不清？
- path 是否可能误导为已完成创建？

## Phase 8: 端到端验证

### 目标

证明 v1 边界下仍能跑通 SSH Pi worker，同时无 server-side key、无 snapshot、无兼容回退。

### 准备

1. 确认 `pentestVM` SSH：
   - `ssh -F /tmp/cairn_pentestvm_ssh_config -o BatchMode=yes cairn-pentestvm true`
   - `/home/kali/cairn-workspaces` 可写。
   - `pi --version` 可用。

2. 创建 `dispatch.local.yaml`。
   - 使用 v1 profiles。
   - 直接写可报废 lab key。
   - 确认 gitignored。

3. 启动临时 server DB。
   - 使用 `/tmp/cairn-v1-e2e.db`。

4. 通过 API 或 UI 创建 SSH environment。
   - 不传任何 `pi_*` 字段。

### SSH transport e2e with mock worker

目标：使用上一阶段已准备好的 `pentestVM` Docker 容器作为真实 SSH 远端地点，先证明 SSH transport、remote workspace、runner、dispatcher 调度链路成立；worker 可用 mock，避免每次消耗真实模型。

说明：这里的 mock 只指 worker driver/mock LLM 行为，不是 mock SSH 环境。SSH endpoint、workspace、runner、stdout/stderr 回流都应走真实 `pentestVM`。

步骤：

```bash
uv run --project cairn cairn server --db /tmp/cairn-v1-e2e.db --host 127.0.0.1 --port 8765
curl -sS -X POST http://127.0.0.1:8765/environments \
  -H 'Content-Type: application/json' \
  -d '{"id":"pentestvm","label":"pentestVM","backend":"ssh","ssh_command":"ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm","workspace_root":"/home/kali/cairn-workspaces","harness":"pi"}'
curl -sS -X POST http://127.0.0.1:8765/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"v1 ssh mock","origin":"start","goal":"finish","environment_id":"pentestvm"}'
uv run --project cairn cairn dispatch --config dispatch.dev.yaml --once
```

验收：

- project 绑定 `pentestvm`。
- run log 有 environment/workspace metadata。
- 不创建 Docker project container。
- remote workspace 写入 graph snapshot 或 run artifacts。

### SSH Pi real e2e

目标：证明 profile + API key 由 dispatcher 注入，server 不知道 key。

步骤：

```bash
uv run --project cairn cairn dispatch --config dispatch.local.yaml --startup-healthcheck-only
uv run --project cairn cairn dispatch --config dispatch.local.yaml --once
```

验收：

- startup healthcheck 至少一个 `pentestvm + pi-worker` 组合 healthy。
- project 完成 bootstrap/reason/explore 中至少一条真实 worker 调用。
- server DB 中无 API key 明文。
- run logs 中无 API key 明文。
- remote `/home/kali/ctf` 未被访问或写入。

### 热刷新 e2e

目标：证明无需重启 dispatcher。

步骤：

1. dispatcher 持续运行。
2. Web UI 新增 environment `pentestvm-2`。
3. 创建 project 绑定 `pentestvm-2`。
4. 观察 dispatcher 自动刷新 registry 并调度。
5. 修改 `pentestvm-2.workspace_root`。
6. 新建 project，确认新 workspace_root 生效。
7. 删除 environment。
8. 新建或恢复绑定该 id 的 project，确认跳过且不回退。

验收：

- 新增/修改 environment 不需要重启 dispatcher。
- 运行中 task 不被中断。
- 删除 environment 后新任务跳过。

### Secret audit

```bash
rg -n '<real-key-fragment>|sk-[A-Za-z0-9]' cairn/src docs dispatch.example.yaml
rg -n '<real-key-fragment>' "$HOME/.local/share/cairn" || true
sqlite3 /tmp/cairn-v1-e2e.db "select * from work_environments;" | rg '<real-key-fragment>|sk-' || true
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'find /home/kali/cairn-workspaces -maxdepth 6 -type f -print0 | xargs -0 grep -n "<real-key-fragment>" || true'
```

注意：`dispatch.local.yaml` 与远端私有 Pi models 文件可含 key；它们必须是 gitignored/受控临时文件，不得进入 server、docs、run log。

## Phase 9: 全量回归与收尾

### 目标

把 v1 从“能跑”收成“可交付”。

### 步骤

1. 全量单测。

```bash
uv run --project cairn python -m unittest discover -s cairn/tests -v
```

2. 编译检查。

```bash
uv run --project cairn python -m compileall cairn/src cairn/tests
```

3. 静态扫描。

```bash
git diff --check
rg -n 'pi_api_key|pi_base_url|pi_provider_api|environment_snapshot_json|api_key_ref|dispatch_mock.yaml|dispatch.yaml' cairn/src docs/specs/v1-worker-environment-requirements.md docs/specs/v1-worker-environment-presentation.html
```

4. 配置文件检查。

```bash
find . -maxdepth 1 -type f -name 'dispatch*.yaml' -print | sort
git check-ignore -v dispatch.dev.yaml dispatch.local.yaml
```

5. UI smoke。
   - Environment panel。
   - Healthcheck。
   - New Project。
   - Project detail workspace topbar。
   - Run log metadata。

6. 更新 docs。
   - `docs/specs/v1-worker-environment-requirements.md` 若实现中有必要澄清，先反映到 spec。
   - 本 plan 勾勒实际通过的测试命令与结果。

### 验收

- 所有单测通过。
- compileall 通过。
- diff check 通过。
- v1 禁用字段无运行路径引用。
- UI smoke 无 console error。
- SSH Pi e2e 通过。

### Self check

- 我是否因为测试方便又把 key 塞回 server？
- 是否还有“临时兼容旧设计”的分支？
- 是否所有 failure 都能归因到 server 地点、dispatcher profile、worker driver、远端 harness 中的某一层？

## 最终交付清单

- `docs/specs/v1-worker-environment-requirements.md` 保持为需求真相源。
- `docs/plan/v1-worker-environment-execution-plan.md` 记录执行步骤与测试设计。
- server environment API/UI 只含地点字段。
- dispatcher v1 config 支持 profiles。
- environment registry 支持热刷新。
- project detail 显示 workspace。
- SSH Pi worker 端到端通过。
- 无 API key 泄露到可提交文件、server DB、run log。
