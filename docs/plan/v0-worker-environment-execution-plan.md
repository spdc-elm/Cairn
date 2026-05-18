# Worker Environment SSH-First 重构执行计划 v0

> 状态：历史执行计划。已被 `docs/specs/v1-worker-environment-requirements.md` 取代。
> v1 明确：server 管 project、environment、project-environment binding 与地点 healthcheck；dispatcher 管 runtime、worker 列表、profile、secret 引用与调度策略。
> v1 不做 snapshot，不兼容 v0 临时 schema，server environment 不再保存 LLM API/provider 配置。

依据：`docs/plan/v0-worker-environment-refactor-plan.html`

目标：把 Cairn 的 worker 运行地点从“dispatcher 完全控制 Docker 容器”重构为“project 绑定用户选择的 work environment”。SSH backend 为主路径；原 Docker 模式保留为兼容 backend。

MVP 边界：先做到 Phase 8。MVP 不能只停在 CLI；Web UI 至少要能配置 SSH 远程机器、触发 healthcheck、在创建 project 时选择 SSH 模式与目标机器，并能在 project/run 视图看到环境与运行输出。

## 当前已确认事实

- `projects` 表当前没有 `environment_id`，project 创建 API 也不接收环境选择。
- dispatcher 当前在初始化时直接创建 `ContainerManager`，任务层直接调用 `ensure_running()`、`write_text_file()`、`build_exec_process()`。
- 当前 Docker 容器是 project 级长活环境；任务通过 Docker exec 执行，stdout/stderr 被 dispatcher 回读并写 run log。
- `pi` driver 当前会生成 agent 配置文件，且可能包含 key；SSH 版必须把凭据模式显式化。
- 测试容器名为 `pentestVM`；容器内可作为远程环境测试。workspace 只能使用 `/home/kali/cairn-workspaces`、`/home/kali/.cairn` 等受控路径，严禁触碰 `/home/kali/ctf`。
- 测试模型使用 `gpt-5.4`。宿主侧 base URL 为 `http://localhost:3000`；从 Docker 容器内访问宿主服务时通常应使用 `http://host.docker.internal:3000`。若 Pi/OpenAI-compatible provider 需要 `/v1` 根路径，则配置为对应的 `/v1` 版本。
- 本次测试 key 是可报废 lab key，允许在本执行计划与本地 lab 配置中明文出现，便于复现实验。生产实现仍必须避免把 key 写入 run log、request.json、HTML/JS 静态文件或 UI 展示文本。

## 测试 LLM 配置约定

统一使用以下本地变量名。为方便 MVP 实验，此处直接写入可报废 lab key：

```bash
export CAIRN_TEST_PI_MODEL="gpt-5.4"
export CAIRN_TEST_PI_BASE_URL_HOST="http://localhost:3000"
export CAIRN_TEST_PI_BASE_URL_DOCKER="http://host.docker.internal:3000"
export CAIRN_TEST_PI_API_KEY="<lab-key-redacted>"
export CAIRN_TEST_PI_PROVIDER_API="openai-completions"
```

也可以创建仅用于本机测试的 `dispatch_ssh_lab.local.yaml` 或 `.env.local` 放置同一 key；这些本地 lab 文件不应作为生产样例。

若 endpoint 实际要求 OpenAI `/v1` root，则把 base URL 改为：

```bash
export CAIRN_TEST_PI_BASE_URL_HOST="http://localhost:3000/v1"
export CAIRN_TEST_PI_BASE_URL_DOCKER="http://host.docker.internal:3000/v1"
```

在 `pentestVM` SSH 测试中，远端进程运行在容器内；因此 inject 模式默认使用 `CAIRN_TEST_PI_BASE_URL_DOCKER`。若 `host.docker.internal` 在当前 Docker 环境不可解析，需在测试容器启动参数中加 host-gateway，或改用容器可达的 Docker bridge gateway 地址。

## 总体阶段

1. Phase 0: 建立测试实验场与回归基线。
2. Phase 1: 抽象执行环境接口，Docker backend 行为保持不变。
3. Phase 2: 重构配置模型，引入 `environments[]`，兼容旧 `container.*`。
4. Phase 3: Project 绑定 environment，补 server API 与 UI 选择。
5. Phase 4: SSH healthcheck MVP。
6. Phase 5: SSH runner 与进程生命周期。
7. Phase 6: Pi harness 与凭据模式。
8. Phase 7: Dispatcher 接入 SSH backend，跑通端到端。
9. Phase 8: 观测、清理、文档与安全收口。
10. Phase 9: tmux/zellij 终端复用二期。

每个阶段均需满足：可独立 review、可独立测试、失败可回滚。

---

## Phase 0: 测试实验场与回归基线

### 目标

先把“能测什么”立住。此阶段不改业务逻辑，只准备本地 Docker 回归与 `pentestVM` SSH 实验环境。

### 可落实步骤

1. 记录当前 Docker 行为基线。
   - `dispatch_mock.yaml` 仍作为 Docker backend 回归样本。
   - 记录 startup healthcheck、一次 dispatcher loop、run log 写入路径。

2. 为 `pentestVM` 配置 SSH 服务。
   - 容器内安装或确认 `openssh-server`。
   - 生成 host keys。
   - 配置 public key 登录。
   - 启动 `sshd`。
   - 不修改 `/home/kali/ctf`。

