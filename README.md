# Planning Swarm

A CLI tool that orchestrates multiple LLM agents in a sequential pipeline to produce implementation-ready software specifications. Feed it a project description and it outputs a complete architecture: component trees, interface contracts, an adversarially-tested design, and a **stateful task graph** optimized for AI agent consumption.

The intelligence lives in the prompts, not the infrastructure. The entire tool is ~2,400 lines of Python orchestration plus ~500 lines of prompt files.

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

The output is designed for AI agent consumption, not human reading. Instead of large markdown files, the plan uses a **two-phase loading** architecture:

### `graph.json` -- The Task DAG

A compact JSON file the agent loads first. Contains the full task topology, dependency graph, and completion status. Typically 50-100 tokens per task.

```json
{
  "project": "Real-time collaborative editor",
  "constraints": {"platform": "web", "language": "typescript"},
  "components": {
    "auth": {"owns": ["sessions", "tokens"], "interfaces": ["auth-to-api"]},
    "api":  {"owns": ["routes"], "interfaces": ["api-to-db", "auth-to-api"]}
  },
  "tasks": [
    {"id": "001", "title": "Project scaffold",  "component": "infra", "complexity": "trivial", "status": "done",  "depends": [],      "tokens": 1200},
    {"id": "002", "title": "Auth core",          "component": "auth",  "complexity": "standard","status": "ready", "depends": ["001"],  "tokens": 3400},
    {"id": "003", "title": "API gateway",        "component": "api",   "complexity": "complex", "status": "blocked","depends": ["001","002"], "tokens": 4100}
  ],
  "discoveries": [],
  "meta": {
    "total_tasks": 3, "done": 1, "ready": 1, "blocked": 1,
    "version": 2, "last_updated": "2026-03-17T10:30:00Z",
    "changelog": [...]
  }
}
```

### `tasks/{id}.json` -- Self-Contained Prompt Packets

Loaded on demand for the selected task only. Contains everything the coding agent needs -- contracts are **inlined**, not referenced. No cross-referencing, no loading architecture.md alongside it.

```json
{
  "id": "002",
  "title": "Auth core",
  "v": 1,
  "instruction": "Implement JWT authentication with login, logout, and token refresh.",
  "component": "auth",
  "complexity": "standard",
  "create": ["src/auth/service.ts", "src/auth/middleware.ts"],
  "modify": ["src/app.ts"],
  "context": ["src/config/env.ts"],
  "contracts": [
    {
      "boundary": "auth-to-api",
      "pattern": "synchronous_call",
      "fns": [
        {"name": "authenticate", "params": {"token": "string"}, "returns": {"user_id": "string"}, "errors": ["TokenExpired"]}
      ],
      "error_shape": {"error": "string", "code": "string"},
      "stub": "Return {user_id:'test-user'} for tokens starting with 'test-'"
    }
  ],
  "done_when": ["POST /auth/login returns JWT", "Middleware rejects expired tokens"],
  "not_this": ["OAuth/SSO", "Rate limiting"],
  "depends": ["001"],
  "unlocks": ["003"]
}
```

### Why This Format?

| Problem with markdown plans | How this solves it |
|---|---|
| Giant files clog the context window | Two-phase loading: read index (tiny), then one packet (small) |
| Cross-referencing contracts/architecture | Contracts are inlined into each task packet |
| ~35% of tokens are formatting | Compressed JSON with minimal keys |
| No way to track completion | `status` field with dependency cascade |
| Plans are write-once | Stateful: discoveries, updates, changelog |
| All-or-nothing loading | Agent reads only what it needs for the current task |

**Token efficiency:** A 20-task plan index is ~1,500-2,000 tokens in `graph.json` vs ~16,000-24,000 tokens loading all markdown task files.

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
swarm status --graph      # Detailed task graph view
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
swarm show 002            # Show a task's full prompt packet
swarm start 002           # Mark a task as in-progress
swarm done 002            # Mark a task as done (cascades: unblocks dependents)
swarm discover 003 "auth needs revokeToken" -t missing_contract_fn -a 002
                          # Report a discovery (flags affected tasks)
