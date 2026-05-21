---
name: cairn-planning
description: Use for Cairn Planning Mode when the user wants to turn an existing docs/specs requirement into a maintainable execution plan in docs/plan plus an interactive HTML presentation.
---

# Cairn Planning Mode

Use this skill after a requirement spec exists or when the user asks to plan from a spec. The goal is an executable engineering plan with strong architecture, tests, and acceptance criteria.

## Workflow

1. Read the basis:
   - Must read `docs/architecture/cairn-architecture-ssot.md`.
   - Must read the target spec.
   - Also read relevant prior plans, code, tests, README, and config.
   - If the spec conflicts with the architecture SSOT or repo state, first check whether it has an explicit `Proposed Architecture Delta` and whether the SSOT has a matching `Pending Architecture Delta`.
   - If the conflict is intentional and marked by Proposed/Pending deltas, plan against the target delta. If it is unmarked, stop and explain the conflict before writing a plan.
   - Treat older plans as historical context when they conflict with the architecture SSOT.
2. Make architecture judgments:
   - Prefer maintainability and clear boundaries over minimizing agent labor.
   - Include refactoring when it meaningfully reduces entropy; explain why it is worth the cost.
   - Identify migration, compatibility, failure paths, and rollback or downgrade strategy where relevant.
   - Do not route new work through deprecated compatibility paths such as `run_provenance`, `/runs/*/transcript`, `/questions`, `question_jobs`, or `server/transcripts/*`.
   - If stale compatibility code could mislead implementation, include an explicit cleanup phase.
   - For architecture refactors, include the SSOT lifecycle: confirm/add Pending marker before implementation, then merge the implemented delta into the SSOT main body and remove/archive the Pending marker after validation.
3. Write an executable plan:
   - Phase-based.
   - Each phase must include purpose, scope, steps, tests, acceptance, and review checkpoints.
   - If implementation needs credentials, images, ports, services, environment, or human decisions, list them and ask the user during planning.

## Outputs

Create or update:

- `docs/plan/<name>-execution-plan.md`
- `docs/plan/<name>-plan-presentation.html`

Keep `<name>` traceable to the source spec.

## Markdown Shape

Use this structure unless the task clearly needs less:

```markdown
# <Feature> Execution Plan

依据：`docs/specs/<name>-requirements.md`
架构依据：`docs/architecture/cairn-architecture-ssot.md`
日期：YYYY-MM-DD
状态：待执行计划/已确认计划

## 0. 目标边界

## Phase 0: Contract First 与测试基线

### 目的
### 步骤
### 测试
### 验收
### 审查点

## Phase 1: ...

## 风险与开放问题

## 最终验收清单
```

Rules:

- The plan is an implementation manual, not a vision document.
- Every phase should be executable by an agent.
- Test details matter: unit, API, dispatcher/service, UI smoke, browser automation, and manual E2E as applicable.
- Specify the real final validation path: how to start services, walk the workflow, and judge success.
- Plans that touch core architecture must state whether the architecture SSOT needs updates.
- Plans that implement a Proposed Architecture Delta must state when the delta is only planned, when it becomes current, and which SSOT sections must be edited after implementation.
- Prefer deleting unused old compatibility code over preserving it as ambiguous fallback.

## HTML Presentation

The presentation file must be self-contained HTML/CSS/JS and open directly in a browser.

Write it for mentor/PM-style review. Show architecture choices, phase route, risks, and acceptance. Do not repeat spec details or implementation minutiae.

## Final Reply

Summarize briefly:

- Files written.
- Core architecture tradeoff.
- Anything still needed before implementation.
- Whether implementation can begin.