3. 为本机 SSH 建测试 Host。
   - 优先使用 OpenSSH `ProxyCommand` 经 `docker exec` 转发到容器内 sshd。
   - 好处：即使容器未暴露 22 端口，也能测试 SSH 协议、pubkey、sshd、远端命令。

4. 创建远端 workspace 根目录。
   - 使用 `/home/kali/cairn-workspaces`。
   - 使用 `/home/kali/.cairn` 存放 runner、临时 harness 配置。
   - 明确禁止清理逻辑越过这两个前缀。

5. 验证测试 LLM 连通性。
   - 宿主侧先测 `http://localhost:3000` 是否可达。
   - 容器内再测 `http://host.docker.internal:3000` 是否可达。
   - 若容器内不可达，记录 Docker 网络修复步骤，再进入后续 phase。

### 建议测试命令

生成临时 key：

```bash
ssh-keygen -t ed25519 -N "" -f /tmp/cairn_pentestvm_ed25519
```

容器内准备 sshd：

```bash
docker exec -it pentestVM zsh
```

容器内执行：

```bash
sudo apt update
sudo apt install -y openssh-server
sudo ssh-keygen -A
mkdir -p /home/kali/.ssh /home/kali/cairn-workspaces /home/kali/.cairn/bin
chmod 700 /home/kali/.ssh
```

回到宿主，将公钥放入容器：

```bash
docker cp /tmp/cairn_pentestvm_ed25519.pub pentestVM:/tmp/cairn_pentestvm_ed25519.pub
docker exec pentestVM zsh -lc 'cat /tmp/cairn_pentestvm_ed25519.pub >> /home/kali/.ssh/authorized_keys && chmod 600 /home/kali/.ssh/authorized_keys && chown -R kali:kali /home/kali/.ssh /home/kali/cairn-workspaces /home/kali/.cairn'
docker exec -d pentestVM /usr/sbin/sshd -D -e
```

创建临时 SSH config：

```sshconfig
Host cairn-pentestvm
  HostName 127.0.0.1
  User kali
  IdentityFile /tmp/cairn_pentestvm_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  UserKnownHostsFile /tmp/cairn_pentestvm_known_hosts
  ProxyCommand docker exec -i pentestVM ncat 127.0.0.1 22
```

连接测试：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config -o BatchMode=yes cairn-pentestvm 'whoami; pwd; command -v pi; pi --version; mkdir -p /home/kali/cairn-workspaces/.healthcheck && echo ok > /home/kali/cairn-workspaces/.healthcheck/probe && cat /home/kali/cairn-workspaces/.healthcheck/probe'
```

LLM endpoint reachability：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "$CAIRN_TEST_PI_BASE_URL_HOST"
ssh -F /tmp/cairn_pentestvm_ssh_config -o BatchMode=yes cairn-pentestvm 'curl -sS -o /dev/null -w "%{http_code}\n" http://host.docker.internal:3000 || true'
```

注意：这里只测网络可达，不在命令行打印 API key。

Docker 旧路径回归：

```bash
uv run --project cairn cairn dispatch --config dispatch_mock.yaml --startup-healthcheck-only
```

### 验收标准

- `ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm true` 返回 0。
- 远端 `command -v pi` 成功，或明确记录缺失并安装。
- `/home/kali/cairn-workspaces` 可写。
- 宿主可访问测试 LLM endpoint；`pentestVM` 内可通过 `host.docker.internal` 或记录的替代 gateway 访问同一 endpoint。
- `/home/kali/ctf` 未被读取、写入、删除或作为 workspace。
- 旧 Docker startup healthcheck 仍可运行，若失败需记录失败原因，不得混入 SSH 改动。

### Self check

- 我是否把任何测试文件写到了 `/home/kali/ctf`？
- 如果容器没有端口映射，我是否仍能通过 `ProxyCommand docker exec ... ncat 127.0.0.1 22` 走 SSH 协议？
- host key 是否仅写入 `/tmp/cairn_pentestvm_known_hosts`，而非污染用户全局 known_hosts？
- 我是否避免在 curl/ssh 命令、日志或文档中打印测试 API key？
- 如果 `host.docker.internal` 不通，我是否先解决网络可达性，而不是让后续 healthcheck 背锅？
- 当前回归失败是既有 Docker 环境问题，还是我引入的新问题？

---

## Phase 1: 抽象执行环境接口

### 目标

把任务层从 `ContainerManager` 解耦。Docker backend 先包一层，行为完全不变。

### 可落实步骤

1. 新增模块：
   - `cairn/src/cairn/dispatcher/runtime/environments/base.py`
   - `cairn/src/cairn/dispatcher/runtime/environments/docker.py`
   - `cairn/src/cairn/dispatcher/runtime/environments/__init__.py`

2. 定义协议：
   - `EnvironmentHandle`
   - `EnvironmentState`
   - `ManagedProcessLike`
   - `WorkEnvironment`

3. 让 `DockerEnvironment` 组合现有 `ContainerManager`。
   - 方法名可以先兼容旧语义。
   - `ensure_running(project_id)` 等价为 `prepare_project(project_id)`。
   - `write_text_file()`、`build_process()` 仍调用现有 Docker 实现。

4. 修改任务层函数签名。
   - `run_reason_task(..., container_manager, ...)` 改为 `run_reason_task(..., environment, ...)`。
   - 局部变量从 `container_name` 改为 `handle` 或 `target_name`。
   - 日志里可暂保留 `container` metadata，但新增 `environment_id`。

