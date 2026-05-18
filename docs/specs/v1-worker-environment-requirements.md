# Cairn Worker Environment Requirements v1

日期：2026-05-18

状态：当前需求基线。取代 `docs/plan/v0-worker-environment-refactor-plan.html` 与 `docs/plan/v0-worker-environment-execution-plan.md` 中关于 snapshot、兼容旧 `container.*`、server 持久化 LLM 配置的设计。

## 1. 核心判断

Cairn 的边界应是：

- server 管“任务与地点”。
- dispatcher 管“兵力与凭据”。

换言之，project 绑定 execution environment；worker/profile 决定用什么模型、什么 provider、什么 secret。environment 只描述命令在哪里运行，不描述模型如何调用。

## 2. v0 已完成的 SSH 大改动

v0 已经证明 SSH backend 可行，主要成果如下：

- dispatcher 任务层已从 `ContainerManager` 解耦到 `WorkEnvironment`。
- 已有 Docker backend adapter，旧 Docker 路径可作为地点 backend。
- 已有 SSH backend：通过 OpenSSH 命令连接远端，准备 workspace，安装/调用远端 `cairn-runner`，支持 stdout/stderr 回流、timeout、cancel、cleanup。
- server 已有 environment CRUD、environment healthcheck、project 创建时选择 environment 的 UI/API。
- dispatcher 已能从 server 拉取 SSH environments，并按 project `environment_id` 选择 backend。
- Pi worker 已能在远端 workspace 下生成私有 `.cairn/pi/<worker>` 配置并运行。
- 已用 `pentestVM` over SSH 跑通过 dispatcher 拉起 Pi agent worker 的端到端调用。

v0 的主要问题是职责混叠：server environment 中混入了 `pi_model`、`pi_base_url`、`pi_api_key`、`pi_provider_api` 等 worker/profile 配置；execution plan 还保留了 snapshot 与兼容旧配置的思路。v1 需收口。

## 3. 目标分工

### server.db

server 持久化用户与项目事实：

- projects
- work environments
- project -> environment binding
- environment healthcheck result

server 不持久化 LLM API key，不持久化 provider profile，不负责选择 worker。

### dispatcher config files

dispatcher 配置运行时与 worker fleet：

- dispatcher runtime：server URL、poll interval、timeouts、并发上限。
- scheduling policy：worker priority、max_running、task_types、allowed_environments。
- workers：worker 类型、profile 引用、任务能力。
- profiles：model、base_url、provider_api、context_window、API key。

配置文件分三类：

- `dispatch.example.yaml`：唯一可提交的样例配置；不得包含真实 secret。
- `dispatch.dev.yaml`：本机开发配置；必须 gitignored；可放 mock/dev 场景的具体参数。
- `dispatch.local.yaml`：个人私密配置；必须 gitignored；可直接放真实 LLM API key。

不再使用 `dispatch.yaml` 作为主配置文件名。运行 dispatcher 时应显式选择 `--config dispatch.dev.yaml` 或 `--config dispatch.local.yaml`。

### environment

environment 只描述执行地点：

- `id`
- `label`
- `backend`: `ssh` 或 `docker`
- `ssh_command`
- `workspace_root`
- `harness`
- `cleanup`
- `terminal`

environment 不包含 `model/base_url/provider_api/api_key`。

### worker/profile

worker/profile 描述模型能力与凭据：

- `profile.id`
- `profile.type`
- `profile.model`
- `profile.base_url`
- `profile.provider_api`
- `profile.api_key`
- `profile.context_window`

MVP 不引入 secret ref 体系。`api_key` 可直接写在 gitignored 的 `dispatch.dev.yaml` 或 `dispatch.local.yaml` 中。server、run log、project metadata、UI 静态文件均不得出现 API key 明文。

## 4. 不做 snapshot

v1 MVP 不做 `environment_snapshot_json`。

理由：

- 当前仍未形成稳定用户数据契约，审计复现不是 MVP 主目标。
- snapshot 会制造第二个 environment truth source。
- 执行真相应唯一：`project.environment_id -> current server environment`。

若以后需要审计，可新增只含非敏感地点信息的历史记录；不得把 snapshot 用作调度输入。

## 5. 不做兼容

v1 不兼容 v0 schema。

理由：

- 当前还未完成 MVP，无需为了临时实现保留双语义。
- 兼容旧 `container.*` 与 server-side `pi_*` 会继续污染边界。
- 破坏式改干净，比在错误抽象上迁就更低成本。

允许保留 `docker-default` 作为内置地点，但它必须仍是 environment，而非 worker/profile 配置容器。

## 6. Healthcheck 分层

healthcheck 必须拆成两类。

### server environment healthcheck

server 只能检查地点：

- SSH 是否免交互可连。
- `workspace_root` 是否可创建、读写、清理 probe 文件。
- `harness` 是否存在，例如 `pi --version`。
- runner 是否可安装或调用。
- terminal backend 是否存在；缺失时按 optional 标记。

server 不测试模型 endpoint，不读取 API key，不调用 LLM。

### dispatcher startup healthcheck

dispatcher 检查完整调度矩阵：

- environment 是否可用。
- worker 是否允许该 environment。
- profile 是否完整。
- API key 是否存在且不会被日志打印。
- worker + profile + environment 是否能执行模型 ping。

模型失败应归因到 dispatcher/profile/key，不归因到 server environment。

