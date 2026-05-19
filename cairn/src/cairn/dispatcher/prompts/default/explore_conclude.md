# Task
You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
But note that you are not continuing the task here, and you do not need to wait for unfinished tasks or commands. You only need to summarize the key facts that have already been confirmed so far and are most helpful for reaching Goal.
This is the conclude phase. It overrides any earlier instruction in the same session that told you to keep working, continue exploring, solve Goal, wait for command results, or perform more actions.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following:
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"title": "...", "description": "..."}}
```

# Rules
- Stop the exploration immediately. Do not continue solving the task.
- Before producing the JSON, create parent directories if needed and create or update only the Markdown execution report at `{report_path}` using the facts already known in this session.
- Do not run analysis commands, inspect additional files, wait for unfinished commands, browse, install tools, or try to obtain new information. Creating the report directory and writing the report file are the only allowed actions before the final JSON.
- Base your answer only on information that has already been confirmed before this conclude prompt. If something has not already been confirmed, do not wait for it and do not include it.
- This JSON summary is your final output for this phase. After outputting it, stop.
- `description` must be an already confirmed objective factual conclusion. Do not output plans, guesses, or explanatory filler. Do not put long data blobs in `description`; long data should be placed in a file and referenced from `description` instead.
- `description` should contain only the latest incremental facts discovered. Do not repeat information already present in the graph snapshot, and do not include redundant details that do not help advance Goal.
- `title` must be a short human-facing label for graph display. It should help a human scan the board, but it is not a substitute for `description`.
- Existing fact titles in the graph are display summaries; use fact descriptions and reports as the source of truth.
- In the report, record what is already confirmed, relevant artifacts, failures, and uncertainty. Keep the JSON description short.

# Context
## Graph
```
{graph_yaml}
```

## Current Intent
```
{intent_id}
```

## Current Intent Description
```
{intent_description}
```

## Execution Report Path
```
{report_path}
```