5. 暂不改调度策略。
   - `DispatcherLoop.__init__` 仍只创建一个 Docker environment。
   - 确保所有 mock 与 Docker 路径行为不变。

### 测试方式

单元测试建议新增：

```text
cairn/tests/dispatcher/runtime/test_docker_environment_adapter.py
cairn/tests/dispatcher/tasks/test_environment_protocol.py
```

测试点：

- Fake environment 能捕获 `write_text_file()` 的 path/content。
- Fake process 返回 stdout/stderr 后，`run_worker_process()` 能写 run log。
- Docker adapter 调用顺序与旧 `ContainerManager` 一致。

命令：

```bash
uv run --project cairn python -m pytest cairn/tests/dispatcher/runtime cairn/tests/dispatcher/tasks
uv run --project cairn cairn dispatch --config dispatch_mock.yaml --startup-healthcheck-only
```

若项目尚未引入 pytest，本阶段需把 pytest 加入 dev dependency，或先用 `unittest` 写等价测试。不要留下“计划测试但无法运行”的空壳。

### 验收标准

- Docker backend 代码路径仍能通过 mock startup healthcheck。
- 任务层不再 import `ContainerManager`，只依赖 `WorkEnvironment` 或兼容协议。
- run log 中至少能区分 `backend=docker` 与原容器名。
- 无 SSH 代码混入本阶段。

### Self check

- 我是否只是改名，没有改变 Docker 行为？
- 任务层是否还直接引用 Docker SDK 类型？
- Fake environment 是否足以模拟文件写入、进程执行、取消？
- 如果本阶段回滚，是否只影响抽象层，不影响 server/UI？

---

## Phase 2: 配置模型重构

### 目标

引入 `environments[]`，但兼容旧 `container.*`。此阶段不改变 project schema。

### 可落实步骤

1. 扩展 `config.py`。
   - 新增 `EnvironmentConfig` union。
   - `SshEnvironmentConfig`
   - `DockerEnvironmentConfig`
   - SSH MVP 支持两种声明方式：
     - structured: `host/user/port/ssh_config/identity_file`
     - command: `ssh_command`
   - `CredentialsMode = remote | inject | merge`
   - `CleanupPolicy`
   - `TerminalMode = none | tmux | zellij`

2. 兼容旧配置。
   - 如果 YAML 有旧 `container.*` 且无 `environments[]`，自动生成：

```yaml
environments:
  - id: docker-default
    label: Docker Default
    backend: docker
    ...
```

3. 扩展 worker 配置。
   - 新增 `allowed_environments: list[str] | None`。
   - 为空时表示允许全部环境，保持旧行为。

4. 配置校验。
   - environment id 唯一。
   - worker 引用的 environment 必须存在。
   - SSH environment 的 workspace_root 不得是 `/home/kali/ctf`，也不得是 `/`、`/home`、`/home/kali`。
   - `ssh_command` 用 `shlex.split()` 解析为 argv，禁止通过 shell 执行；healthcheck 与 runner 只能在 argv 后追加受控 remote command。
   - `credentials.mode=remote` 时允许 worker env 缺少 baseurl/key，但 driver 需声明支持 remote credentials。

### 测试方式

测试文件建议：

```text
cairn/tests/dispatcher/test_config_environments.py
```

测试点：

- 旧 `dispatch_mock.yaml` 可加载，并生成 `docker-default`。
- 新 SSH config 可加载。
- 重复 environment id 报错。
- `workspace_root: /home/kali/ctf` 报错。
- `ssh_command: "ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm"` 可加载，并被解析为 argv。
- `ssh_command` 中出现明显 shell 控制符时拒绝，或至少不经 shell 执行。
- worker allowed environment 不存在时报错。
- `credentials.mode=remote` 下 pi worker 可不填 `PI_API_KEY`，但 `inject` 下仍要求。

命令：

```bash
uv run --project cairn python -m pytest cairn/tests/dispatcher/test_config_environments.py
uv run --project cairn cairn dispatch --config dispatch_mock.yaml --startup-healthcheck-only
```

### 验收标准

- 旧配置无需修改即可运行。
- 新配置能表达 `pentestVM`：

```yaml
environments:
  - id: pentestvm-ssh
    label: "pentestVM over SSH"
    backend: ssh
    ssh_command: "ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm"
    workspace_root: /home/kali/cairn-workspaces
    harness: pi
    credentials:
      mode: inject

workers:
  - name: pi-ssh-test
    type: pi
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
    allowed_environments: [pentestvm-ssh]
    env:
      PI_MODEL: "${CAIRN_TEST_PI_MODEL}"
      PI_BASE_URL: "${CAIRN_TEST_PI_BASE_URL_DOCKER}"
      PI_API_KEY: "${CAIRN_TEST_PI_API_KEY}"
      PI_PROVIDER_API: "${CAIRN_TEST_PI_PROVIDER_API}"
```

### Self check

- 旧 `container.*` 是否仍被支持？
- SSH 配置是否泄露 secret 到 server 可见字段？
- workspace guard 是否能拦住 `/home/kali/ctf` 与过宽路径？
- `ssh_command` 是否只是连接命令，而不是任意 shell 脚本入口？
- `credentials.mode` 是 environment 级语义，还是被误塞成 worker 类型特例？

---

## Phase 3: Environment 管理 API、Web 面板与 Project 绑定

