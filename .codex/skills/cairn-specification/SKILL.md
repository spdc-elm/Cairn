---
name: cairn-specification
description: Use for Cairn Specification Mode when the user wants to clarify a feature, requirement, rough idea, bug, or product decision into docs/specs markdown plus an interactive HTML presentation.
---

# Cairn Specification Mode

Use this skill when shaping a Cairn requirement before implementation. The goal is not to code yet; it is to turn natural language into a clear specification that can later feed planning.

## Workflow

1. Read current facts first:
   - Relevant files in `docs/specs/`, `docs/plan/`, README, code, and tests.
   - Do not claim how the repo works unless you read the relevant files in this turn.
2. Clarify the requirement:
   - Identify the original goal, user scenario, success criteria, non-goals, and key decision points.
   - Ask targeted questions only when the answer changes the spec.
   - Challenge unnecessary entities and premature implementation assumptions.
3. Write the spec once the core intent is clear:
   - Open questions are allowed, but the core goal must not be empty.
   - Keep the design simple: 如无必要，勿增实体.

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

## 9. 验收标准

## 10. 待确认问题
```

Rules:

- Define what is wanted, what is not wanted, and how it will be accepted.
- Do not write detailed implementation phases; leave that to `cairn-planning`.
- Mark uncertainty explicitly. Do not present guesses as facts.

## HTML Presentation

The presentation file must be self-contained HTML/CSS/JS and open directly in a browser.

Keep it concise. Show only the communication-critical parts: scope, core judgment, key decisions, risks, and acceptance. Do not paste the full Markdown into HTML.

## Final Reply

Summarize briefly:

- Files written.
- Core spec conclusion.
- Remaining decisions, if any.
- Next step: use `cairn-planning` on the spec.

