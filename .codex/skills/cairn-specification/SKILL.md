---
name: cairn-specification
description: Use for Cairn Specification Mode when the user wants to clarify a feature, requirement, rough idea, bug, or product decision into docs/specs markdown plus an interactive HTML presentation.
---

# Cairn Specification Mode

Use this skill when shaping a Cairn requirement before implementation. The requirement may be a user-facing feature, architecture refactor, compatibility cleanup, module boundary change, or technical-debt cleanup. The goal is not to code yet; it is to turn natural language into a clear specification that can later feed planning.

## Workflow

1. Read current facts first:
   - Must read `docs/architecture/cairn-architecture-ssot.md` before making any architecture, data model, runtime, Output/Conversation, worker session, migration, or compatibility claim.
   - Relevant files in `docs/specs/`, `docs/plan/`, README, code, and tests.
   - Do not claim how the repo works unless you read the relevant files in this turn.
   - Treat older specs/plans as historical context when they conflict with the architecture SSOT.
2. Clarify the requirement:
   - Identify the original goal, user scenario, success criteria, non-goals, and key decision points.
   - Ask targeted questions only when the answer changes the spec.
   - Challenge unnecessary entities and premature implementation assumptions.
   - If the feature would revive `run_provenance`, `/runs/*/transcript`, `/questions`, `question_jobs`, or `server/transcripts/*` as a main path, flag it as an architecture conflict unless the user explicitly wants to revise the SSOT.
   - For refactor specs, define the reason, current architecture fact, target architecture boundary, preserved behavior, migration/cleanup scope, rollback risk, and acceptance criteria.
3. Write the spec once the core intent is clear:
   - Open questions are allowed, but the core goal must not be empty.
   - Keep the design simple: 如无必要，勿增实体.
   - Reference the architecture SSOT in the spec basis when the feature touches core architecture.
   - If the spec changes architecture, add a `Proposed Architecture Delta` section. Do not rewrite the SSOT main body as if the target is already implemented.
   - If the user confirms the refactor spec and future agents need to plan from it, add or request a matching `Pending Architecture Delta` marker in `docs/architecture/cairn-architecture-ssot.md`.

## Outputs

Create or update:

- `docs/specs/<name>-requirements.md`
- `docs/specs/<name>-presentation.html`

Use kebab-case for `<name>`. Preserve existing version naming when extending an existing spec.

## Markdown Shape

Use this structure unless the task clearly needs less:

```markdown
# <Feature> Requirements

日期：YYYY-MM-DD
状态：需求草案/已确认
依据：<用户输入、旧文档或相关代码>

## 1. 核心判断

## 2. 背景与现状

## 3. 目标

## 4. 非目标

## 5. 用户流程

## 6. 功能需求

## 7. 数据/API/界面需求

## 8. 关键决策点

## 9. Proposed Architecture Delta

> Only include this section when the spec intentionally changes architecture.
> State: current SSOT fact, proposed target fact, affected SSOT sections, preserved behavior, cleanup/migration scope, and when the delta becomes current.

## 10. 验收标准

## 11. 待确认问题
```

Rules:

- Define what is wanted, what is not wanted, and how it will be accepted.
- Do not write detailed implementation phases; leave that to `cairn-planning`.
- Mark uncertainty explicitly. Do not present guesses as facts.
- Refactor specs must not be vague cleanup wishes; they need crisp before/after boundaries and behavior that must remain unchanged.
- A refactor spec may intentionally conflict with Current SSOT only through an explicit `Proposed Architecture Delta`; otherwise treat the conflict as an error to clarify.
- Do not make future architecture look current. Current facts live in the SSOT main body; planned changes live in Proposed/Pending deltas until implemented.
- If requirements imply changing the architecture SSOT, list that as a key decision point instead of silently drifting.
- If old compatibility code is unnecessary and likely to mislead future implementation, mention cleanup as a requirement or non-goal boundary.

## HTML Presentation

The presentation file must be self-contained HTML/CSS/JS and open directly in a browser.

Keep it concise. Show only the communication-critical parts: scope, core judgment, key decisions, risks, and acceptance. Do not paste the full Markdown into HTML.

## Final Reply

Summarize briefly:

- Files written.
- Core spec conclusion.
- Remaining decisions, if any.
- Next step: use `cairn-planning` on the spec.