### 目标

让用户能在 Web UI 配置 SSH 远程机器、触发 healthcheck，并在创建 project 时选择 SSH 模式与目标机器。server 存储 project 绑定；dispatcher 后续按绑定选择 backend。

### 可落实步骤

1. DB schema 增量迁移。
   - `projects.environment_id TEXT`
   - `projects.environment_snapshot_json TEXT`
   - 新增 `work_environments` 表，保存用户通过 Web UI 配置的环境：
     - `id`
     - `label`
     - `backend`
     - `ssh_command`
     - `workspace_root`
     - `harness`
     - `credentials_mode`
     - `created_at`
     - `updated_at`
     - `last_health_status`
     - `last_healthcheck_json`
   - 现有数据库需兼容自动迁移。

2. API model 更新。
   - `CreateProjectRequest.environment_id: str | None`
   - `ProjectMeta.environment_id`
   - `ProjectMeta.environment`
   - environment 只返回 redacted metadata，不返回 key。

3. Create project 逻辑。
   - 若未传 `environment_id`，用 config 默认环境。
   - 若传入不存在环境，返回 400。
   - 创建时写入 snapshot，至少包含 id、label、backend、workspace_root redacted form、credentials mode。

4. 新增环境列表 API。
   - `GET /environments`
   - `POST /environments`
   - `PUT /environments/{environment_id}`
   - `DELETE /environments/{environment_id}`
   - `POST /environments/{environment_id}/healthcheck`
   - 返回 label、id、backend、workspace redacted、credentials mode、last health status；不返回 secret。

5. UI 增加 Environment 面板。
   - 入口放在现有 settings 或新增 sidebar/header 按钮中。
   - 可新增 SSH machine，最小字段：
     - Label
     - SSH command，例如 `ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm`
     - Workspace root，默认 `/home/kali/cairn-workspaces`
     - Harness，MVP 固定或默认 `pi`
     - Credentials mode，MVP 先 `inject | remote`
   - 面板可点击 Healthcheck，展示分层结果：connect、workspace、harness、model、stream。
   - MVP 面板可以接收并保存可报废 lab key，便于测试；字段默认 password 显示，API 返回 redacted。
   - 生产模式应支持从 dispatcher env 或本地 secret provider 读 key，避免 server 持久化明文 key。

6. UI New Project modal 增加 execution mode 与 environment 下拉。
   - Execution mode: `Docker` / `SSH`。
   - 选择 SSH 时只展示 SSH environments。
   - 默认选最近 healthcheck 成功的 SSH environment；没有可用环境时给出配置入口。
   - Project card 显示 environment badge。
   - Project detail header 显示当前 environment。

### 测试方式

API 测试建议：

```text
cairn/tests/server/test_project_environment.py
```

测试点：

- 不传 environment 时使用默认值。
- 传合法 environment 时 project detail 返回绑定。
- 传非法 environment 返回 400。
- 新增 SSH environment 后，`GET /environments` 可见。
- `POST /environments/{id}/healthcheck` 返回分层结果并更新 last health status。
- ProjectSummary 返回 environment badge 所需字段。
- snapshot 不含 secret。

手工 API：

```bash
uv run --project cairn cairn serve --db-path /tmp/cairn-env-test.db
curl -sS http://127.0.0.1:8000/environments
curl -sS -X POST http://127.0.0.1:8000/environments \
  -H 'content-type: application/json' \
  -d '{"label":"pentestVM","backend":"ssh","ssh_command":"ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm","workspace_root":"/home/kali/cairn-workspaces","harness":"pi","credentials_mode":"inject"}'
curl -sS -X POST http://127.0.0.1:8000/environments/pentestvm/healthcheck
curl -sS -X POST http://127.0.0.1:8000/projects \
  -H 'content-type: application/json' \
  -d '{"title":"ssh env smoke","origin":"start","goal":"finish","environment_id":"pentestvm"}'
```

UI 手测：

- 打开首页。
- 打开 Environment 面板，新增 `pentestVM` SSH machine。
- 在面板中点击 Healthcheck，可看到 connect/workspace/harness/model/stream 结果。
- New Project modal 可见 execution mode 与 environment selector。
- 选择 SSH mode 后可选 `pentestVM`。
- 创建项目后卡片显示所选 environment。
- 进入项目详情后 header 显示所选 environment。

### 验收标准

- 旧项目无 environment 时不会崩；dispatcher 可回退默认环境。
- 新项目必须能绑定指定 environment。
- 生产模式下 server 不存明文模型 key；MVP lab 模式可存可报废测试 key，但 API 列表与详情默认 redacted。
- UI 默认不展示 secret；可提供显式“show test key”调试开关。
- Web UI 能完成远程机器 CRUD 与 healthcheck，不需要用户手改 YAML 才能测试 SSH MVP。

### Self check

- environment 是 project 属性，而不是 worker 属性，我是否保持了这个边界？
- 修改 project title/status/reopen 是否保留 environment？
- 删除 project 是否不会误删远端 workspace？清理由 dispatcher 负责，不由 server 直接做。
- UI 是否在 environment 配置缺失时仍能打开历史项目？
- Environment 面板是否只保存 SSH 连接与 workspace 信息，而不保存 LLM key？
- Healthcheck API 是同步短任务还是后台任务？若同步，是否有明确超时？

---

## Phase 4: SSH Healthcheck MVP

### 目标

