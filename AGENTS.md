# Cairn Agent Instructions

本文件约束仓库根目录下的协作方式。若子目录另有 `AGENTS.md`，以更近者为准。

## 工作原则

- 先读后判。凡涉及当前代码、文档、接口、测试、目录结构的判断，必须先用 `rg`/定向读取核实。
- 不凭惯例替代事实。若需求、spec、plan、实现状态不明，先查源文件；仍不明再问用户。
- 如无必要，勿增实体。优先简洁、清晰、可维护；新增抽象必须能降低真实复杂度。
- 反迎合。先审查问题 framing、关键假设与失败路径，再决定赞同、修正或反驳。
- 默认用中文简洁沟通；实现、命令、代码标识保持仓库原语言与风格。

## 产品本体定义

涉及 Cairn 架构、spec、plan 或实现时，先保持下列概念边界：

- `Fact/Intent DAG` 是用户心智模型：Cairn 把从 `origin` 到 `goal` 的问题解决过程表达成一张探索 DAG。
- `Fact` 不是 atomic assertion，也不是所有观察的最小集合。`Fact` 是一次探索步骤进入黑板后的结果节点，承载该次探索对用户有用的摘要；一次 successful concluded exploration intent 默认产出一个 primary result fact。
- `Intent` 是图上的探索边，也是工作订单：它从已有 facts 出发，描述下一步要探索什么。`Intent` 不应持有 live worker lease、heartbeat、cancel/retry runtime state。
- `ExecutionRun` 是运行层原子：某个 worker 在某个环境中实际执行了一次。worker identity、session、lease、heartbeat、stdout/stderr、returncode、timeout、cancel、retry 等运行事实应挂在 execution 上。
- `ExecutionEvent` 是实时输出源：Output、conversation transcript、tool/message stream 应从 execution events 投影，不应由 `Fact` / `Intent` 反向拼日志。
- `Branch` 表达 fork/resume 的 session 连续线；多轮 fork/resume 是同一 branch 下追加 executions。
- `Artifact` 保存证据和大产物：report、transcript、scan output、screenshot、文件等。细粒度观察和原始证据优先进入 event/artifact，不把图层拆成许多小 fact。
- 不要为了“候选事实/待审核断言”提前引入 `Claim` 等新核心概念。除非 spec 明确要求长期审核 workflow，否则用 `Fact` + `ExecutionRun` + `Artifact` 足够。

## 开发模式

本仓库以三段式开发为主，但不强制所有改动都走完整流程。

### 0. Lightweight Mode

入口：简单、小范围、目标明确的功能优化或修复。

目的：避免为低风险改动引入过重流程。

要求：

- 可采用 Codex 内置的 plan -> execute 模式：先给简短执行计划，经确认或合理判断后直接实现。
- 不必产出 `docs/specs/` 或 `docs/plan/` 文档，除非改动过程中发现需求边界、架构影响或验收口径并不简单。
- 仍须先读相关代码，保护用户改动，并运行与改动匹配的测试。
- 若改动开始外溢到跨模块协议、数据模型、复杂 UI 流程、迁移或长期架构取舍，应升级到 Specification/Planning Mode。

### 1. Specification Mode

入口：项目级 skill `cairn-specification`。

目的：把自然语言需求澄清为明确 specification。

产物：

- `docs/specs/<name>-requirements.md`
- `docs/specs/<name>-presentation.html`

要求：

- 通过多轮对话澄清用户意图、边界、非目标、关键决策点。
- 不急于设计实现；只在必要处说明技术约束对需求的影响。
- 明确验收口径，但不展开实现计划。
- HTML presentation 用于交互式沟通，只展示核心判断、分歧点、决策与范围；不要把 md 全量搬过去。

### 2. Planning Mode

入口：项目级 skill `cairn-planning`。

目的：依据已确认 spec 生成可执行 plan。

产物：

- `docs/plan/<name>-execution-plan.md`
- `docs/plan/<name>-plan-presentation.html`

要求：

- 维护性与架构优先。若重构能显著降低熵增，应纳入计划并解释代价。
- plan 必须能指导 agent 独立实施：阶段、文件范围、数据模型/API/UI/迁移、风险、回滚或兼容策略。
- 测试与验收细节必须写充分，包括 unit/API/integration/UI smoke/manual E2E 等适用层级。
- HTML presentation 面向导师/PM 式沟通，只展示架构取舍、阶段划分、风险与验收关键点；避免重复 spec 细节。
- 若实现需要环境、凭据、外部服务或人工决策，必须在 plan 阶段列出并向用户确认。

### 3. Implementation Mode

入口：普通任务或用户要求实现时进入；不单独做 slash command。

要求：

- 实现前读取对应 spec 与 plan；若二者冲突，先停下说明冲突。
- 优先 RED/GREEN TDD：先补失败测试，再实现，再重构。
- 严格保护既有用户改动，不回滚不相关变更。
- 实现完成后，除单元测试外，尽量通过浏览器自动化或真实命令手动跑一遍关键流程。
- 若需要但缺少环境、凭据、镜像、服务或授权，说明缺口；能在 plan 阶段提前索要的，不拖到实现末尾。

## 文档约定

- spec 放 `docs/specs/`，plan 放 `docs/plan/`。
- Markdown 是完整源文档；HTML 是沟通展示，不替代源文档。
- 文件名使用 kebab-case，并带语义版本或功能名，例如：
  - `v2-command-blackboard-requirements.md`
  - `v2-command-blackboard-presentation.html`
  - `v2-command-blackboard-execution-plan.md`
  - `v2-command-blackboard-plan-presentation.html`
- 新文档顶部写明依据、日期、状态。

## 测试与验收

- 窄改动至少跑相关单测。
- 触及 server/dispatcher/UI/协议边界时，补覆盖相应层级的测试。
- UI 或流程类功能，最后尽量跑真实服务并用浏览器自动化检查关键路径。
- 结论以测试和实际流程为准，不以代码阅读自证完成。