## 7. 调度规则

project 创建时选择 `environment_id`。

dispatcher 执行时：

1. 读显式传入的 dispatcher config，例如 `dispatch.dev.yaml` 或 `dispatch.local.yaml`，解析 runtime、workers、profiles。
2. 从 server 拉取 environments。
3. 校验 `allowed_environments` 引用是否存在于 server environments 或内置 environments。
4. 对每个 project，按 `project.environment_id` 选择 environment。
5. 从允许该 environment 的 workers 中选择 worker。
6. 用 worker.profile 构造 agent 命令与注入环境变量。

若 project 绑定的 environment 不存在，dispatcher 不应回退到默认环境；应跳过该 project 并给出明确日志。

dispatcher 必须支持 environment 配置热刷新：

- server-side environments 是用户通过 Web UI 管理的地点配置，dispatcher 不应只在启动时读取一次。
- dispatcher 应定期拉取 `/environments`，检测新增、修改、删除。
- 新增 environment 应进入后续调度与 healthcheck 矩阵。
- 修改 environment 应对后续新任务生效；已经运行中的任务继续使用启动时的 handle，不被中途打断。
- 删除 environment 后，绑定该 environment 的 project 应被跳过并明确记录原因；不自动回退。
- 若 environment 正在运行任务，删除只影响新任务；清理由 dispatcher 按既有 running task 生命周期处理。

## 8. 配置形态

示例：

```yaml
server: "http://127.0.0.1:8000"

runtime:
  interval: 3
  max_workers: 4
  max_running_projects: 2
  max_project_workers: 2
  healthcheck_timeout: 10
  prompt_group: "default"

profiles:
  - id: pi-gpt54-local
    type: pi
    model: gpt-5.4
    base_url: "http://host.docker.internal:3000/v1"
    provider_api: "openai-completions"
    api_key: "<api-key>"
    context_window: 200000

workers:
  - name: pi-worker-1
    type: pi
    profile: pi-gpt54-local
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
    allowed_environments: [pentestvm]
```

server environment 示例：

```json
{
  "id": "pentestvm",
  "label": "pentestVM over SSH",
  "backend": "ssh",
  "ssh_command": "ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm",
  "workspace_root": "/home/kali/cairn-workspaces",
  "harness": "pi",
  "cleanup": {"completed_action": "stop"},
  "terminal": {"mode": "none"}
}
```

## 9. v1 MVP 改造清单

- 删除 server models/API/UI 中的 `pi_model`、`pi_base_url`、`pi_api_key`、`pi_provider_api`。
- 删除 server environment healthcheck 对模型 endpoint 的检查。
- 删除 `projects.environment_snapshot_json` 新写入逻辑；新 schema 不再依赖该字段。
- 删除 dispatcher 从 server environment 生成 worker env override 的逻辑。
- 在 dispatcher config 中新增 `profiles[]`，worker 改为引用 `profile`。
- 删除 `dispatch.yaml` 主文件概念；保留 `dispatch.example.yaml`、`dispatch.dev.yaml`、`dispatch.local.yaml` 三类配置。
- 确保只有 `dispatch.example.yaml` 可提交；`dispatch.dev.yaml` 与 `dispatch.local.yaml` 必须 gitignored。
- 修改 `allowed_environments` 校验：允许引用 server-side environment；启动后统一校验。
- 修改 Pi driver 输入：从 resolved profile 生成 `PI_MODEL`、`PI_BASE_URL`、`PI_PROVIDER_API`、`PI_API_KEY`。
- 更新 UI Environment 面板：只配置地点字段。
- 更新 New Project：只选择 environment，不选择 worker/profile。
- 增加 dispatcher environment registry 热刷新；不要求重启 dispatcher 才能使用 Web UI 新增/修改的 environment。
- Project detail 顶栏显示该 project 的 workspace 路径；SSH workspace 路径按 `workspace_root/project_id` 展示，Docker workspace 按 backend 能提供的路径展示。
- 更新测试：server 测地点与绑定；dispatcher 测 profile、secret、worker/environment matrix。

## 10. 验收标准

- server DB 不再新增或展示任何 LLM key/provider 字段。
- 创建 project 后只持久化 `environment_id`。
- repo 中不存在 `dispatch.yaml`；只有 `dispatch.example.yaml` 可提交。
- `dispatch.dev.yaml` 与 `dispatch.local.yaml` 被 gitignore 排除。
- Environment 面板可创建 SSH 地点并触发地点 healthcheck。
- Web UI 新增或修改 environment 后，dispatcher 无需重启即可用于后续调度。
- Project detail 顶栏显示当前 environment 与 workspace 路径。
- Dispatcher 可用 gitignored dispatcher config 中的 profile + API key 在 server 配置的 SSH environment 上运行 Pi worker。
- `allowed_environments` 能限制 worker 只跑指定地点。
- run log 不包含 API key 明文。
- 绑定缺失 environment 的 project 被跳过，不自动落到默认环境。
- Docker backend 若保留，只作为 environment backend；模型配置仍来自 profile。

## 11. 非目标

- 不做 snapshot。
- 不兼容 v0 临时 schema。
- 不做 server-side secret storage。
- 不做 secret ref/keychain/env-var 体系；MVP 直接使用 gitignored dispatcher config。
- 不做多 host 负载均衡。
- 不做 terminal 交互 UI；tmux/zellij 可作为后续增强。
