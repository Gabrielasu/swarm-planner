# Planning Swarm

A CLI tool that orchestrates multiple LLM agents in a sequential pipeline to produce implementation-ready software specifications. Feed it a project description and it outputs a complete architecture: component trees, interface contracts, an adversarially-tested design, and a **stateful task graph** optimized for AI agent consumption.

It includes a **real-time web dashboard** for monitoring pipeline progress, managing tasks, filing and resolving discoveries, and controlling pipeline reruns -- all from the browser.

## What It Produces

Given a project description, the pipeline outputs:

- **Structured brief** -- clarified and expanded from your input
- **Component tree** -- with ownership boundaries and data flows
- **Interface contracts** -- at every component boundary, implementation-grade
- **Adversarially-tested design** -- with documented decisions and resolved findings
- **Stateful task graph** (`graph.json`) -- ordered, dependency-tracked, with completion status
- **Self-contained prompt packets** (`tasks/*.json`) -- each task has everything a coding agent needs

All artifacts are written to `.plan/` in your project directory.

## Output Format: Stateful Task Graph

The output is designed for AI agent consumption, not human reading. Instead of large markdown files, the plan uses a **two-phase loading** architecture that minimizes context window usage.

### `graph.json` -- The Task DAG

A compact JSON file the agent loads first. Contains the full task topology, dependency graph, completion status, and discovery tracking. Typically 50-100 tokens per task.

```json
{
  "project": "Real-time collaborative editor",
  "constraints": {"platform": "web", "language": "typescript"},
  "components": {
    "Authentication": {"id": "auth", "responsibility": "handles auth", "owns": ["sessions", "tokens"], "interfaces": ["auth-to-api"]},
    "API Layer":      {"id": "api",  "responsibility": "handles routing", "owns": ["routes"], "interfaces": ["api-to-db", "auth-to-api"]}
  },
  "tasks": [
    {"id": "infra-001", "title": "Project scaffold",     "component": "Infrastructure", "complexity": "trivial",  "status": "done",    "depends": [],                "tokens": 620},
    {"id": "auth-001",  "title": "JWT utility functions", "component": "Authentication", "complexity": "standard", "status": "ready",   "depends": ["infra-001"],     "tokens": 850},
    {"id": "auth-002",  "title": "Register function",     "component": "Authentication", "complexity": "standard", "status": "pending",  "depends": ["auth-001"],     "tokens": 940},
    {"id": "api-001",   "title": "Express app scaffold",  "component": "API Layer",      "complexity": "standard", "status": "pending", "depends": ["infra-001","auth-002"], "tokens": 1100}
  ],
  "discoveries": [],
  "meta": {
    "total_tasks": 4, "done": 1, "ready": 1, "blocked": 2,
    "version": 2, "last_updated": "2026-03-17T10:30:00Z",
    "changelog": [...]
  }
}
```

### `tasks/{id}.json` -- Self-Contained Prompt Packets

Loaded on demand for the selected task only. Contains **everything** the coding agent needs -- contracts are inlined, not referenced. No cross-referencing, no loading other files alongside it.

```json
{
  "id": "auth-001",
  "title": "JWT utility functions",
  "v": 1,
  "instruction": "Implement signToken and verifyToken using jsonwebtoken.",
  "component": "Authentication",
  "complexity": "standard",
  "create": ["src/auth/jwtUtils.ts", "src/auth/__tests__/jwtUtils.test.ts"],
  "modify": [],
  "context": ["src/types/errors.ts"],
  "contracts": [
    {
      "boundary": "auth-to-api",
      "pattern": "synchronous_call",
      "fns": [
        {"name": "verifyToken", "params": {"token": "string"}, "returns": {"user_id": "string"}, "errors": ["TokenExpired", "TokenInvalid"]}
      ],
      "error_shape": {"error": "string", "code": "string"},
      "stub": "Return {user_id:'test-user'} for tokens starting with 'test-'"
    }
  ],
  "done_when": [
    "signToken returns a valid JWT with userId and email claims",
    "verifyToken decodes a valid token and returns AuthenticatedUser",
    "verifyToken throws TokenExpiredError for expired tokens",
    "npx tsc --noEmit exits 0"
  ],
  "not_this": ["Registration flow", "Login flow", "HTTP route handlers"],
  "depends": ["infra-001"],
  "unlocks": ["auth-002", "auth-003"]
}
```

Tasks are intentionally small and focused. Each task implements one logical unit of functionality (a single function, a single endpoint, a single data layer), not an entire component. A medium-complexity project typically produces 40-80 tasks.

### Why This Format?