实现 SSH environment 的健康检查，先不接入正式任务执行。

### 可落实步骤

1. 新增 `SshEnvironment` skeleton。
   - 支持 OpenSSH config。
   - 支持 Web UI 保存的 `ssh_command`。执行时用 `shlex.split(ssh_command)` 生成基础 argv，再追加远程命令；不得 `shell=True`。
   - 优先用系统 `ssh` / `scp` / `sftp` subprocess，原因是它天然支持 agent、ProxyCommand、用户 ssh config。
   - Python-native SSH 可作为后续替换，不在第一刀引入。

2. 实现 healthcheck 分层。
   - connect: `ssh -o BatchMode=yes host true`
   - workspace: `mkdir -p workspace_root`，写读删 probe 文件。
   - harness: `command -v pi`、`pi --version`、`python3 --version`、`command -v timeout`、`command -v setsid`
   - model: inject 模式用 `gpt-5.4`、`CAIRN_TEST_PI_BASE_URL_DOCKER`、`CAIRN_TEST_PI_API_KEY` 做一次轻量 Pi ping；remote 模式用远端已有配置。
   - stream: 运行短命令，同时产生 stdout/stderr。

3. healthcheck 输出结构化。
   - 每层 status: ok | failed | skipped
   - duration_ms
   - redacted command
   - stdout/stderr preview

4. CLI 接入。
   - 扩展 `cairn dispatch --startup-healthcheck-only`，显示 environment + worker matrix。
   - SSH 失败不得挂死；超时必须可控。

5. Web API 接入。
   - `POST /environments/{id}/healthcheck` 调用同一 healthcheck service。
   - 返回完整分层结果给 Environment 面板。
   - API 必须超时；MVP 可同步返回，后续再改后台任务。

### 测试方式

容器实验：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config -o BatchMode=yes cairn-pentestvm true
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'mkdir -p /home/kali/cairn-workspaces && echo ok > /home/kali/cairn-workspaces/probe && cat /home/kali/cairn-workspaces/probe && rm /home/kali/cairn-workspaces/probe'
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'command -v pi && pi --version'
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'curl -sS -o /dev/null -w "%{http_code}\n" http://host.docker.internal:3000 || true'
```

自动测试建议：

```text
cairn/tests/dispatcher/runtime/test_ssh_healthcheck.py
```

可先用 fake ssh command runner 测：

- connect success/failure。
- workspace write failure。
- missing pi。
- model endpoint unreachable。
- missing `host.docker.internal` route。
- stdout/stderr 分流。
- timeout。

真实集成测试用 env gate：

```bash
CAIRN_SSH_TEST_HOST=cairn-pentestvm \
CAIRN_SSH_TEST_CONFIG=/tmp/cairn_pentestvm_ssh_config \
uv run --project cairn python -m pytest cairn/tests/integration/test_ssh_environment.py
```

### 验收标准

- `pentestVM` SSH healthcheck 可跑通。
- 故意移除 key 或改错 host 时，healthcheck 快速失败且错误清晰。
- 缺 `pi` 时报告 harness missing，不进入任务派发。
- 模型层显示当前使用 `gpt-5.4` 与 redacted base URL；不显示 API key。
- Web UI Environment 面板能触发同一 healthcheck，并显示分层结果。
- healthcheck 不读取或写入 `/home/kali/ctf`。

### Self check

- 我是否依赖 `docker exec` 的只是测试 ProxyCommand，而不是生产 SSH backend？
- SSH 命令是否全部使用 BatchMode，避免卡在密码提示？
- 错误信息是否足够让用户知道是 key、host key、workspace、还是 harness 问题？
- model healthcheck 失败时，是否明确区分“SSH 成功但 endpoint 不通”和“API key/model 不可用”？
- preview 是否 redacted，未打印 key？

---

## Phase 5: SSH Runner 与进程生命周期

### 目标

让 SSH backend 具备原 Docker exec 的核心元能力：启动命令、实时回读 stdout/stderr、超时、取消、返回 exit code。

### 可落实步骤

1. 设计远端 runner。
   - 路径：`/home/kali/.cairn/bin/cairn-runner`
   - 语言：Python stdlib。
   - 输入：request JSON。
   - 输出：worker stdout/stderr 原样转发。
   - 状态文件：workspace 下 `.cairn/runs/<run_id>/state.json`。

2. Runner execute。
   - 读取 `argv`、`env`、`timeout_seconds`、`cwd`。
   - `subprocess.Popen(..., start_new_session=True)`。
   - 写入 pid、pgid、started_at。
   - 双线程或 select 转发 stdout/stderr。
   - 超时后 kill process group。

3. Runner cancel。
   - 读取 state.json。
   - kill `-pgid`。
   - 若进程已退出，返回 success。

4. Dispatcher 侧 `SshManagedProcess`。
   - `start()`：上传 request，启动 ssh subprocess 执行 runner。
   - `communicate()`：读本地 ssh stdout/stderr。
   - `kill()`：另开 ssh 命令执行 runner cancel，再等待原 ssh 进程退出。
   - `cancel(reason)`：记录 reason 后调用 kill。

5. 路径安全。
   - 所有 project workspace 必须是 `workspace_root/project_id`。
   - 删除或清理前 `realpath` 必须以 `workspace_root` 开头。
   - hard block `/home/kali/ctf`。

### 测试方式

远端 runner 手测：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'mkdir -p /home/kali/.cairn/bin /home/kali/cairn-workspaces/p-runner-smoke'
```

