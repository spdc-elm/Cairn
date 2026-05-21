# Cairn Agent Instructions

本文件约束仓库根目录下的协作方式。若子目录另有 `AGENTS.md`，以更近者为准。

## 工作原则

- 先读后判。凡涉及当前代码、文档、接口、测试、目录结构的判断，必须先用 `rg`/定向读取核实。
- 不凭惯例替代事实。若需求、spec、plan、实现状态不明，先查源文件；仍不明再问用户。
- 涉及架构、数据模型、运行链路、Output/Conversation、worker session、迁移或兼容策略时，必须先读 `docs/architecture/cairn-architecture-ssot.md`，并以其为当前架构单一事实源。
- 如无必要，勿增实体。优先简洁、清晰、可维护；新增抽象必须能降低真实复杂度。
- 反迎合。先审查问题 framing、关键假设与失败路径，再决定赞同、修正或反驳。
- 默认用中文简洁沟通；实现、命令、代码标识保持仓库原语言与风格。

## 产品本体定义

架构 SSOT：`docs/architecture/cairn-architecture-ssot.md`。本节只保留最常用边界，若与 SSOT 冲突，以 SSOT 为准。

涉及 Cairn 架构、spec、plan 或实现时，先保持下列概念边界：

- `Fact/Intent DAG` 是用户心智模型：Cairn 把从 `origin` 到 `goal` 的问题解决过程表达成一张探索 DAG。
- `Fact` 不是 atomic assertion，也不是所有观察的最小集合。`Fact` 是一次探索步骤进入黑板后的结果节点，承载该次探索对用户有用的摘要；一次 successful concluded exploration intent 默认产出一个 primary result fact。
- `Intent` 是图上的探索边，也是工作订单：它从已有 facts 出发，描述下一步要探索什么。`Intent` 不应持有 live worker lease、heartbeat、cancel/retry runtime state。
- `ExecutionRun` 是运行层原子：某个 worker 在某个环境中实际执行了一次。worker identity、session、lease、heartbeat、stdout/stderr、returncode、timeout、cancel、retry 等运行事实应挂在 execution 上。
- `ExecutionEvent` 是实时输出源：Output、conversation transcript、tool/message stream 应从 execution events 投影，不应由 `Fact` / `Intent` 反向拼日志。
- `Branch` 表达 fork/resume 的 session 连续线；多轮 fork/resume 是同一 branch 下追加 executions。
- `Artifact` 保存证据和大产物：report、transcript、scan output、screenshot、文件等。细粒度观察和原始证据优先进入 event/artifact，不把图层拆成许多小 fact。
- 不要为了“候选事实/待审核断言”提前引入 `Claim` 等新核心概念。除非 spec 明确要求长期审核 workflow，否则用 `Fact` + `ExecutionRun` + `Artifact` 足够。
- `run_provenance`、`/runs/*/transcript`、`/questions`、`server/transcripts/*` 是旧路径/历史背景，不得恢复为 v3.2+ Output/Conversation 主链路；若发现无实质用途的兼容残余，优先纳入清理。

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

目的：把自然语言需求澄清为明确 specification。需求可以是用户功能，也可以是架构重构、兼容清理、模块边界调整或技术债治理。

产物：

- `docs/specs/<name>-requirements.md`
- `docs/specs/<name>-presentation.html`

要求：

- 先读 `docs/architecture/cairn-architecture-ssot.md`，新 spec 不得与 SSOT 冲突；若需求确实要改变架构，必须把“需更新 SSOT”列为决策点。
- 若 spec 主题是重构，必须写清楚重构动机、当前架构事实、目标架构边界、非目标、用户可见行为不变项、迁移/清理范围和验收口径；不要只写“整理代码”。
- 若 spec 会改变 SSOT，spec 里必须新增 `Proposed Architecture Delta`；不要把 SSOT 主体提前改成未实现的未来事实。若 spec 已确认且后续 plan/implementation 需要引用该偏离，在 SSOT 的 `Pending Architecture Deltas` 留标记。
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

- 先读 `docs/architecture/cairn-architecture-ssot.md`。plan 必须引用并遵守 Current SSOT；若目标 spec 含 `Proposed Architecture Delta` 且 SSOT 有对应 Pending 标记，则按目标 delta 规划，不把该冲突误判为漂移。
- 维护性与架构优先。若重构能显著降低熵增，应纳入计划并解释代价。
- plan 必须能指导 agent 独立实施：阶段、文件范围、数据模型/API/UI/迁移、风险、回滚或兼容策略。
- 架构重构 plan 必须包含 SSOT 生命周期：实现前确认 Pending 标记；实现完成并验收后，把 delta 合并进 SSOT 主体并移除/归档 Pending 标记。
- 测试与验收细节必须写充分，包括 unit/API/integration/UI smoke/manual E2E 等适用层级。
- HTML presentation 面向导师/PM 式沟通，只展示架构取舍、阶段划分、风险与验收关键点；避免重复 spec 细节。
- 若实现需要环境、凭据、外部服务或人工决策，必须在 plan 阶段列出并向用户确认。

### 3. Implementation Mode

入口：普通任务或用户要求实现时进入；不单独做 slash command。

要求：

- 实现前读取对应 spec、plan 与 `docs/architecture/cairn-architecture-ssot.md`；若三者冲突，先判断是否存在明确的 Proposed/Pending Architecture Delta。没有 delta 标记则停下说明冲突；有 delta 标记则按 plan 实现并在完成后同步 SSOT 主体。
- 优先 RED/GREEN TDD：先补失败测试，再实现，再重构。
- 严格保护既有用户改动，不回滚不相关变更。
- 实现完成后，除单元测试外，尽量通过浏览器自动化或真实命令手动跑一遍关键流程。
- 若需要但缺少环境、凭据、镜像、服务或授权，说明缺口；能在 plan 阶段提前索要的，不拖到实现末尾。

## 文档约定

- spec 放 `docs/specs/`，plan 放 `docs/plan/`。
- Markdown 是完整源文档；HTML 是沟通展示，不替代源文档。
- 架构 SSOT 放 `docs/architecture/`。新 spec/plan 若触及核心架构，必须引用 SSOT。
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

## Git 提交前检查

- 若改动触及架构、schema、migration、API、dispatcher contract、worker session、Output/Conversation、兼容策略或 agent 指令，必须检查 `docs/architecture/cairn-architecture-ssot.md` 是否需要同步更新。
- 若引入或删除旧兼容路径，必须明确其与 SSOT 的关系；无实质用途的旧兼容代码不应保留。
- 若是已实现的架构改动，提交前 SSOT 主体必须反映新事实，不能只留下 Pending 标记；纯 spec/plan/docs 提案提交除外。
- 若决定不更新 SSOT，在提交说明、PR 描述或最终回复中说明原因。