| Problem with markdown plans | How this solves it |
|---|---|
| Giant files clog the context window | Two-phase loading: read index (tiny), then one packet (small) |
| Cross-referencing contracts/architecture | Contracts are inlined into each task packet |
| ~35% of tokens are formatting | Compressed JSON with minimal keys |
| No way to track completion | `status` field with automatic dependency cascade |
| Plans are write-once | Stateful: discoveries, updates, versioned changelog |
| All-or-nothing loading | Agent reads only what it needs for the current task |

**Token efficiency:** A 50-task plan index is ~3,000-4,000 tokens in `graph.json` vs ~40,000-60,000 tokens loading all markdown task files.

## Install

Requires Python 3.11+.

```bash
# Global install (recommended)
pipx install /path/to/swarm-planner

# Or editable install in a virtualenv
pip install -e /path/to/swarm-planner
```

## Setup

```bash
# Configure with an Anthropic API key
swarm init

# Or use OpenCode (Claude subscription, no API key needed)
swarm init --opencode
```

Your API key is saved to `~/.config/swarm/config.toml` with owner-only permissions (0600). It is never stored in any project directory or committed to git.

You can also set the key via environment variable (overrides the config file):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

### Planning

```bash
# Plan from a file
swarm plan brief.md

# Plan from inline text
swarm plan -i "build a real-time collaborative document editor with role-based permissions"

# Plan a brownfield project (existing codebase)
swarm plan brief.md --codebase ./src

# Interactive -- type your description, Ctrl+D when done
swarm plan
```

### Pipeline Controls

```bash
swarm status              # Show pipeline progress + task graph
swarm status --graph      # Detailed task graph view with all tasks
swarm approve             # Continue after human review checkpoint
swarm plan --resume       # Resume after a crash or interruption
swarm rerun adversary     # Re-run from a specific step (invalidates downstream)
swarm reset               # Clear pipeline state (keeps artifacts)
swarm log                 # Show full planning log + graph changelog
```

### Task Graph Management

After the pipeline completes, use these commands to manage the task graph as coding agents work through the plan:

```bash
swarm ready               # List tasks that are ready to work on
swarm show auth-001       # Show a task's full prompt packet
swarm start auth-001      # Mark a task as in-progress
swarm done auth-001       # Mark a task as done (cascades: unblocks dependents)
```

### Discovery System

When a coding agent finds something during implementation that affects the plan, it records a **discovery**:

```bash
# Report a discovery
swarm discover auth-003 "auth-to-api needs revokeToken for logout" \
  -t missing_contract_fn \
  -a auth-001 \
  -s high

# Resolve a discovery (unblocks affected tasks)
swarm resolve 0 "Added revokeToken to auth contract and updated task packet"
```

Discovery types: `missing_contract_fn`, `task_split_needed`, `dependency_missing`, `scope_change`, `blocker`

Discoveries cascade automatically:
- Affected tasks that were `done` become `invalidated`
- Affected tasks that were `pending`/`ready` become `needs_update`
- Tasks downstream of invalidated tasks become `pending` (blocked)
- Resolving a discovery recomputes all statuses -- tasks unblock if their dependencies are satisfied

### How a Coding Agent Uses the Plan

1. **Load index**: Read `graph.json` (~3-4k tokens for a 50-task plan)
2. **Pick a task**: Filter for `"status": "ready"`, choose based on complexity/dependencies
3. **Load packet**: Read `tasks/{id}.json` (~300-900 tokens, self-contained)
4. **Implement**: Everything needed is in the packet -- contracts, files, acceptance criteria, scope exclusions
5. **Report completion**: `swarm done {id}` -- cascades to unblock downstream tasks
6. **Report discoveries**: `swarm discover {id} "description"` -- flags affected tasks for review

### Task Status State Machine

```
 pending ──> ready ──> in_progress ──> done
    ^          |           |             |
    |          v           v             v
    +------ blocked    blocked     invalidated
                                        |
                                        v
                                  needs_update
                                        |
                                        v
                                   pending ──> ready (after planner resolves)
```

- **pending**: Dependencies not yet met
- **ready**: All dependencies done, can be picked up
- **in_progress**: A coding agent is working on it
- **done**: Implementation complete
- **invalidated**: Was done, but an upstream discovery broke it
- **needs_update**: Planner flagged for revision

## Dashboard

A real-time web dashboard for monitoring and managing the entire planning and implementation lifecycle. Launch it from your project directory:

```bash
swarm dashboard              # Opens at http://localhost:8420
swarm dashboard --port 9000  # Custom port
swarm dashboard --no-open    # Don't auto-open the browser
```