上传 runner 后：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm '/home/kali/.cairn/bin/cairn-runner --version'
```

stdout/stderr 测试：

```json
{
  "run_id": "smoke-stream",
  "cwd": "/home/kali/cairn-workspaces/p-runner-smoke",
  "argv": ["python3", "-c", "import sys,time; print('out1'); print('err1', file=sys.stderr); time.sleep(0.2); print('out2')"],
  "env": {},
  "timeout_seconds": 5,
  "kill_after_seconds": 1
}
```

取消测试：

```json
{
  "run_id": "smoke-cancel",
  "cwd": "/home/kali/cairn-workspaces/p-runner-smoke",
  "argv": ["python3", "-c", "import time; time.sleep(60)"],
  "env": {},
  "timeout_seconds": 120,
  "kill_after_seconds": 1
}
```

验残留：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm "pgrep -af 'time.sleep\\(60\\)' || true"
```

自动测试：

```text
cairn/tests/dispatcher/runtime/test_ssh_process.py
cairn/tests/integration/test_ssh_runner_lifecycle.py
```

### 验收标准

- stdout 与 stderr 分别进入 run log。
- 超时返回 `timed_out=True`，exit code 语义与 Docker 版兼容，建议 124 或 137。
- cancel 后远端无残留子进程。
- kill 本地 ssh 进程不是主要取消机制；必须调用 remote cancel。
- request/state 文件不放在 `/home/kali/ctf`。

### Self check

- 如果 worker fork 子进程，kill process group 能否杀干净？
- runner 自己异常时，dispatcher 是否能拿到 stderr 与非零 returncode？
- `request.json` 是否可能长期留 secret？inject 模式是否有删除策略？
- 路径清理是否有 realpath guard？

---

## Phase 6: Pi Harness 与凭据模式

### 目标

先以 Pi agent 为唯一 SSH harness 跑通。实现 `remote` 与 `inject` 两种凭据模式。

### 可落实步骤

1. Pi driver 增加 execution context。
   - 当前 driver 只接收 `WorkerConfig`。
   - 需新增上下文，至少包含 backend、credentials mode、workspace path、agent config dir。

2. inject 模式。
   - dispatcher 将 `PI_MODEL`、`PI_BASE_URL`、`PI_API_KEY`、`PI_PROVIDER_API` 注入远端进程。
   - MVP 测试值：
     - `PI_MODEL=${CAIRN_TEST_PI_MODEL}`，即 `gpt-5.4`
     - `PI_BASE_URL=${CAIRN_TEST_PI_BASE_URL_DOCKER}`，通常为 `http://host.docker.internal:3000` 或 `/v1` 变体
     - `PI_API_KEY=${CAIRN_TEST_PI_API_KEY}`
     - `PI_PROVIDER_API=${CAIRN_TEST_PI_PROVIDER_API}`
   - Pi models config 写到 project run 私有目录，例如：
     - `/home/kali/cairn-workspaces/<project_id>/.cairn/pi/<worker>/models.json`
   - 设置 `PI_CODING_AGENT_DIR` 指向该目录。
   - 任务结束按策略删除含 secret 的临时 config，或至少 chmod 600。

3. remote 模式。
   - dispatcher 不传 baseurl/key。
   - driver 不生成 models.json，直接调用远端已有 `pi` 配置。
   - healthcheck 用远端配置跑 `pi` ping。

4. lab 默认用 inject。
   - 用户已说明原有 piagent 无配置，可任意覆盖。
   - 仍建议写入 `/home/kali/cairn-workspaces/<project_id>/.cairn/pi`，不要污染全局。

5. Secret redaction。
   - argv 不含 key。
   - run log 不含 key。
   - healthcheck command preview 不含 key。
   - request/state 如含 env，落盘前对 secret 单独加权限或拆到 env file。

### 测试方式

inject 模式单元测试：

- build argv 不含 `PI_API_KEY` 字符串。
- env 包含 `PI_API_KEY`。
- models.json 写入路径在 workspace 内。
- run log metadata 不含 key。

remote 模式单元测试：

- 缺 `PI_API_KEY` 不报 config validation error。
- 不生成 models.json。
- healthcheck 命令不注入 key。

实验容器：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'rm -rf /home/kali/cairn-workspaces/pi-smoke && mkdir -p /home/kali/cairn-workspaces/pi-smoke'
```

若使用 inject，先在宿主导出 `CAIRN_TEST_PI_API_KEY` 等变量，再由 dispatcher 注入远端进程。为本次 MVP，也允许在 `dispatch_ssh_lab.local.yaml` 或 Environment 面板中填写这枚可报废 lab key；不要写入生产样例或 Web 静态文件。

Pi ping 集成测试应使用 `gpt-5.4`：

```bash
CAIRN_TEST_PI_MODEL=gpt-5.4 \
CAIRN_TEST_PI_BASE_URL_DOCKER=http://host.docker.internal:3000 \
CAIRN_TEST_PI_PROVIDER_API=openai-completions \
uv run --project cairn python -m pytest cairn/tests/integration/test_pi_ssh_credentials.py
```

若 endpoint 要求 `/v1`，改用 `http://host.docker.internal:3000/v1`。

### 验收标准

