<div align="center">

<img src="./README/banner.png" alt="Cairn Banner"/>

# Cairn
### More Than Just AI Penetration Testing — Towards General State-Space Search

**Current self-developed MVP: `v1.3.2`**

This line has diverged from the original upstream release track. The `1` prefix marks the current self-developed product line; `3.2` maps to the v3.2 live conversation thread milestone.

<p>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tencent.png" alt="Tencent" height="55" />
  </a>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tch.png" alt="TCH" height="55" />
  </a>
</p>

Cairn is a general-purpose problem-solving engine. <br/>It defines no roles, no workflows. Given an origin and a goal, it searches for a path through an unknown state space. <br/>AI Penetration Testing is one such problem — and a proven one.

<p>
  <a href="https://discord.gg/nDSy4NZVP" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord" />
  </a>
  <a href="https://x.com/le1xia0" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/X-000000?style=flat-square&logo=x&logoColor=white" alt="X" />
  </a>
</p>

</div>

<p align="center">
  <a href="https://www.bilibili.com/video/BV1a8R5BhEVi/" target="_blank" rel="noopener noreferrer">
    <img src="./README/cairn.png" alt="Cairn runtime screenshot" width="900" />
  </a>
</p>

## What is Cairn?

Penetration testing is fundamentally a **directed search through a near-infinite state space**:

- **Origin**: known (target IP, target system)
- **Goal**: defined (get a shell, capture the flag)
- **Path**: unknown

This structure is not unique to penetration testing. Vulnerability research, mathematical proof, CTF challenges — any problem with a clear starting point, a clear success condition, and an unknown path in between shares the same shape.

Cairn is built for this class of problems. Penetration testing is the first domain it has been validated on.

The engine is built on a **Blackboard Architecture** with an explicit fact-intent graph. The user-facing search model stays small:

| Concept | Meaning |
|---------|---------|
| **Fact** | A useful result node written back to the board after an exploration step |
| **Intent** | A work order on the graph: what should be explored next |
| **Hint** | Human judgment injected at any time; absorbed by workers on the next read |

The graph grows from `origin` toward `goal`. Every new Fact is a stepping stone; every Intent is a step into the unknown.

Runtime state is kept out of the graph:

| Runtime layer | Meaning |
|---------------|---------|
| **ExecutionRun** | One actual worker run: lease, worker identity, workspace, session, status |
| **ExecutionEvent** | The append-only event stream for output, messages, tools, sessions, and status |
| **Branch** | The session lineage for `resume`, `fork`, and `fresh_context` conversations |
| **Environment** | The place work runs: Docker container, SSH workspace, or another backend |

Agent Workers run an OODA loop — Observe the full graph, Orient to the current state, Decide on next intents, Act to explore — and write their findings back as new Facts. Workers have no fixed roles. Tasks are generated at runtime from the graph's current state, not from predefined job descriptions.

Agents coordinate exclusively through the shared board (Stigmergy). No direct communication. No information silos.

## Cairn in Action

https://github.com/user-attachments/assets/e557b1ac-dda4-41cb-87dd-9d56dbf05133


## How It Works

Four task types, all executed by the same Worker:

| Task | What it does | Output |
|------|-------------|--------|
| **Bootstrap** | At project start, attempts to solve the problem directly | Fact + possible Complete |
| **Reason** | Reads the full graph: is the goal met? What should be explored next? | Complete / new Intents / no-op |
| **Explore** | Claims one Intent, executes the exploration, reports findings | One Fact |
| **Question** | Lets the user ask follow-up questions against a run/session | ExecutionEvents, optional promoted Fact |

System architecture:

```
          ┌──────────────────────────────────────┐
          │             Cairn Server             │
          │  UI + API + Fact/Intent DAG          │
          │  ExecutionRun/Event + Branch ledger  │
          └──────────────────┬───────────────────┘
                             │
                      HTTP read/write API
                             │
          ┌──────────────────┴───────────────────┐
          │              Dispatcher              │
          │  Schedules work, runs workers,        │
          │  streams ExecutionEvents back         │
          └───────────┬────────────────┬─────────┘
                      │                │
       ┌──────────────┴─────┐   ┌──────┴──────────────┐
       │ Docker Environment │   │ SSH Environment      │
       │ per-project space  │   │ remote workspace     │
       └──────────────┬─────┘   └──────┬──────────────┘
                      │                │
                Claude / Codex / Pi workers
```