### What the dashboard shows

- **Pipeline progress**: Visual step-by-step view of the 11-stage planning pipeline. Each completed step is clickable to trigger a rerun from that point.
- **Task graph visualization**: Interactive DAG with pan, zoom, and click-to-inspect. Nodes are colored by status (green=done, blue=ready, cyan=active, gray=blocked, red=invalidated).
- **Task list**: Filterable by status (all, ready, active, done, blocked, issues). Click any task to open the detail drawer.
- **Task detail drawer**: Shows the full prompt packet -- instruction, files, inlined contracts with function signatures, acceptance criteria, dependencies, and unlocks. Action buttons to start, complete, or invalidate tasks.
- **Components panel**: Per-component progress bars showing done/total tasks, with ownership data and responsibility descriptions on hover.
- **Discoveries panel**: Lists all discoveries with severity badges, affected task links, and inline resolve UI. "New" button opens a modal to file discoveries from the browser.
- **Activity feed**: Combined pipeline log and graph changelog, sorted by time.
- **Stats bar**: Total tasks, completed, in progress, ready, blocked, and open issues at a glance.

### Dashboard actions

Everything that can be done from the CLI can be done from the dashboard:

| Action | CLI | Dashboard |
|--------|-----|-----------|
| Mark task as started | `swarm start <id>` | Click task > "Start Task" button |
| Mark task as done | `swarm done <id>` | Click task > "Complete Task" button |
| Invalidate a task | n/a | Click task > "Invalidate" button |
| File a discovery | `swarm discover <id> "desc"` | Discoveries panel > "+ New" button |
| Resolve a discovery | `swarm resolve <idx> "desc"` | Discoveries panel > "Mark Resolved" |
| Review affected tasks | `swarm show <id>` | Discovery > "Review Tasks" > click task links |
| Rerun from a step | `swarm rerun <step>` | Click a completed pipeline step |
| Approve human review | `swarm approve` | Click the "Approve" step in the pipeline |

The dashboard uses WebSocket for real-time updates. Any change to `.plan/` files (from the CLI, a coding agent, or the dashboard itself) is detected within 500ms and pushed to all connected browsers.

## Pipeline

The pipeline runs 11 steps in sequence. Each step is a focused LLM agent with a specialized prompt and structured output schema.

```
 [1]  Interviewer          Evaluates your input, asks clarifying questions if needed
 [2]  Codebase Analyzer    Scans existing code for brownfield projects (optional)
 [3]  Decomposer           Breaks the system into components with ownership boundaries
 [4]  Contract Writer      Defines interface contracts at every component boundary
 [5]  Contract Resolver    Makes contracts implementation-grade (types, errors, consistency)
 [6]  Adversary Loop       Attacks the plan -- structural, data flow, and failure issues
 [7]  Human Review         Pipeline pauses for you to review (swarm approve to continue)
 [8]  Sequencer            Converts architecture into small, focused implementation tasks
 [9]  Simulator            Checks if each task can be one-shotted by a coding agent
[10]  Refinement           Re-resolves contracts and tasks that failed readiness checks
[11]  Graph Export          Builds graph.json + self-contained task packets
```

The adversary loop runs up to 3 rounds by default (configurable with `--max-rounds`). It stops early if no critical or high-severity findings remain.

The sequencer is designed to produce **small, focused tasks** -- each implementing one logical unit of functionality. A task that creates more than 3 files, has more than 6-8 acceptance criteria, or combines unrelated work will be flagged by the simulator for splitting during the refinement step.

## Output Structure

```
.plan/
├── .state.json              # Pipeline state (enables resume/rerun)
├── brief.md                 # Structured brief from the Interviewer
├── review.md                # Compact review doc (architecture + contracts + decisions)
├── graph.json               # Stateful task DAG (THE primary output)
└── tasks/                   # Self-contained prompt packets (one per task)
    ├── infra-001.json
    ├── auth-001.json
    ├── auth-002.json
    └── ...
```

## Model Routing

Different agents use different model tiers based on task complexity:

| Tier     | Default Model          | Used By                              |
|----------|------------------------|--------------------------------------|
| Frontier | `claude-opus-4-6`      | Decomposer, Adversary, Simulator     |
| Coding   | `claude-sonnet-4-6`    | Contract Writer/Resolver, Sequencer  |
| Fast     | `claude-haiku-4-5`     | Interviewer                          |

Override via environment variables:

```bash
export SWARM_FRONTIER_MODEL="claude-opus-4-6"
export SWARM_CODING_MODEL="claude-sonnet-4-6"
export SWARM_FAST_MODEL="claude-haiku-4-5-20251001"
```