- `credentials.mode=inject` 下，Pi 可用注入配置运行。
- `credentials.mode=remote` 下，dispatcher 不要求也不传 key。
- `gpt-5.4` 测试调用成功，或失败时能明确给出 base URL 不通、认证失败、模型不可用三类之一。
- secret 不出现在 argv、run log、healthcheck preview；UI 默认 redacted，只有显式调试开关可显示 lab key。
- 所有 pi agent 生成物在 workspace 或 `/home/kali/.cairn`，不碰 `/home/kali/ctf`。

### Self check

- 我是否把“远端已有配置”和“dispatcher 注入配置”混成了隐式行为？
- remote 模式是否真的没有传 secret？
- inject 模式是否能在任务结束后清理或保护 secret 文件？
- Pi driver 改动是否破坏 Docker backend 的旧行为？

---

## Phase 7: Dispatcher 接入 SSH Backend

### 目标

让 project 按 `environment_id` 派发到对应 backend，跑通 bootstrap/reason/explore。

### 可落实步骤

1. Environment registry。
   - Dispatcher 启动时构建 `environment_id -> WorkEnvironment`。
   - 默认环境用于旧项目与未指定项目。

2. Project dispatch 选择环境。
   - `_try_dispatch_project()` 获取 `project.project.environment_id`。
   - 若环境不可用，记录 skip reason，不 claim intent。
   - worker selection 增加 `allowed_environments` 过滤。

3. Cleanup 改环境感知。
   - completed/stopped cleanup 调用对应 backend。
   - Docker backend 保持 stop/remove。
   - SSH backend stop=cancel active runs + keep workspace；remove=删 workspace，需 path guard。

4. Startup healthcheck matrix。
   - 显示每个 environment、worker、task type 是否可用。
   - 至少一个可用组合时 dispatcher 可启动。
   - 对某 project，只能选择其绑定 environment 下可用 worker。

5. Run log metadata。
   - 增加 `environment_id`、`backend`、`workspace` redacted。
   - 保留 `container` 字段兼容旧 UI，但不要依赖它。

### 测试方式

Docker 回归：

```bash
uv run --project cairn cairn dispatch --config dispatch_mock.yaml --startup-healthcheck-only
```

SSH mock command 集成：

- 通过 Web UI Environment 面板或 API 创建 `pentestVM` SSH 环境。
- 使用 mock worker 或 pi worker 的 no-model test mode。
- 创建 project 绑定 `pentestVM` 环境。
- dispatcher `--once` 能在远端 workspace 写 graph snapshot 并执行命令。

端到端流程：

```bash
uv run --project cairn cairn serve --db-path /tmp/cairn-ssh-e2e.db
curl -sS -X POST http://127.0.0.1:8000/projects \
  -H 'content-type: application/json' \
  -d '{"title":"ssh e2e","origin":"start","goal":"finish","environment_id":"pentestvm"}'
uv run --project cairn cairn dispatch --config dispatch_ssh_lab.yaml --once
```

远端验证：

```bash
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'find /home/kali/cairn-workspaces -maxdepth 4 -type f | sort | head -50'
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'find /home/kali/ctf -maxdepth 0 -print'
```

### 验收标准

- 绑定 SSH environment 的 project 不创建 Docker 容器。
- 绑定 Docker environment 的 project 行为与旧逻辑一致。
- worker selection 尊重 `allowed_environments`。
- Web UI 创建的 SSH environment 与 YAML 内置 environment 都能被 dispatcher registry 读取，冲突时有明确优先级。
- project stopped 后远端活跃进程被取消。
- completed keep 模式保留 workspace。
- `/home/kali/ctf` 未被使用。

### Self check

- claim intent 之前是否已确认 environment 可用？
- environment 不可用时是否会反复刷屏？是否有 retry backoff？
- cleanup 是否按 project 绑定的 backend，而非全局 backend？
- 多 project 共用同一 SSH host 时，并发上限是否生效？

---

## Phase 8: 观测、清理、文档与安全收口

### 目标

让 SSH backend 可用、可解释、可运维，而不只是能跑。

### 可落实步骤

1. UI 观测。
   - Environment 面板可新增、编辑、删除 SSH machine。
   - Environment 面板可直接填写 SSH command；MVP 不强迫用户拆成 host/user/port。
   - Environment 面板可填写本次可报废 lab key；保存与返回列表时默认 redacted。
   - Environment 面板可触发 healthcheck，并展示 connect/workspace/harness/model/stream 分层状态。
   - Project card 显示 environment badge。
   - Run detail 显示 backend、environment、workspace、healthcheck result。
   - 环境不可用时给明确 warning。

2. 安全文档。
   - SSH backend 是可信远程环境。
   - Docker backend 提供容器隔离，但远程 daemon 权限大。
   - credentials mode 三种语义。
   - workspace guard 与 `/home/kali/ctf` 禁区说明。

3. 清理工具。
   - CLI 增加列出 environment 状态。
   - 可选：`cairn dispatch --environment-healthcheck-only`。
   - 可选：`cairn dispatch --cleanup-environment pentestvm-ssh --project p12`，默认 dry-run。

4. Secret audit。
   - grep run logs 与 workspace，确认测试 key 只出现在允许的 lab 配置位置，不出现在 run log、healthcheck preview、静态 JS/HTML。
   - request.json 或 env file 权限检查。

### 测试方式

Secret grep 示例：

