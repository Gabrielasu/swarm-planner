# Planning Swarm

A CLI tool that orchestrates multiple LLM agents in a sequential pipeline to produce implementation-ready software specifications. Feed it a project description and it outputs a complete architecture: component trees, interface contracts, an adversarially-tested design, and a sequenced task graph that coding agents can execute.

The intelligence lives in the prompts, not the infrastructure. The entire tool is ~2,400 lines of Python orchestration plus ~500 lines of prompt files.

## What It Produces

Given a project description, the pipeline outputs:

- **Structured brief** -- clarified and expanded from your input
- **Component tree** -- with ownership boundaries and data flows
- **Interface contracts** -- at every component boundary, implementation-grade
- **Adversarially-tested design** -- with documented decisions and resolved findings
- **Sequenced task graph** -- ordered, parallelizable, ready for coding agents
- **Readiness verdicts** -- whether each task can be one-shotted by a coding agent

All artifacts are written to `.plan/` in your project directory.

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
swarm status              # Show pipeline progress
swarm approve             # Continue after human review checkpoint
swarm plan --resume       # Resume after a crash or interruption
swarm rerun adversary     # Re-run from a specific step (invalidates downstream)
swarm reset               # Clear pipeline state (keeps artifacts)
swarm log                 # Show full planning log
```

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
[11]  Beads Export         Exports task graph to .beads/ for downstream consumption
```

The adversary loop runs up to 3 rounds by default (configurable with `--max-rounds`). It stops early if no critical or high-severity findings remain.

## Output

```
.plan/
├── .state.json              # Pipeline state (enables resume/rerun)
├── brief.md                 # Structured brief from the Interviewer
├── architecture.md          # Component tree with ownership and data flows
├── contracts/               # One file per interface contract
│   ├── auth-to-api.md
│   └── api-to-db.md
├── decisions.md             # Adversary findings and resolution rationale
├── tasks/                   # One file per implementation task
│   ├── 001-setup.md
│   └── 002-auth-core.md
└── simulation-report.md     # Readiness verdict for each task
```

The Beads export (step 11) writes a separate `.beads/` directory with a `manifest.json` and individual task files for the `bd` task runner.

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

**Resumable pipeline.** State is persisted to `.plan/.state.json` after every step. Recover from crashes with `swarm plan --resume`. Selectively re-run from any step with `swarm rerun <step>`, which automatically invalidates all downstream artifacts.

**Structured output.** All inter-agent artifacts are validated with Pydantic schemas. JSON schema instructions are appended to system prompts, and responses are parsed with robust extraction (markdown fence stripping, bracket matching, truncated-JSON repair).

**Human-in-the-loop.** The pipeline pauses at step 7 for human review of the architecture, contracts, and design decisions. Approve to continue, or re-run from any earlier step.

**Context engineering.** Each agent receives only the context it needs via `assemble_context()`. Prompt templates become system prompts; variables are injected as user messages wrapped in XML-style tags.

**Two LLM backends.** Direct Anthropic API (pay-as-you-go) or OpenCode CLI (uses an existing Claude subscription). The OpenCode backend shells out to `opencode run` and parses JSON-line events.

## Project Structure

```
swarm-planner/
├── swarm/
│   ├── cli.py              # Click CLI (init, plan, approve, rerun, status, log, reset)
│   ├── runner.py            # Pipeline orchestration engine
│   ├── models.py            # Model routing and Anthropic/OpenCode API calls
│   ├── schemas.py           # Pydantic models for all artifact types
│   ├── config.py            # Config management (~/.config/swarm/config.toml)
│   ├── context.py           # Context assembly for agent prompts
│   ├── artifacts.py         # .plan/ file I/O
│   ├── beads_bridge.py      # Beads export format
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