Or configure during `swarm init`.

## Cost

You can use your Claude subscription through OpenCode to run the tool. Alternatively, use the Anthropic API. A typical run for a medium-complexity project (40-80 tasks) costs **$12-35** across 10-20 LLM calls. The Interviewer step adds negligible cost ($0.10-0.30) but significantly improves output quality for vague inputs.

## Design

**Stateful task graph.** The plan is a living document, not write-once markdown. Tasks have completion status, dependencies cascade automatically, and discoveries feed back into the plan. The graph version increments on every mutation with a full changelog.

**Two-phase loading.** Agents read the compact index first (~3-4k tokens), then load only the task packet they need (~300-900 tokens). Never loads the whole plan into context.

**Self-contained packets.** Each task's prompt packet inlines all relevant contracts, stubs, and error shapes. No cross-referencing between files.

**Small, focused tasks.** The sequencer produces tasks that each implement one logical unit of functionality. The simulator enforces this -- tasks that are too large are flagged for splitting during refinement. Each task should be completable in a single agent session with no memory of previous tasks.

**Token-budgeted.** Every packet knows its approximate token cost. Orchestrators can verify a task fits in the agent's context window before dispatch.

**Discovery feedback loop.** When a coding agent finds something during implementation that affects the plan, it records a discovery. Discoveries cascade through the dependency graph, invalidating affected tasks and blocking downstream work until resolved. This keeps the plan honest as implementation reveals reality.

**Resumable pipeline.** State is persisted to `.plan/.state.json` after every step. Recover from crashes with `swarm plan --resume`. Selectively re-run from any step with `swarm rerun <step>`, which automatically invalidates all downstream artifacts.

**Real-time dashboard.** A web UI at `swarm dashboard` provides full visibility and control. Pipeline progress, task graph visualization, task management, discovery filing/resolution, and pipeline reruns -- all from the browser with WebSocket-driven live updates.

**Structured output.** All inter-agent artifacts are validated with Pydantic schemas. JSON schema instructions are appended to system prompts, and responses are parsed with robust extraction (markdown fence stripping, bracket matching, truncated-JSON repair).

**Human-in-the-loop.** The pipeline pauses at step 7 for human review of the architecture, contracts, and design decisions. A consolidated `review.md` shows everything in one file. Approve to continue, or re-run from any earlier step.

**Context engineering.** Each agent receives only the context it needs via `assemble_context()`. Prompt templates become system prompts; variables are injected as user messages wrapped in XML-style tags.

**Two LLM backends.** Direct Anthropic API (pay-as-you-go) or OpenCode CLI (uses an existing Claude subscription). The OpenCode backend shells out to `opencode run` and parses JSON-line events.

## Project Structure

```
swarm-planner/
├── swarm/
│   ├── cli.py              # Click CLI (plan, status, done, discover, resolve, ready, show, dashboard, etc.)
│   ├── runner.py            # Pipeline orchestration engine
│   ├── graph_builder.py     # Stateful task graph builder and manager
│   ├── dashboard.py         # FastAPI + WebSocket real-time dashboard server
│   ├── models.py            # Model routing and Anthropic/OpenCode API calls
│   ├── schemas.py           # Pydantic models for all artifact types
│   ├── config.py            # Config management (~/.config/swarm/config.toml)
│   ├── context.py           # Context assembly for agent prompts
│   ├── artifacts.py         # .plan/ file I/O
│   ├── static/
│   │   └── dashboard.html   # Single-file dashboard UI (HTML + CSS + JS)
│   └── prompts/             # Agent prompt files
│       ├── 00_interviewer.md
│       ├── 00b_interviewer_refine.md
│       ├── 00_codebase_analyzer.md
│       ├── 01_decomposer.md
│       ├── 01b_decomposer_review.md
│       ├── 02_contract_writer.md
│       ├── 03_contract_resolver.md
│       ├── 04_adversary.md
│       ├── 05_adversary_resolver.md
│       ├── 06_sequencer.md
│       └── 07_simulator.md
└── pyproject.toml
```

## Dependencies

| Package     | Version  | Purpose                         |
|-------------|----------|---------------------------------|
| `click`     | >=8.1    | CLI framework                   |
| `pydantic`  | >=2.0    | Data validation and JSON schema |
| `anthropic` | >=0.39   | Anthropic API client            |
| `fastapi`   | >=0.100  | Dashboard web server            |
| `uvicorn`   | >=0.20   | ASGI server for dashboard       |

Python 3.11+ (uses `tomllib` from the standard library).

## License

MIT