```

### How a Coding Agent Uses the Plan

1. **Load index**: Read `graph.json` (~2k tokens for a 20-task plan)
2. **Pick a task**: Filter for `"status": "ready"`, choose based on complexity/dependencies
3. **Load packet**: Read `tasks/{id}.json` (~300-500 tokens, self-contained)
4. **Implement**: Everything needed is in the packet -- contracts, files, acceptance criteria
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

### Discovery System

When a coding agent finds something during implementation that affects the plan, it records a **discovery**:

```bash
swarm discover 003 "auth-to-api needs revokeToken for logout" \
  -t missing_contract_fn \
  -a 002 \
  -s high
```

Discovery types: `missing_contract_fn`, `task_split_needed`, `dependency_missing`, `scope_change`, `blocker`

Discoveries cascade: affected tasks are automatically flagged as `invalidated` (if done) or `needs_update` (if pending/ready). The graph changelog tracks all mutations.

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
 [8]  Sequencer            Converts architecture into an ordered, parallelizable task graph
 [9]  Simulator            Checks if each task can be one-shotted by a coding agent
[10]  Refinement           Re-resolves contracts and tasks that failed readiness checks
[11]  Graph Export          Builds graph.json + self-contained task packets
```

The adversary loop runs up to 3 rounds by default (configurable with `--max-rounds`). It stops early if no critical or high-severity findings remain.

## Output Structure

```
.plan/
├── .state.json              # Pipeline state (enables resume/rerun)
├── brief.md                 # Structured brief from the Interviewer
├── review.md                # Compact review doc (architecture + contracts + decisions)
├── graph.json               # Stateful task DAG (THE primary output)
└── tasks/                   # Self-contained prompt packets (one per task)
    ├── 001.json
    ├── 002.json
    └── 003.json
```

No more `.beads/` directory, per-contract markdown files, per-task markdown files, or simulation reports. Everything is in `graph.json` + `tasks/*.json`.

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

You can use your Claude subscription through Opencode to run the tool. Alternatively, use the Anthropic API.
A typical run for a medium-complexity project (20-40 tasks) costs **$12-35** across 10-20 LLM calls. The Interviewer step adds negligible cost ($0.10-0.30) but significantly improves output quality for vague inputs.

## Design

**Stateful task graph.** The plan is a living document, not write-once markdown. Tasks have completion status, dependencies cascade automatically, and discoveries feed back into the plan. The graph version increments on every mutation with a full changelog.

**Two-phase loading.** Agents read the compact index first (~2k tokens), then load only the task packet they need (~300-500 tokens). Never loads the whole plan into context.

**Self-contained packets.** Each task's prompt packet inlines all relevant contracts, stubs, and error shapes. No cross-referencing between files.

**Token-budgeted.** Every packet knows its approximate token cost. Orchestrators can verify a task fits in the agent's context window before dispatch.

**Resumable pipeline.** State is persisted to `.plan/.state.json` after every step. Recover from crashes with `swarm plan --resume`. Selectively re-run from any step with `swarm rerun <step>`, which automatically invalidates all downstream artifacts.

**Structured output.** All inter-agent artifacts are validated with Pydantic schemas. JSON schema instructions are appended to system prompts, and responses are parsed with robust extraction (markdown fence stripping, bracket matching, truncated-JSON repair).

**Human-in-the-loop.** The pipeline pauses at step 7 for human review of the architecture, contracts, and design decisions. A consolidated `review.md` shows everything in one file. Approve to continue, or re-run from any earlier step.

**Context engineering.** Each agent receives only the context it needs via `assemble_context()`. Prompt templates become system prompts; variables are injected as user messages wrapped in XML-style tags.

**Two LLM backends.** Direct Anthropic API (pay-as-you-go) or OpenCode CLI (uses an existing Claude subscription). The OpenCode backend shells out to `opencode run` and parses JSON-line events.

## Project Structure

```
swarm-planner/
├── swarm/
│   ├── cli.py              # Click CLI (plan, status, done, discover, ready, show, etc.)
│   ├── runner.py            # Pipeline orchestration engine
│   ├── graph_builder.py     # Stateful task graph builder and manager
│   ├── models.py            # Model routing and Anthropic/OpenCode API calls
│   ├── schemas.py           # Pydantic models for all artifact types
│   ├── config.py            # Config management (~/.config/swarm/config.toml)
│   ├── context.py           # Context assembly for agent prompts
│   ├── artifacts.py         # .plan/ file I/O
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

Python 3.11+ (uses `tomllib` from the standard library).

## License

MIT