```bash
rg "sk-|PI_API_KEY|OPENAI_API_KEY|ANTHROPIC_AUTH_TOKEN" "$HOME/.local/share/cairn/runs" || true
ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm 'find /home/kali/cairn-workspaces -type f -name "*.json" -maxdepth 6 -print0 | xargs -0 grep -n "PI_API_KEY\\|sk-" || true'
rg "<lab-key-redacted>" cairn/src/cairn/server/static || true
```

清理 dry-run：

```bash
uv run --project cairn cairn dispatch --config dispatch_ssh_lab.yaml --environment-healthcheck-only
```

UI 手测：

- 打开 Environment 面板，新增 `pentestVM`。
- 点击 `pentestVM` healthcheck，能看到分层结果；模型层使用 `gpt-5.4` 且 key 不可见。
- New Project 选择 SSH mode 与 `pentestVM` machine。
- 环境 badge 可见。
- Run panel 可看到 stdout/stderr。
- 停止项目后 UI 不再显示 running，远端无残留进程。

### 验收标准

- 用户能从 UI 判断 project 跑在哪个环境。
- 用户能从 UI 配置远程机器并触发 healthcheck。
- 用户能在创建 project 时选择 SSH mode 与目标机器。
- healthcheck 失败能定位到连接、workspace、harness、模型、输出回读哪一层。
- secret audit 通过：可报废 lab key 只允许出现在本执行计划、本地 lab 配置或显式保存的 environment secret 存储处。
- 清理命令默认 dry-run，不会误删 workspace。

### Self check

- 我是否给了用户足够信息，而不是只显示 “failed”？
- 远程机器配置面板是否足以让用户不手改 YAML 完成 MVP 测试？
- 清理命令是否有二次确认或 dry-run？
- secret 是否可能通过 stderr preview、run log、静态前端文件泄漏？
- 文档是否明确 SSH backend 不等价 Docker 隔离？

---

## Phase 9: tmux / zellij 终端复用二期

### 目标

增强 UI 交互感，但不改变第一版执行语义。

### 可落实步骤

1. terminal mode config。

```yaml
terminal:
  mode: zellij
  session_template: "cairn-{project_id}"
  pane_per_task: true
  capture_interval_ms: 500
```

2. Runner 支持 terminal adapter。
   - none: 现有 raw process。
   - tmux: new-window / split-pane / capture-pane。
   - zellij: session/pane 管理。

3. 输出源统一。
   - 第一版仍以 stdout/stderr stream 为真源。
   - terminal capture 作为 UI 附加视图。

4. UI 附加终端视图。
   - 只读 capture。
   - 后续再考虑输入控制。

### 测试方式

- 远端无 tmux/zellij 时，healthcheck 标记 terminal optional missing，不影响 raw SSH backend。
- zellij mode 下任务可见 pane。
- capture 内容与 run log 至少前缀一致。
- stop project 后 pane/session 按策略保留或关闭。

### 验收标准

- raw SSH backend 不受 terminal mode 影响。
- terminal capture 不泄漏 secret。
- 用户可从 UI 看到 worker 近实时输出。

### Self check

- 我是否把 terminal 复用当成执行可靠性的依赖？不应如此。
- 用户输入控制是否会破坏任务可复现性？第一版不做。
- terminal session cleanup 是否会误杀用户已有 session？

---

## 里程碑验收表

| 里程碑 | 必须可证明 |
| --- | --- |
| M0 | `pentestVM` 可通过 SSH config 免密连接；Docker 旧路径有基线 |
| M1 | 任务层不再直接依赖 Docker SDK 类型；Docker 行为不变 |
| M2 | 旧配置兼容，新 `environments[]` 可校验 |
| M3 | Web UI 可配置 SSH machine，Project 可绑定 environment，UI 可选择 SSH mode 与机器 |
| M4 | SSH healthcheck 能定位失败层，Web UI 可触发并展示结果 |
| M5 | SSH runner 支持 stdout/stderr、timeout、cancel，无远端残留 |
| M6 | Pi inject/remote 凭据模式可控，secret 不泄漏 |
| M7 | SSH project 端到端跑通，Docker project 仍可跑 |
| M8 | MVP 完成：远程机器面板、healthcheck、project 选择、run 输出、清理安全、文档完整 |
| M9 | tmux/zellij 只读终端视图可选 |

## 最小可交付切片

本轮 MVP 明确交付到 M8。可以砍掉以下内容：

- 暂不做 `merge` 凭据模式。
- 暂不做 tmux/zellij。
- 暂不做多 SSH host 负载均衡。
- 暂不做后台异步 healthcheck 队列；MVP 可同步执行并设置严格超时。
- 暂不做复杂 SSH 表单解析；MVP 可直接保存用户输入的 `ssh_command`，但必须 `shlex.split` 后 argv 执行。

保留不可砍内容：

- Web UI Environment 面板：新增/编辑/删除 SSH machine。
- Web UI healthcheck：connect/workspace/harness/model/stream 分层结果。
- New Project：可选择 SSH mode 与机器。
- SSH 免密 healthcheck。
- workspace guard，尤其 `/home/kali/ctf` 禁区。
- stdout/stderr 回读。
- remote cancel 杀进程组。
- Docker backend 回归。
- secret redaction；本次可报废 lab key 可明文出现在执行计划与本地 lab 配置，但运行日志、healthcheck preview、静态前端文件仍不得泄漏。
- `gpt-5.4` 测试路径，base URL 支持宿主 `localhost:3000` 与容器内 `host.docker.internal:3000`。