**Cairn Server** owns the UI, API, graph consistency, execution ledger, branch timelines, and persisted artifacts.

**Cairn Dispatcher** reads pending work, chooses healthy workers, prepares the selected environment, runs the agent command, and streams stdout/stderr/message/tool/session events back to the server.

Supported worker backends: **Claude Code**, **Codex**, and **Pi**.

Supported execution environments: **Docker** and **SSH**.

Live conversation support in this MVP:

| Mode | Behavior |
|------|----------|
| **resume** | Continues the source session and persists the reply into the main Output history |
| **fork** | Creates a temporary branch, supports multi-turn context, and stays in the Ask panel until closed |
| **fresh_context** | Starts without remote session lineage and stays temporary |

## Results

**Tencent Cloud Hackathon · AI Penetration Testing Challenge · 2nd Edition**

610 teams · 1,345 participants · top universities and security firms across China

| Metric | Value |
|--------|-------|
| Problems solved | **54 / 54 — only team to AK** |
| Final ranking | 3rd |

> The system had never been tested before the competition. The full pipeline came online for the first time at 4 AM on race day. No training, no tuning, no domain-specific tooling. Zero MCP tools, zero RAG, zero predefined agent roles.

## Further Reading

- <a href="https://mp.weixin.qq.com/s/DlpEH7bVr0xi0VawPJs3XA" target="_blank" rel="noopener noreferrer">The Strongest AI Penetration Testing Agent: Postmortem of the Only Team to Achieve AK at the TCH Tencent Cloud Hackathon Intelligent Penetration Testing Challenge (2nd Edition)</a>
- <a href="https://mp.weixin.qq.com/s/2rEqFLvkxvYWM3gW170C2w" target="_blank" rel="noopener noreferrer">The Pathless Path: Cairn AI from Penetration Testing to General Problem Solving</a>

## Getting Started

**Prerequisites**
 
- macOS or Linux
- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker
- A worker CLI you plan to use: Claude Code, Codex, or Pi

### Development mode with UV

This is the recommended path when hacking on Cairn itself. It runs directly from the local source tree, so code changes are picked up without rebuilding an image.

```bash
# 1. Prepare a local dispatcher config
cp dispatch.example.yaml dispatch.dev.yaml

# 2. For local host development, make sure dispatch.dev.yaml points at:
# server: "http://127.0.0.1:8000"
#
# Then configure workers/model profiles in dispatch.dev.yaml and add provider
# endpoints/API keys in the server Environment UI.

# 3. Initialize or migrate the local development database
uv run --project cairn cairn db migrate --db-path cairn.local.db

# 4. Start the API server and built-in web UI
uv run --project cairn cairn serve --db-path cairn.local.db --host 127.0.0.1 --port 8000

# 5. In another terminal, run startup checks
uv run --project cairn cairn dispatch --config dispatch.dev.yaml --startup-healthcheck-only

# 6. Start the dispatcher
uv run --project cairn cairn dispatch --config dispatch.dev.yaml
```

Open the UI at <http://127.0.0.1:8000>. The web frontend is served by `cairn serve`; there is no separate frontend dev server in the current MVP.

### Dispatcher config

`dispatch.dev.yaml` tells the dispatcher which server to talk to, which worker commands are available, and how much concurrency each worker may use. Provider URLs and API keys are not stored in this YAML; add them in the web UI under Environment.

Minimal local shape:

```yaml
server: "http://127.0.0.1:8000"

runtime:
  interval: 3
  max_workers: 4
  max_running_projects: 2
  max_project_workers: 4
  healthcheck_timeout: 20
  prompt_group: "default"

tasks:
  bootstrap:
    timeout: 300
    conclude_timeout: 90
  reason:
    timeout: 300
    max_intents: 2
  explore:
    timeout: 300
    conclude_timeout: 90

container:
  image: "ghcr.io/oritera/cairn-worker-container:latest"
  platform: "linux/amd64"
  network_mode: "host"
  completed_action: "stop"

model_profiles:
  - id: "pi-main"
    type: "pi"
    model: "gpt-5.4"
    context_window: 262144

workers:
  - name: "pi-agent"
    type: "pi"
    model_profile: "pi-main"
    endpoint: "pi-default"
    task_types: [bootstrap, reason, explore]
    max_running: 2
    priority: 0
```

Important fields:

| Field | Meaning |
|-------|---------|
| `model_profiles[].id` | Local profile name used by workers through `model_profile` |
| `model_profiles[].type` | Worker adapter type: `pi`, `codex`, `claudecode`, or `mock` |
| `model_profiles[].model` | Model name passed to the worker CLI |
| `workers[].name` | Dispatcher-side worker name shown in health/runtime status |
| `workers[].type` | Must match the intended adapter type |
| `workers[].model_profile` | Must equal one `model_profiles[].id` |
| `workers[].endpoint` | Must equal the provider endpoint ID you created in the web UI Environment screen |
| `workers[].task_types` | Which task kinds this worker can claim |

For example, if the web UI Environment endpoint ID is `pi-default`, then the YAML worker must use `endpoint: "pi-default"`. This is an ID lookup, not a base URL. The base URL, provider API type, and API key live in the server database through the web UI.

Useful development commands:

```bash
# Check migration state
uv run --project cairn cairn db status --db-path cairn.local.db

# Recreate a v3.2 database, keeping an ignored backup beside it
uv run --project cairn cairn db reset --to v3.2 --db-path cairn.local.db --yes

# Run the test suite
env PYTHONPATH=cairn/src python -m pytest cairn/tests -q
```

`cairn.local.db`, `cairn.local.db-*`, and `cairn.local.db.bak-*` are gitignored.

### Pull required images
 
Docker environments require the worker container image:
 
```bash
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```
 
### Docker Compose
 
Pull the base image used to build Cairn:
 
```bash
docker pull ghcr.io/astral-sh/uv:python3.13-trixie
```
 
Copy `dispatch.example.yaml` to gitignored `dispatch.dev.yaml`, configure model profiles/workers there, then add provider endpoints and API keys in the server Environment UI. Start both services:
 
```bash
docker compose up --build
```
 
This starts `cairn-server` on port `8000` and `cairn-dispatcher` once the server passes its health check. The dispatcher mounts `dispatch.dev.yaml` from the project root and connects to Docker via the host socket. Data is persisted to `./datas/cairn/`.

## Disclaimer

Cairn is a general-purpose problem-solving engine. Although it supports penetration testing, CTF solving, security assessment, and vulnerability research workflows, it is intended to be used only in environments where you have explicit authorization to operate.

You are solely responsible for how you use this project. Do not use Cairn against systems, networks, applications, or data without clear prior permission from the owner or operator. Unauthorized security testing, exploitation, or data access may be illegal and may cause harm.

The developers and contributors of this project do not endorse or accept responsibility for any misuse, abuse, damage, loss, or legal consequences arising from its use. By using this project, you agree to ensure that your activities comply with all applicable laws, regulations, contractual obligations, and professional or organizational policies in your jurisdiction.

## Star History

<a href="https://www.star-history.com/#oritera/Cairn&Date" target="_blank" rel="noopener noreferrer">
  <img src="https://api.star-history.com/svg?repos=oritera/Cairn&type=Date" alt="Star History Chart" />
</a>

## ⚖️ License
This project is licensed under **GNU AGPLv3** for personal and educational use.

**Commercial Use**: If you wish to use this project in a commercial or proprietary environment without the AGPL-3.0 open-source obligations, **please contact me to obtain a commercial license.**

**Contributions**: By submitting a Pull Request, you agree that your contributions may be used under both the AGPL-3.0 and the project's commercial license.
