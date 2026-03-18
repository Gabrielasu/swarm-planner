# Planning Swarm — Implementation Guide

## What You're Building

A CLI tool called `swarm` (or whatever you want to name it) that orchestrates 
eight LLM agents to produce implementation-ready specifications. It's a Python 
or TypeScript project — your choice. I'll describe it in Python since it's 
simpler for CLI tools, but the architecture is language-agnostic.

The tool is ~600-900 lines of orchestration code plus eight prompt files. 
That's it. The intelligence lives in the prompts, not in the infrastructure.

---

## Project Structure

```
planning-swarm/
├── swarm                     # CLI entry point (executable)
├── swarm/
│   ├── __init__.py
│   ├── cli.py                # CLI argument parsing and command routing
│   ├── runner.py             # Core orchestration engine
│   ├── models.py             # Model routing (which agent gets which model)
│   ├── context.py            # Context assembly for each agent
│   ├── artifacts.py          # Read/write/validate .plan/ artifacts
│   ├── graph_builder.py      # Build stateful task graph (graph.json + prompt packets)
│   └── schemas.py            # Pydantic models for all artifact types
├── prompts/
│   ├── 00_interviewer.md
│   ├── 01_decomposer.md
│   ├── 01b_decomposer_review.md
│   ├── 02_contract_writer.md
│   ├── 03_contract_resolver.md
│   ├── 04_adversary.md
│   ├── 05_adversary_resolver.md
│   ├── 06_sequencer.md
│   └── 07_simulator.md
├── .plan/                    # Generated output (gitignored until exported)
│   ├── .state.json           # Pipeline state (enables resume/rerun)
│   ├── brief.md              # Structured brief (Interviewer output)
│   ├── review.md             # Consolidated review doc (architecture + contracts + decisions)
│   ├── graph.json            # Stateful task DAG (THE primary output)
│   └── tasks/                # Self-contained prompt packets (one per task)
│       ├── 001.json
│       └── 002.json
├── pyproject.toml
└── README.md
```

---

## The Four Files That Matter Most

Everything else is plumbing. These four files are where the real work happens.

### 1. schemas.py — The Artifact Schemas

This is the Planning Swarm's own "interface contracts." Every artifact that
passes between agents has a defined structure. This is what the self-review
identified as the most critical missing piece.

```python
from pydantic import BaseModel
from enum import Enum
from typing import Optional


# ── Structured Brief (Interviewer output) ────────────────────────

class UserType(BaseModel):
    type: str                        # e.g. "editor", "viewer", "admin"
    primary_actions: list[str]       # What they do in the system
    access_level: str                # What they can see/modify

class DataEntity(BaseModel):
    name: str                        # e.g. "Document", "User", "Comment"
    description: str                 # What this represents
    relationships: list[str]         # e.g. ["owned by User", "contains Comments"]

class Assumption(BaseModel):
    assumption: str                  # What was assumed
    basis: str                       # Why this assumption was made
    overridable: bool                # Can the human change this?

class InterviewQuestion(BaseModel):
    dimension: str                   # Which of the 5 dimensions this covers
    question: str                    # The specific question
    default_assumption: str          # What we'll assume if they don't answer
    why_it_matters: str              # Why the Decomposer needs this

class InterviewerOutput(BaseModel):
    brief_sufficient: bool           # True = skip questions, go to Decomposer
    structured_brief: "StructuredBrief"
    questions: list[InterviewQuestion]  # Empty if brief_sufficient

class StructuredBrief(BaseModel):
    system_purpose: str              # One sentence: what this system does
    user_types: list[UserType]       # Distinct interaction patterns
    core_data_entities: list[DataEntity]  # The nouns of the system
    scope_boundary: list[str]        # Explicit "NOT building" list
    technical_constraints: dict      # {platform, existing_codebase,
                                     #  mandated_tech, deployment}
    assumptions_made: list[Assumption]   # Gaps filled by Interviewer
    deferred_decisions: list[str]    # Things to decide later (not now)


# ── Component Tree (Decomposer output) ──────────────────────────

class Component(BaseModel):
    id: str                          # e.g. "auth", "api", "database"
    name: str                        # Human-readable name
    responsibility: str              # Single sentence: what this owns
    owns_data: list[str]             # Data entities this component owns
    depends_on: list[str]            # Component IDs this depends on
    exposes_to: list[str]            # Component IDs this exposes interfaces to

class ComponentTree(BaseModel):
    components: list[Component]
    data_flows: list[dict]           # [{from, to, data_description}]
    rationale: str                   # Why this decomposition


# ── Interface Contracts (Contract Writer output) ─────────────────

class ErrorCase(BaseModel):
    error_type: str                  # e.g. "NotFoundError"
    condition: str                   # When this error occurs
    response_shape: dict             # What the error looks like
    propagation: str                 # How callers should handle it

class CommunicationPattern(str, Enum):
    SYNC_CALL = "synchronous_call"
    ASYNC_EVENT = "async_event"
    HTTP_REQUEST = "http_request"
    SHARED_STATE = "shared_state"
    MESSAGE_QUEUE = "message_queue"

class StubStrategy(BaseModel):
    can_stub: bool                   # Can this be mocked for parallel dev?
    stub_description: str            # What the stub returns

class InterfaceContract(BaseModel):
    boundary_id: str                 # e.g. "auth-to-api"
    from_component: str              # Component ID
    to_component: str                # Component ID
    communication_pattern: CommunicationPattern
    functions: list[dict]            # [{name, params: [{name, type, required}], 
                                     #   returns: {type, shape}, preconditions, 
                                     #   postconditions}]
    error_cases: list[ErrorCase]
    data_schemas: dict               # Named type definitions used in this contract
    stub_strategy: StubStrategy


# ── Adversary Critique ───────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"

class Finding(BaseModel):
    severity: Severity
    affected_components: list[str]   # Component IDs
    affected_contracts: list[str]    # Boundary IDs
    finding: str                     # What's wrong
    evidence: str                    # Why this is a problem
    suggested_direction: str         # Hint for the Adversary Resolver


# ── Adversary Resolver Output ────────────────────────────────────

class Resolution(BaseModel):
    finding_index: int               # Which finding this resolves
    action: str                      # "revised" | "deferred" | "acknowledged"
    changes_made: str                # What was changed
    rationale: str                   # Why this resolution


# ── Task Graph (Sequencer output) ────────────────────────────────

class Complexity(str, Enum):
    TRIVIAL = "trivial"              # Boilerplate, config
    STANDARD = "standard"            # Single component, clear spec
    COMPLEX = "complex"              # Multiple interactions
    HARD = "hard"                    # Novel algorithms, security-critical

class Task(BaseModel):
    id: str                          # e.g. "001"
    title: str
    component: str                   # Which component this implements
    description: str                 # What to build
    contracts_to_satisfy: list[str]  # Boundary IDs
    files_to_create: list[str]       # New files
    files_to_modify: list[str]       # Existing files to change
    context_files: list[str]         # Files to read for context (not modify)
    acceptance_criteria: list[str]   # Machine-verifiable assertions
    not_in_scope: list[str]          # Explicit exclusions
    depends_on: list[str]            # Task IDs that must complete first
    complexity: Complexity
    estimated_tokens: Optional[int]  # Rough context budget needed


# ── Simulator Verdict ────────────────────────────────────────────

class Readiness(str, Enum):
    READY = "ready"                  # Can be one-shotted
    NEEDS_REFINEMENT = "needs_refinement"
    BLOCKED = "blocked"

class TaskVerdict(BaseModel):
    task_id: str
    readiness: Readiness
    gaps: list[str]                  # Specific missing information
    refinement_target: Optional[str] # Which contract/component needs work
```

### 2. runner.py — The Orchestration Engine

Three key design decisions in this file:

1. **Automatic flow**: `run()` executes all steps in sequence without 
   manual triggering. The only pause is the human review checkpoint — the 
   pipeline stops, you review offline, then `swarm approve` continues.

2. **Resumable**: After every step, pipeline state saves to 
   `.plan/.state.json`. Ctrl+C, crash, or connection loss? 
   `swarm plan --resume` picks up from the last completed step. 
   `swarm rerun <step>` reruns a specific step and invalidates everything 
   after it.

3. **Live progress**: A spinner with elapsed time shows during every LLM 
   call. You see which agent is working and how long it's been thinking, 
   but not a token-by-token stream.

```python
import json
import time
import sys
import threading
from pathlib import Path
from datetime import datetime
from enum import Enum as PyEnum
from .models import call_model, ModelTier
from .context import assemble_context
from .artifacts import save_artifact, load_artifact
from .schemas import *
from .graph_builder import build_graph


# ── Pipeline Steps ───────────────────────────────────────────────

class Step(str, PyEnum):
    INTERVIEW = "interview"
    CODEBASE = "codebase_analysis"
    DECOMPOSE = "decompose"
    WRITE_CONTRACTS = "write_contracts"
    RESOLVE_CONTRACTS = "resolve_contracts"
    ADVERSARY = "adversary"
    HUMAN_REVIEW = "human_review"
    SEQUENCE = "sequence"
    SIMULATE = "simulate"
    REFINE = "refine"
    EXPORT = "graph_export"

STEP_ORDER = list(Step)

STEP_LABELS = {
    Step.INTERVIEW:         "🎤 Interviewer",
    Step.CODEBASE:          "📂 Codebase Analysis",
    Step.DECOMPOSE:         "🧱 Decomposer",
    Step.WRITE_CONTRACTS:   "📝 Contract Writer",
    Step.RESOLVE_CONTRACTS: "🔍 Contract Resolver",
    Step.ADVERSARY:         "⚔️  Adversary Loop",
    Step.HUMAN_REVIEW:      "🧑 Human Review",
    Step.SEQUENCE:          "📋 Sequencer",
    Step.SIMULATE:          "🧪 Simulator",
    Step.REFINE:            "🔄 Refinement",
    Step.EXPORT:            "📦 Graph Export",
}


# ── Progress Spinner ─────────────────────────────────────────────

class ProgressSpinner:
    """Non-blocking spinner with elapsed time. Shows you it's not stuck."""
    
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    
    def __init__(self, agent_name: str, detail: str = ""):
        self.agent_name = agent_name
        self.detail = detail
        self._running = False
        self._thread = None
        self._start_time = None
    
    def start(self):
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
    
    def stop(self, summary: str = ""):
        self._running = False
        if self._thread:
            self._thread.join()
        elapsed = time.time() - self._start_time
        sys.stderr.write(f"\r\033[K")  # clear spinner line
        sys.stderr.flush()
        if summary:
            print(f"   ✓ {summary} ({elapsed:.1f}s)")
    
    def _spin(self):
        i = 0
        while self._running:
            elapsed = time.time() - self._start_time
            frame = self.FRAMES[i % len(self.FRAMES)]
            detail = f" — {self.detail}" if self.detail else ""
            sys.stderr.write(
                f"\r   {frame} {self.agent_name}{detail} [{elapsed:.0f}s]"
            )
            sys.stderr.flush()
            time.sleep(0.1)
            i += 1


# ── Pipeline State ───────────────────────────────────────────────

class PipelineState:
    """Persisted to .plan/.state.json. Enables resume and rerun."""
    
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.data = self._load()
    
    def _load(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {
            "completed_steps": [],
            "current_step": None,
            "started_at": None,
            "outputs": {},
            "adversary_round": 0,
            "log": [],
        }
    
    def save(self):
        self.state_file.write_text(json.dumps(self.data, indent=2))
    
    def is_complete(self, step: Step) -> bool:
        return step.value in self.data["completed_steps"]
    
    def mark_complete(self, step: Step, output_data: dict = None,
                      log_msg: str = ""):
        if step.value not in self.data["completed_steps"]:
            self.data["completed_steps"].append(step.value)
        if output_data:
            self.data["outputs"][step.value] = output_data
        self.data["current_step"] = None
        if log_msg:
            self.data["log"].append({
                "time": datetime.now().isoformat(),
                "step": step.value,
                "message": log_msg,
            })
        self.save()
    
    def mark_started(self, step: Step):
        self.data["current_step"] = step.value
        self.save()
    
    def get_output(self, step: Step) -> dict | None:
        return self.data["outputs"].get(step.value)
    
    def get_resume_point(self) -> Step | None:
        for step in STEP_ORDER:
            if not self.is_complete(step):
                return step
        return None
    
    def invalidate_from(self, step: Step):
        """Invalidate this step and everything after it."""
        idx = STEP_ORDER.index(step)
        to_remove = {s.value for s in STEP_ORDER[idx:]}
        self.data["completed_steps"] = [
            s for s in self.data["completed_steps"] if s not in to_remove
        ]
        for s in to_remove:
            self.data["outputs"].pop(s, None)
        self.save()
    
    def print_status(self):
        for step in STEP_ORDER:
            if self.is_complete(step):
                icon = "✅"
            elif self.data["current_step"] == step.value:
                icon = "🔄"
            else:
                icon = "⬜"
            print(f"  {icon} {STEP_LABELS[step]}")


# ── The Swarm ────────────────────────────────────────────────────

class PlanningSwarm:
    def __init__(self, project_dir: Path, config: dict):
        self.project_dir = project_dir
        self.plan_dir = project_dir / ".plan"
        self.config = config
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        (self.plan_dir / "contracts").mkdir(exist_ok=True)
        (self.plan_dir / "tasks").mkdir(exist_ok=True)
        self.state = PipelineState(self.plan_dir / ".state.json")

    def _agent(self, name, prompt, variables, tier, fmt=None, detail=""):
        """Run an agent with a progress spinner."""
        spinner = ProgressSpinner(name, detail)
        spinner.start()
        try:
            ctx = assemble_context(prompt_file=prompt, variables=variables)
            return call_model(ctx, tier=tier, response_format=fmt)
        finally:
            spinner.stop()

    # ── Step 0a: Interview ──────────────────────────────────────

    def interview(self, raw_input: str) -> StructuredBrief:
        """Analyze input, ask clarifying questions if needed, 
        produce a normalized StructuredBrief."""

        # Phase 1: Analyze the raw input
        context = assemble_context(
            prompt_file="prompts/00_interviewer.md",
            variables={"raw_input": raw_input}
        )

        result = call_model(context, tier=ModelTier.FAST,
                          response_format=InterviewerOutput)

        if result.brief_sufficient:
            # Input was detailed enough — no questions needed
            print("   ✓ Brief is sufficient. No clarification needed.")
            save_artifact(self.plan_dir / "brief.md",
                         self._render_brief(result.structured_brief))
            return result.structured_brief

        # Phase 2: Present questions to the human
        brief = result.structured_brief
        questions = result.questions

        print(f"\n   Found {len(questions)} areas that need clarification.\n")
        print("   For each question, type your answer or press Enter")
        print("   to accept the default assumption.\n")

        answers = {}
        for i, q in enumerate(questions):
            print(f"   [{i+1}/{len(questions)}] {q.question}")
            print(f"   Default: {q.default_assumption}")
            answer = input("   → ").strip()
            if answer:
                answers[q.dimension] = answer
            else:
                print("   (using default)")
            print()

        # Phase 3: Rebuild the brief with answers incorporated
        if answers:
            refine_context = assemble_context(
                prompt_file="prompts/00b_interviewer_refine.md",
                variables={
                    "original_brief": brief.model_dump_json(indent=2),
                    "questions_and_answers": json.dumps(
                        {q.question: answers.get(q.dimension, q.default_assumption) 
                         for q in questions}, indent=2
                    ),
                }
            )
            brief = call_model(refine_context, tier=ModelTier.FAST,
                             response_format=StructuredBrief)

        # Show assumptions for quick review
        if brief.assumptions_made:
            print("   Assumptions made (review quickly):")
            for a in brief.assumptions_made:
                print(f"   • {a.assumption}")
            print()
            override = input("   Any corrections? (type or Enter to accept all): ").strip()
            if override:
                # Quick re-run with the correction as additional context
                brief.assumptions_made = [
                    a for a in brief.assumptions_made 
                    if override.lower() not in a.assumption.lower()
                ]
                # TODO: more sophisticated override handling

        save_artifact(self.plan_dir / "brief.md",
                     self._render_brief(brief))
        return brief

    # ── Step 0b (optional): Codebase Analysis ────────────────────

    def analyze_codebase(self) -> str | None:
        """For brownfield projects. Scans existing code, produces summary."""
        if not self.config.get("existing_codebase"):
            return None

        codebase_path = self.config["existing_codebase"]
        # Read directory tree + key files (package.json, schema files, 
        # main entry points). Don't dump the whole codebase — just 
        # enough for structural understanding.
        tree = self._scan_directory_tree(codebase_path)
        key_files = self._read_key_files(codebase_path)

        context = assemble_context(
            prompt_file="prompts/00_codebase_analyzer.md",
            variables={"directory_tree": tree, "key_files": key_files}
        )

        result = call_model(context, tier=ModelTier.CODING)
        save_artifact(self.plan_dir / "codebase_summary.md", result)
        return result

    # ── Step 1: Decompose ────────────────────────────────────────

    def decompose(self, brief: StructuredBrief, 
                  codebase_summary: str = None) -> ComponentTree:
        """Break the system into components."""
        context = assemble_context(
            prompt_file="prompts/01_decomposer.md",
            variables={
                "brief": brief.model_dump_json(indent=2),
                "codebase_summary": codebase_summary or "Greenfield project.",
            }
        )

        result = call_model(context, tier=ModelTier.FRONTIER, 
                          response_format=ComponentTree)

        # Self-review: Decomposer checks its own output
        review_context = assemble_context(
            prompt_file="prompts/01b_decomposer_review.md",
            variables={"component_tree": result.model_dump_json(indent=2)}
        )
        reviewed = call_model(review_context, tier=ModelTier.FRONTIER,
                            response_format=ComponentTree)

        save_artifact(self.plan_dir / "architecture.md", 
                     self._render_architecture(reviewed))
        return reviewed

    # ── Step 2: Write Contracts ──────────────────────────────────

    def write_contracts(self, tree: ComponentTree) -> list[InterfaceContract]:
        """Create draft interface contracts for every boundary."""
        context = assemble_context(
            prompt_file="prompts/02_contract_writer.md",
            variables={"component_tree": tree.model_dump_json(indent=2)}
        )

        result = call_model(context, tier=ModelTier.CODING,
                          response_format=list[InterfaceContract])

        for contract in result:
            save_artifact(
                self.plan_dir / "contracts" / f"{contract.boundary_id}.md",
                self._render_contract(contract)
            )
        return result

    # ── Step 3: Resolve Contracts ────────────────────────────────

    def resolve_contracts(self, tree: ComponentTree, 
                          contracts: list[InterfaceContract]
                         ) -> list[InterfaceContract]:
        """Make contracts implementation-grade."""
        context = assemble_context(
            prompt_file="prompts/03_contract_resolver.md",
            variables={
                "component_tree": tree.model_dump_json(indent=2),
                "contracts": json.dumps(
                    [c.model_dump() for c in contracts], indent=2
                ),
            }
        )

        result = call_model(context, tier=ModelTier.CODING,
                          response_format=list[InterfaceContract])

        # Overwrite drafts with resolved versions
        for contract in result:
            save_artifact(
                self.plan_dir / "contracts" / f"{contract.boundary_id}.md",
                self._render_contract(contract)
            )
        return result

    # ── Step 4: Adversarial Review ───────────────────────────────

    def run_adversary(self, tree: ComponentTree,
                      contracts: list[InterfaceContract]
                     ) -> list[Finding]:
        """Try to break the plan."""
        context = assemble_context(
            prompt_file="prompts/04_adversary.md",
            variables={
                "component_tree": tree.model_dump_json(indent=2),
                "contracts": json.dumps(
                    [c.model_dump() for c in contracts], indent=2
                ),
            }
        )

        result = call_model(context, tier=ModelTier.FRONTIER,
                          response_format=list[Finding])
        return result

    # ── Step 5: Resolve Adversary Findings ───────────────────────

    def resolve_adversary(self, tree: ComponentTree,
                          contracts: list[InterfaceContract],
                          findings: list[Finding]
                         ) -> tuple[ComponentTree, list[InterfaceContract], 
                                    list[Resolution]]:
        """Address each finding from the Adversary."""
        context = assemble_context(
            prompt_file="prompts/05_adversary_resolver.md",
            variables={
                "component_tree": tree.model_dump_json(indent=2),
                "contracts": json.dumps(
                    [c.model_dump() for c in contracts], indent=2
                ),
                "findings": json.dumps(
                    [f.model_dump() for f in findings], indent=2
                ),
            }
        )

        # The Adversary Resolver returns revised tree + contracts + 
        # resolution log
        result = call_model(context, tier=ModelTier.FRONTIER,
                          response_format=dict)  # Custom parsing needed

        revised_tree = ComponentTree(**result["revised_tree"])
        revised_contracts = [InterfaceContract(**c) 
                           for c in result["revised_contracts"]]
        resolutions = [Resolution(**r) for r in result["resolutions"]]

        # Save updated artifacts
        save_artifact(self.plan_dir / "architecture.md",
                     self._render_architecture(revised_tree))
        for contract in revised_contracts:
            save_artifact(
                self.plan_dir / "contracts" / f"{contract.boundary_id}.md",
                self._render_contract(contract)
            )
        save_artifact(self.plan_dir / "decisions.md",
                     self._render_decisions(resolutions))

        return revised_tree, revised_contracts, resolutions

    # ── Step 6: Sequence into Tasks ──────────────────────────────

    def sequence(self, tree: ComponentTree,
                 contracts: list[InterfaceContract]
                ) -> list[Task]:
        """Convert architecture into ordered task graph."""
        context = assemble_context(
            prompt_file="prompts/06_sequencer.md",
            variables={
                "component_tree": tree.model_dump_json(indent=2),
                "contracts": json.dumps(
                    [c.model_dump() for c in contracts], indent=2
                ),
            }
        )

        result = call_model(context, tier=ModelTier.CODING,
                          response_format=list[Task])

        for task in result:
            save_artifact(
                self.plan_dir / "tasks" / f"{task.id}-{task.title.lower().replace(' ', '-')}.md",
                self._render_task(task)
            )
        return result

    # ── Step 7: Simulate Readiness ───────────────────────────────

    def simulate(self, tree: ComponentTree,
                 contracts: list[InterfaceContract],
                 tasks: list[Task]
                ) -> list[TaskVerdict]:
        """Check if each task can be one-shotted."""
        context = assemble_context(
            prompt_file="prompts/07_simulator.md",
            variables={
                "component_tree": tree.model_dump_json(indent=2),
                "contracts": json.dumps(
                    [c.model_dump() for c in contracts], indent=2
                ),
                "tasks": json.dumps(
                    [t.model_dump() for t in tasks], indent=2
                ),
            }
        )

        result = call_model(context, tier=ModelTier.FRONTIER,
                          response_format=list[TaskVerdict])

        save_artifact(self.plan_dir / "simulation-report.md",
                     self._render_simulation(result))
        return result

    # ── Full Orchestration ───────────────────────────────────────
    # 
    # run() drives the entire pipeline. Each step:
    #   1. Checks if already complete (skip if resuming)
    #   2. Marks itself as started (so crash = resume from here)
    #   3. Runs the agent with a progress spinner
    #   4. Saves output to state + artifacts
    #   5. Marks complete
    #
    # Human review pauses the pipeline. `swarm approve` continues.
    # `swarm rerun <step>` invalidates from that step and re-runs.

    def _serialize(self, **kwargs) -> dict:
        """Serialize Pydantic models for state storage."""
        out = {}
        for k, v in kwargs.items():
            if isinstance(v, list):
                out[k] = [x.model_dump() if hasattr(x, 'model_dump') 
                         else x for x in v]
            elif hasattr(v, 'model_dump'):
                out[k] = v.model_dump()
            else:
                out[k] = v
        return out

    def _load_brief(self) -> StructuredBrief:
        return StructuredBrief(**self.state.get_output(Step.INTERVIEW)["brief"])

    def _load_tree(self) -> ComponentTree:
        for step in [Step.ADVERSARY, Step.DECOMPOSE]:
            data = self.state.get_output(step)
            if data and "tree" in data:
                return ComponentTree(**data["tree"])
        raise ValueError("No component tree found in state")

    def _load_contracts(self) -> list[InterfaceContract]:
        for step in [Step.ADVERSARY, Step.RESOLVE_CONTRACTS, 
                     Step.WRITE_CONTRACTS]:
            data = self.state.get_output(step)
            if data and "contracts" in data:
                return [InterfaceContract(**c) for c in data["contracts"]]
        return []

    def _load_tasks(self) -> list[Task]:
        for step in [Step.REFINE, Step.SEQUENCE]:
            data = self.state.get_output(step)
            if data and "tasks" in data:
                return [Task(**t) for t in data["tasks"]]
        return []

    def run(self, raw_input: str = None, resume: bool = False,
            from_step: str = None):
        """Run the complete planning pipeline.
        
        Three modes:
          swarm plan brief.md         → fresh run
          swarm plan --resume         → pick up where we left off
          swarm rerun decompose       → rerun from a specific step
        """
        if from_step:
            step = Step(from_step)
            self.state.invalidate_from(step)
            print(f"   Rerunning from {STEP_LABELS[step]}. "
                  f"Subsequent steps invalidated.\n")

        if resume or from_step:
            rp = self.state.get_resume_point()
            if rp is None:
                print("   All steps complete. Nothing to resume.")
                return
            print(f"   Resuming from {STEP_LABELS[rp]}\n")
            self.state.print_status()
            print()

        total = len(STEP_ORDER)
        start_time = time.time()

        def header(step):
            idx = STEP_ORDER.index(step) + 1
            print(f"\n{'─' * 50}")
            print(f"  [{idx}/{total}] {STEP_LABELS[step]}")
            print(f"{'─' * 50}")

        def cached(step, info=""):
            msg = f"  ⏭️  {STEP_LABELS[step]} (cached)"
            if info:
                msg += f": {info}"
            print(msg)

        # ── INTERVIEW ──────────────────────────────────────────
        if not self.state.is_complete(Step.INTERVIEW):
            header(Step.INTERVIEW)
            self.state.mark_started(Step.INTERVIEW)
            brief = self.interview(raw_input)
            self.state.mark_complete(
                Step.INTERVIEW,
                output_data=self._serialize(brief=brief),
                log_msg=f"Brief: {brief.system_purpose}")
        else:
            brief = self._load_brief()
            cached(Step.INTERVIEW, brief.system_purpose)

        # ── CODEBASE ANALYSIS ──────────────────────────────────
        if not self.state.is_complete(Step.CODEBASE):
            header(Step.CODEBASE)
            self.state.mark_started(Step.CODEBASE)
            summary = self.analyze_codebase()
            self.state.mark_complete(
                Step.CODEBASE,
                output_data={"summary": summary},
                log_msg="Analyzed" if summary else "Greenfield (skipped)")
        else:
            summary = (self.state.get_output(Step.CODEBASE) or {}).get("summary")
            cached(Step.CODEBASE)

        # ── DECOMPOSE ─────────────────────────────────────────
        if not self.state.is_complete(Step.DECOMPOSE):
            header(Step.DECOMPOSE)
            self.state.mark_started(Step.DECOMPOSE)
            tree = self.decompose(brief, summary)
            self.state.mark_complete(
                Step.DECOMPOSE,
                output_data=self._serialize(tree=tree),
                log_msg=f"{len(tree.components)} components")
        else:
            tree = self._load_tree()
            cached(Step.DECOMPOSE, f"{len(tree.components)} components")

        # ── WRITE CONTRACTS ───────────────────────────────────
        if not self.state.is_complete(Step.WRITE_CONTRACTS):
            header(Step.WRITE_CONTRACTS)
            self.state.mark_started(Step.WRITE_CONTRACTS)
            contracts = self.write_contracts(tree)
            self.state.mark_complete(
                Step.WRITE_CONTRACTS,
                output_data=self._serialize(contracts=contracts),
                log_msg=f"{len(contracts)} drafts")
        else:
            contracts = self._load_contracts()
            cached(Step.WRITE_CONTRACTS, f"{len(contracts)} contracts")

        # ── RESOLVE CONTRACTS ─────────────────────────────────
        if not self.state.is_complete(Step.RESOLVE_CONTRACTS):
            header(Step.RESOLVE_CONTRACTS)
            self.state.mark_started(Step.RESOLVE_CONTRACTS)
            contracts = self.resolve_contracts(tree, contracts)
            self.state.mark_complete(
                Step.RESOLVE_CONTRACTS,
                output_data=self._serialize(contracts=contracts),
                log_msg=f"{len(contracts)} resolved")
        else:
            contracts = self._load_contracts()
            cached(Step.RESOLVE_CONTRACTS)

        # ── ADVERSARY LOOP ────────────────────────────────────
        if not self.state.is_complete(Step.ADVERSARY):
            header(Step.ADVERSARY)
            self.state.mark_started(Step.ADVERSARY)
            max_rounds = self.config.get("max_adversary_rounds", 3)

            for i in range(self.state.data.get("adversary_round", 0), 
                          max_rounds):
                print(f"\n   Round {i + 1}/{max_rounds}")
                findings = self.run_adversary(tree, contracts)
                critical = [f for f in findings 
                           if f.severity in (Severity.CRITICAL, Severity.HIGH)]
                print(f"   → {len(findings)} findings "
                      f"({len(critical)} critical/high)")

                if not critical:
                    print("   ✓ Plan is stable.")
                    break

                tree, contracts, _ = self.resolve_adversary(
                    tree, contracts, findings)
                contracts = self.resolve_contracts(tree, contracts)
                self.state.data["adversary_round"] = i + 1
                self.state.save()  # checkpoint within the loop

            self.state.mark_complete(
                Step.ADVERSARY,
                output_data=self._serialize(tree=tree, contracts=contracts),
                log_msg=f"Stable after {i + 1} round(s)")
        else:
            tree = self._load_tree()
            contracts = self._load_contracts()
            cached(Step.ADVERSARY)

        # ── HUMAN REVIEW ──────────────────────────────────────
        if not self.state.is_complete(Step.HUMAN_REVIEW):
            header(Step.HUMAN_REVIEW)
            self.state.mark_started(Step.HUMAN_REVIEW)
            print(f"\n   Review the plan in: {self.plan_dir}/")
            print(f"   • architecture.md     — component tree")
            print(f"   • contracts/          — interface contracts")
            print(f"   • decisions.md        — rationale")
            print(f"\n   Then run:")
            print(f"     swarm approve         → continue pipeline")
            print(f"     swarm rerun adversary → re-run adversary loop")
            return  # ← Pipeline pauses here

        # ── SEQUENCE ──────────────────────────────────────────
        if not self.state.is_complete(Step.SEQUENCE):
            header(Step.SEQUENCE)
            self.state.mark_started(Step.SEQUENCE)
            tasks = self.sequence(tree, contracts)
            self.state.mark_complete(
                Step.SEQUENCE,
                output_data=self._serialize(tasks=tasks),
                log_msg=f"{len(tasks)} tasks")
        else:
            tasks = self._load_tasks()
            cached(Step.SEQUENCE, f"{len(tasks)} tasks")

        # ── SIMULATE ──────────────────────────────────────────
        if not self.state.is_complete(Step.SIMULATE):
            header(Step.SIMULATE)
            self.state.mark_started(Step.SIMULATE)
            verdicts = self.simulate(tree, contracts, tasks)
            ready = sum(1 for v in verdicts if v.readiness == Readiness.READY)
            self.state.mark_complete(
                Step.SIMULATE,
                output_data={"verdicts": [v.model_dump() for v in verdicts]},
                log_msg=f"{ready}/{len(verdicts)} ready")
        else:
            vdata = self.state.get_output(Step.SIMULATE)["verdicts"]
            verdicts = [TaskVerdict(**v) for v in vdata]
            cached(Step.SIMULATE)

        # ── REFINE ────────────────────────────────────────────
        needs_work = [v for v in verdicts 
                     if v.readiness == Readiness.NEEDS_REFINEMENT]
        if needs_work and not self.state.is_complete(Step.REFINE):
            header(Step.REFINE)
            self.state.mark_started(Step.REFINE)
            print(f"   {len(needs_work)} tasks need refinement")
            contracts = self.resolve_contracts(tree, contracts)
            tasks = self.sequence(tree, contracts)
            verdicts = self.simulate(tree, contracts, tasks)
            still_bad = sum(1 for v in verdicts 
                          if v.readiness == Readiness.NEEDS_REFINEMENT)
            self.state.mark_complete(
                Step.REFINE,
                output_data=self._serialize(
                    tasks=tasks, contracts=contracts,
                    verdicts=verdicts),
                log_msg=f"{still_bad} still need work" 
                       if still_bad else "All ready")
        elif not needs_work:
            self.state.mark_complete(Step.REFINE, log_msg="Not needed")

        # ── GRAPH EXPORT ──────────────────────────────────────
        if not self.state.is_complete(Step.EXPORT):
            header(Step.EXPORT)
            self.state.mark_started(Step.EXPORT)
            build_graph(brief, tree, tasks, contracts, verdicts, self.project_dir)
            self.state.mark_complete(
                Step.EXPORT, log_msg=f"{len(tasks)} tasks exported")

        # ── DONE ──────────────────────────────────────────────
        elapsed = time.time() - start_time
        ready = sum(1 for v in verdicts if v.readiness == Readiness.READY)
        print(f"\n{'═' * 50}")
        print(f"  ✅ Planning complete ({elapsed:.0f}s)")
        print(f"{'═' * 50}")
        print(f"  Plan:    {self.plan_dir}/")
        print(f"  Tasks:   {len(tasks)} total, {ready} ready")
        print(f"  Next:    bd ready --json")
        print()
        self.state.print_status()
```

### 3. models.py — Model Routing

```python
import os
from enum import Enum
from anthropic import Anthropic


class ModelTier(str, Enum):
    FRONTIER = "frontier"    # Opus / o3 — architecture, adversary, decisions
    CODING = "coding"        # Sonnet / GPT-5 — contracts, sequencing
    FAST = "fast"            # Haiku / mini — compression, summaries


# Configure these to your preference
MODEL_MAP = {
    ModelTier.FRONTIER: "claude-opus-4-6",
    ModelTier.CODING: "claude-sonnet-4-6",
    ModelTier.FAST: "claude-haiku-4-5-20251001",
}


def call_model(context: str, tier: ModelTier, 
               response_format=None) -> str | dict:
    """Call the appropriate model for this tier.
    
    If response_format is a Pydantic model or list thereof,
    instruct the model to return JSON matching that schema
    and parse the response.
    """
    client = Anthropic()  # Uses ANTHROPIC_API_KEY env var
    model = MODEL_MAP[tier]

    system_prompt = context["system"]
    user_message = context["user"]

    # If we need structured output, append schema to system prompt
    if response_format:
        schema_instruction = _build_schema_instruction(response_format)
        system_prompt += "\n\n" + schema_instruction

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    text = response.content[0].text

    if response_format:
        return _parse_structured(text, response_format)
    return text


def _build_schema_instruction(response_format) -> str:
    """Generate JSON schema instruction from Pydantic model."""
    # Extract the JSON schema from the Pydantic model
    # and instruct the model to respond only in that format
    if hasattr(response_format, "model_json_schema"):
        schema = response_format.model_json_schema()
    else:
        # For list[Model] or dict, handle generics
        schema = str(response_format)

    return (
        "RESPOND ONLY WITH VALID JSON matching this schema. "
        "No preamble, no markdown fences, no explanation.\n\n"
        f"Schema: {schema}"
    )


def _parse_structured(text: str, response_format):
    """Parse model response into structured format."""
    import json
    # Strip any accidental markdown fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
        cleaned = cleaned.rsplit("```", 1)[0]
    return json.loads(cleaned)
```

### 4. context.py — Context Assembly

This is where the context engineering happens. Each agent gets exactly the 
tokens it needs and nothing more.

```python
from pathlib import Path


PROMPT_DIR = Path(__file__).parent.parent / "prompts"


def assemble_context(prompt_file: str, variables: dict) -> dict:
    """Build a context packet for an agent call.
    
    Returns {"system": str, "user": str} ready for the model.
    
    The prompt file contains the agent's identity and instructions
    (becomes the system prompt). The variables contain the specific
    input for this call (becomes the user message).
    """
    # Read the prompt template
    prompt_path = PROMPT_DIR / Path(prompt_file).name
    prompt_template = prompt_path.read_text()

    # The system prompt is the agent's identity and instructions
    system = prompt_template

    # The user message is the assembled variables
    user_parts = []
    for key, value in variables.items():
        if value:
            user_parts.append(f"<{key}>\n{value}\n</{key}>")

    user = "\n\n".join(user_parts)

    return {"system": system, "user": user}
```

---

## The Seven Prompts

These are the actual intelligence of the system. Each one is a markdown file
in `prompts/`. I'll give you the full prompt for each agent.

### prompts/00_interviewer.md

```markdown
You are the Interviewer — your job is to ensure the planning pipeline has 
enough information to begin. You are the first agent in a multi-agent 
planning system. The next agent (the Decomposer) needs specific information 
to break a system into components.

## Your Task

Read the project description and evaluate it against five dimensions:

1. SYSTEM PURPOSE: Do I know what this system does in one clear sentence?
   Not a feature list — the core value proposition.

2. USER TYPES: Do I know who uses it and their distinct interaction patterns?
   Not personas — but distinct types of access and behavior. "Editors and 
   viewers" is enough. "Users" is not.

3. CORE DATA ENTITIES: Do I know the key nouns of the system?
   Documents, users, orders, messages — the things the system stores and 
   manages. These become ownership boundaries for components.

4. SCOPE BOUNDARY: Do I know what this system is NOT?
   Without explicit exclusions, the planner will invent authentication, 
   analytics, admin panels, and notification systems whether wanted or not.

5. TECHNICAL CONSTRAINTS: Do I know the platform and hard requirements?
   Web app, CLI, mobile? Existing codebase? Mandated technologies? 
   Deployment environment?

## Decision Logic

If all five dimensions are CLEAR from the input:
→ Set brief_sufficient = true
→ Output a complete StructuredBrief
→ Set questions = [] (empty)

If any dimensions have GAPS:
→ Set brief_sufficient = false
→ Output a StructuredBrief with your best assumptions filled in
→ Output targeted questions (MAX 8) for the gaps that matter most

## Rules for Questions

1. MAX 8 QUESTIONS. If the input is so vague you'd need more than 8 
   questions, set brief_sufficient = false and fill in your best-guess 
   StructuredBrief anyway. The human will review assumptions.

2. Every question MUST include a default assumption.
   Format: "I'm assuming X. Is that right, or would you prefer Y?"
   This lets the human just press Enter for things they don't care about.

3. ONLY ask about things the DECOMPOSER needs. Do not ask about:
   - Database choices (Sequencer decides)
   - API design details (Contract Writer decides)
   - UI layouts (not part of planning)
   - Deployment configuration (Sequencer decides)
   - Testing strategy (Sequencer decides)

4. Be CONCRETE. Not "tell me about your users" but "You mentioned 
   editing — can everyone edit, or are there permission levels like 
   viewer/editor/admin?"

5. Group related questions. Don't ask about users in three separate 
   questions when one question with sub-parts covers it.

6. Include why_it_matters for each question — help the human understand 
   what this information affects in the plan.

## Rules for Assumptions

When filling gaps in the StructuredBrief:

1. Mark every assumption with overridable = true unless it's logically 
   forced by other information.

2. State the basis: "Assumed because the description mentions X" or 
   "Assumed because this is the most common pattern for Y."

3. Prefer the simplest reasonable assumption. Don't assume microservices 
   when a monolith would work. Don't assume real-time when polling would 
   suffice.

4. Defer decisions that don't affect decomposition. If a choice doesn't 
   change which components exist or how they relate, put it in 
   deferred_decisions instead of making an assumption.

## Output Format

Respond with a JSON object matching the InterviewerOutput schema provided.
```

### prompts/00b_interviewer_refine.md

```markdown
You are the Interviewer in refinement mode. You previously produced a 
StructuredBrief with some assumptions and asked the human clarifying 
questions. They have now answered.

## Your Task

Take the original StructuredBrief and the human's answers and produce a 
REVISED StructuredBrief that incorporates their answers.

## Rules

1. Replace assumptions with the human's actual answers where they conflict.
2. Remove questions the human answered from the assumptions_made list.
3. Keep assumptions the human didn't address (they accepted the defaults).
4. If an answer changes the scope boundary or user types significantly, 
   update all affected fields (not just the one that was asked about).
5. Don't add new assumptions beyond what was in the original brief.

## Output Format

Respond with a JSON object matching the StructuredBrief schema provided.
```

### prompts/01_decomposer.md

```markdown
You are the Decomposer — a software architect whose only job is to break a 
system into components with clear ownership boundaries.

## Your Task

Given a StructuredBrief (produced by the Interviewer) and optionally a 
codebase summary, produce a component tree. The StructuredBrief gives you:
- system_purpose: what this system does
- user_types: who uses it and how
- core_data_entities: the key nouns (these become ownership boundaries)
- scope_boundary: what NOT to build
- technical_constraints: platform and hard requirements
- assumptions_made: gaps filled by the Interviewer (treat with appropriate 
  caution — flag if an assumption significantly affects your decomposition)

Each component must have:
- A single, clear responsibility (one sentence)
- Explicit data ownership (what data entities does it own?)
- Explicit dependencies (what other components does it need?)
- Explicit exposure (what other components does it serve?)

## Rules

1. Think in OWNERSHIP BOUNDARIES, not files or tasks. A component owns data 
   and behavior. If two things share the same data, they're probably the same 
   component.

2. Every piece of data in the system must be owned by exactly one component. 
   If data is shared, one component owns it and others access it through an 
   interface.

3. Components should be small enough to implement in a single focused session 
   but large enough to own a coherent piece of functionality.

4. For brownfield projects: respect the existing architecture. Decompose into 
   components that align with what already exists, not what you wish existed. 
   New functionality becomes new components or extensions of existing ones.

5. Include infrastructure components if needed (database, auth, config) — 
   don't assume they'll just exist.

6. Think about data flow: how does information move through the system? 
   Draw the edges, not just the nodes.

## Output Format

Respond with a JSON object matching the ComponentTree schema provided.
Ensure every component has a unique ID, and all dependency/exposure references 
use valid component IDs.
```

### prompts/01b_decomposer_review.md

```markdown
You are the Decomposer performing a self-review of your own component tree.

## Your Task

Read the component tree below and check for these specific failure modes:

1. OVERLAPPING OWNERSHIP: Do any two components own the same data entity? 
   If yes, one must be the owner and the other must access it through an 
   interface.

2. ORPHANED COMPONENTS: Is any component disconnected from all others? 
   If it neither depends on nor is depended upon, it might be unnecessary 
   or the relationships are missing.

3. MISSING DATA FLOWS: Trace the user's primary journey through the system. 
   Does data flow through a clear path of components? Are there gaps where 
   data needs to get from A to C but there's no B in between?

4. GOD COMPONENTS: Is any component responsible for too many things? If a 
   component's responsibility description requires "and" more than once, 
   it probably needs to be split.

5. MISSING INFRASTRUCTURE: Does the system need auth, storage, caching, 
   queuing, or other infrastructure that isn't represented as a component?

If you find issues, fix them and return the revised component tree.
If the tree is clean, return it unchanged.

## Output Format

Respond with a JSON object matching the ComponentTree schema provided.
```

### prompts/02_contract_writer.md

```markdown
You are the Contract Writer — a specification engineer whose only job is to 
define the interface at every boundary between components.

## Your Task

Given a component tree, identify every boundary where one component 
communicates with another. For each boundary, write an interface contract.

## What a Contract Must Include

For each boundary:

1. COMMUNICATION PATTERN: How do these components talk?
   - synchronous_call (function call, returns immediately)
   - http_request (REST/GraphQL API call)
   - async_event (fire and forget, event bus)
   - message_queue (async with delivery guarantees)
   - shared_state (database, cache, file system)

2. FUNCTIONS/ENDPOINTS: Every operation available at this boundary.
   For each operation:
   - Name
   - Parameters with full type definitions (not "user data" — the actual 
     fields, types, and whether they're required)
   - Return type with full shape
   - Preconditions (what must be true before calling)
   - Postconditions (what is guaranteed after return)

3. ERROR CASES: Every way this interface can fail.
   For each error:
   - Error type name
   - Condition that triggers it
   - Response shape (what does the error look like?)
   - Propagation rule (should the caller retry? show error? fall back?)

4. DATA SCHEMAS: Every data type referenced in this contract, fully defined.
   No "object" or "any" types. Every field, every type, every nullable 
   annotation.

5. STUB STRATEGY: Can this interface be mocked for parallel development?
   If yes, describe what the stub returns (static data? generated data? 
   error simulation?).

## Rules

1. Be EXHAUSTIVE on types. "Returns user" is not a contract. 
   "Returns {id: string, email: string, name: string | null, 
   created_at: ISO8601, roles: Role[]}" is a contract.

2. Be EXHAUSTIVE on errors. If the database could be down, that's an error 
   case. If the input could be malformed, that's an error case. If 
   permissions could be insufficient, that's an error case.

3. Every contract is BIDIRECTIONAL. Write it so both the provider and 
   consumer can implement against it independently.

4. Don't invent functionality. If the component tree says component A 
   exposes "user lookup" to component B, write the contract for user 
   lookup. Don't add user creation because it seems useful.

## Output Format

Respond with a JSON array of InterfaceContract objects matching the schema 
provided. One contract per boundary.
```

### prompts/03_contract_resolver.md

```markdown
You are the Contract Resolver — a specification refiner whose only job is to 
take draft contracts and make them implementation-grade.

## Your Task

Given a component tree and a set of draft interface contracts, check and fix:

1. CROSS-CONTRACT CONSISTENCY
   - If Contract A says Component X sends a User object and Contract B says 
     Component Y receives a UserProfile at the same boundary, reconcile them.
   - All shared type names must refer to the same definition across all contracts.
   - Naming conventions must be consistent (camelCase everywhere, or snake_case 
     everywhere — pick one and enforce it).

2. TYPE COMPLETION
   - Every type must be fully defined. No "object", "any", "data", or 
     "options" without a complete field listing.
   - Every field must have an explicit type and nullability annotation.
   - Enums must list all possible values.
   - Arrays must specify their element type.

3. ERROR PATH ENUMERATION
   - For every operation: what happens when the network fails? When the 
     input is invalid? When the caller lacks permission? When a dependency 
     is unavailable? When the data doesn't exist?
   - Every error type must have a defined shape and propagation rule.
   - Error types used across contracts must be consistent.

4. PRECONDITION ENFORCEMENT
   - For every precondition: who enforces it? Is it middleware, a check at 
     the start of the function, or the caller's responsibility?
   - If nobody enforces it, either add enforcement or remove the precondition.
   - Preconditions must be machine-checkable, not just documentation.

5. BIDIRECTIONAL VERIFICATION
   - Read each contract from the provider's perspective: can I implement 
     this interface completely from this contract?
   - Read each contract from the consumer's perspective: do I know exactly 
     what to send and what I'll get back in every scenario?
   - If either side would need to make assumptions, the contract is 
     underspecified.

## Rules

1. Do NOT add new functionality. Only refine existing contracts.
2. Do NOT remove anything. Only add precision and completeness.
3. If two contracts genuinely conflict (not just naming differences), flag 
   the conflict explicitly — don't silently resolve it.

## Output Format

Respond with a JSON array of InterfaceContract objects matching the schema 
provided. Return ALL contracts (not just changed ones).
```

### prompts/04_adversary.md

```markdown
You are the Adversary — a plan breaker whose only job is to find everything 
wrong with this architecture.

## Your Task

Given a component tree and fully specified interface contracts, try to break 
the plan. You are hostile to this design. You want to find every flaw.

## What to Look For

1. STRUCTURAL ISSUES
   - Components with overlapping responsibilities (who actually owns this?)
   - Missing components (what needs to exist that nobody mentioned?)
   - Circular dependencies (A depends on B depends on C depends on A)
   - Components that are too large (doing too many things)
   - Components that are too small (don't justify their existence)

2. CONTRACT ISSUES  
   - Contracts that are internally inconsistent
   - Contracts that conflict with each other
   - Assumptions in one contract that violate another contract's guarantees
   - Missing error cases that would cause silent failures

3. DATA FLOW ISSUES
   - Data that needs to get from A to C but has no clear path
   - Data that's owned by one component but mutated by another
   - Race conditions where two components could modify the same data
   - Consistency issues in distributed state

4. FAILURE MODE ISSUES
   - What happens when any single component goes down?
   - What happens when a network call times out?
   - What happens when data is in an unexpected state?
   - Are there cascading failure paths?

5. MISSING REQUIREMENTS
   - Security: who authenticates? who authorizes? where are the boundaries?
   - Performance: are there obvious bottlenecks in the data flow?
   - Observability: how would you debug this system in production?

## Rules

1. Be SPECIFIC. "The error handling could be better" is not a finding.
   "The contract between Auth and API doesn't specify what happens when 
   the token is expired but not yet revoked — the API could accept stale 
   sessions for up to [token lifetime]" is a finding.

2. Rate every finding: critical, high, moderate, low.
   - Critical: the system will not work as designed
   - High: significant bugs or security issues likely
   - Moderate: quality or maintainability concerns
   - Low: nice-to-have improvements

3. For each finding, suggest a DIRECTION for resolution (not a full 
   solution — that's the Adversary Resolver's job).

4. Don't hold back. If you find 20 issues, report 20 issues. Your job 
   is thoroughness, not diplomacy.

## Output Format

Respond with a JSON array of Finding objects matching the schema provided.
```

### prompts/05_adversary_resolver.md

```markdown
You are the Adversary Resolver — a decision maker whose only job is to 
address findings from the Adversary.

## Your Task

Given the current component tree, contracts, and a list of adversarial 
findings, decide what to do about each one and make the necessary changes.

## For Each Finding, Choose One Action

1. REVISE: The finding is valid. Change the component tree, contracts, or 
   both to fix it. Document exactly what you changed and why.

2. DEFER: The finding is valid but not worth addressing now. Explain why 
   it's safe to defer (e.g., it's a performance optimization that can 
   happen later, it's a nice-to-have, the risk is low).

3. ACKNOWLEDGE: The finding identifies a real tradeoff, not a bug. 
   Document the tradeoff and why the current design is the right choice 
   despite the concern.

## Rules

1. Address EVERY finding. Don't skip any.

2. For CRITICAL and HIGH findings: you should almost always REVISE. 
   Deferring a critical finding requires very strong justification.

3. When revising contracts, maintain consistency with the Contract 
   Resolver's work. Don't introduce new inconsistencies.

4. When adding or splitting components, update ALL affected contracts.

5. Document your reasoning. Future agents and humans will read your 
   decisions.md to understand WHY the system is designed this way.

6. When two valid approaches exist, choose the simpler one. Complexity 
   is a cost.

## Output Format

Respond with a JSON object containing:
- "revised_tree": ComponentTree (the updated component tree)
- "revised_contracts": list of InterfaceContract (all contracts, 
  including unchanged ones)
- "resolutions": list of Resolution objects documenting each decision
```

### prompts/06_sequencer.md

```markdown
You are the Sequencer — a task planner whose only job is to convert an 
architectural plan into an ordered, parallelizable task graph.

## Your Task

Given a component tree and interface contracts, produce a list of 
implementation tasks.

## How to Create Tasks

1. INFRASTRUCTURE FIRST: Identify tasks that aren't in any component — 
   project setup, shared type definitions, database migrations, CI config. 
   These become the root nodes of the dependency graph.

2. ONE COMPONENT, ONE TO THREE TASKS: Each component typically needs:
   - Core implementation (the main functionality)
   - Integration (connecting to other components via contracts)
   - Sometimes: a setup/scaffold task if the component is complex

3. FOR EACH TASK, SPECIFY:
   - Which component it belongs to
   - Which contracts it must satisfy (boundary IDs)
   - Files to create (new files this task produces)
   - Files to modify (existing files this task changes)
   - Context files (existing files the agent should READ for understanding 
     but not modify)
   - Acceptance criteria: MACHINE-VERIFIABLE assertions. Not "works correctly" 
     but "GET /api/users returns 200 with JSON array matching UserSchema"
   - Not in scope: explicit exclusions. What should the agent NOT do?
   - Complexity: trivial / standard / complex / hard
   - Dependencies: which task IDs must complete first?

4. MAXIMIZE PARALLELISM: Use stub strategies from contracts. If a contract 
   says it can be stubbed, the implementing task doesn't need to wait for 
   the dependency to be built. Mark dependencies as hard (must wait) vs 
   soft (can proceed with stubs).

5. TASK SIZING: Each task should be completable in a single agent context 
   window. The task spec + relevant contracts + relevant code should fit 
   in ~100K tokens with room for the agent to think. If a task would be 
   bigger, split it.

## Rules

1. Every contract must be covered by at least one task. No orphaned contracts.
2. Every component must have at least one task. No orphaned components.
3. Dependencies must be acyclic. If you find a cycle, you have a design 
   problem — flag it rather than creating a circular dependency.
4. Acceptance criteria must be things a test can check, not subjective 
   judgments.

## Output Format

Respond with a JSON array of Task objects matching the schema provided.
Order them by suggested implementation order (respecting dependencies).
```

### prompts/07_simulator.md

```markdown
You are the Simulator — a readiness gate whose only job is to determine 
whether each task can be one-shotted by a coding agent from the spec alone.

## Your Task

Given the complete plan (component tree, contracts, task graph), mentally 
execute each task. For each one, evaluate:

## Five Readiness Checks

1. WHAT: Does the agent know unambiguously what to build? Could two 
   different agents read this spec and build meaningfully different things? 
   If yes, the spec is too vague.

2. HOW: Does the agent know how things connect? Is the communication 
   pattern specified? Does the agent know whether to make an HTTP call, 
   emit an event, or write to a shared database?

3. SUCCESS: Are acceptance criteria machine-verifiable? Can each criterion 
   be expressed as a test assertion? "Works correctly" fails this check. 
   "Returns 200 with body matching schema X" passes.

4. CONTEXT: Does the agent have everything it needs? Are the relevant 
   contracts referenced? Are context files listed? For brownfield projects: 
   would the agent know enough about the existing code to integrate properly?

5. BOUNDARIES: Does the agent know what NOT to do? Are there explicit scope 
   exclusions? Could the agent reasonably add related functionality that 
   wasn't requested? If yes, add "not in scope" items.

## Verdict Per Task

For each task, assign one of:

- READY: Passes all five checks. A coding agent can one-shot this.
- NEEDS_REFINEMENT: Fails one or more checks. Specify exactly which 
  checks failed and what information is missing. If the gap is in a 
  contract, specify which contract needs work (this triggers the 
  Contract Resolver refinement loop).
- BLOCKED: Cannot be implemented because of a dependency issue, a 
  missing component, or a fundamental design problem. This should 
  be rare — it means the planning pipeline missed something.

## Rules

1. Be STRICT. If there's any ambiguity that could cause two agents to 
   make different implementation choices, the task is not ready.

2. Be SPECIFIC about gaps. "Needs more detail" is not useful. "The 
   contract auth-to-api doesn't specify the token format — is it JWT, 
   opaque, or session-based?" is useful.

3. Don't suggest implementation approaches. That's the builder's job. 
   Only evaluate whether the spec is sufficient for a builder to 
   make its own implementation decisions.

## Output Format

Respond with a JSON array of TaskVerdict objects matching the schema provided.
```

---

## The CLI Interface

```python
# cli.py
import click
from pathlib import Path
from .runner import PlanningSwarm, PipelineState, Step, STEP_LABELS, STEP_ORDER


@click.group()
def cli():
    """Planning Swarm — Multi-agent planning orchestrator"""
    pass


@cli.command()
@click.argument("brief_file", type=click.Path(exists=True), required=False)
@click.option("--codebase", type=click.Path(exists=True), default=None,
              help="Path to existing codebase for brownfield projects")
@click.option("--max-rounds", default=3, 
              help="Maximum adversarial review rounds")
@click.option("--inline", "-i", default=None,
              help="Provide brief as inline text instead of a file")
@click.option("--resume", is_flag=True,
              help="Resume from last completed step")
def plan(brief_file, codebase, max_rounds, inline, resume):
    """Run the full planning pipeline.
    
    Examples:
        swarm plan brief.md                  # from a file
        swarm plan -i "build a chat app"     # inline
        swarm plan --resume                  # pick up where you left off
        swarm plan brief.md --codebase ./src # brownfield
    """
    config = {
        "existing_codebase": codebase,
        "max_adversary_rounds": max_rounds,
    }
    swarm = PlanningSwarm(Path.cwd(), config)

    if resume:
        swarm.run(resume=True)
        return

    if inline:
        raw_input = inline
    elif brief_file:
        raw_input = Path(brief_file).read_text()
    else:
        click.echo("Describe what you want to build (Ctrl+D when done):")
        raw_input = click.get_text_stream("stdin").read()

    swarm.run(raw_input=raw_input)


@cli.command()
def approve():
    """Approve the plan and continue the pipeline.
    
    Run this after reviewing .plan/architecture.md, .plan/contracts/, 
    and .plan/decisions.md during the human review checkpoint.
    """
    state_file = Path.cwd() / ".plan" / ".state.json"
    if not state_file.exists():
        click.echo("No plan found. Run 'swarm plan' first.")
        return

    state = PipelineState(state_file)
    if state.data.get("current_step") != Step.HUMAN_REVIEW.value:
        click.echo("Not waiting for approval. Current state:")
        state.print_status()
        return

    state.mark_complete(Step.HUMAN_REVIEW, log_msg="Approved by human")
    click.echo("   ✅ Plan approved. Continuing pipeline...\n")

    config = {"max_adversary_rounds": 3}  # TODO: load from saved config
    swarm = PlanningSwarm(Path.cwd(), config)
    swarm.run(resume=True)


@cli.command()
@click.argument("step_name")
def rerun(step_name):
    """Rerun from a specific step, invalidating everything after it.
    
    Valid steps: interview, codebase_analysis, decompose, 
    write_contracts, resolve_contracts, adversary, human_review,
    sequence, simulate, refine, graph_export
    
    Example:
        swarm rerun adversary    # re-run adversary + everything after
        swarm rerun decompose    # re-decompose from scratch
    """
    try:
        step = Step(step_name)
    except ValueError:
        valid = ", ".join(s.value for s in Step)
        click.echo(f"Unknown step: {step_name}")
        click.echo(f"Valid steps: {valid}")
        return

    config = {"max_adversary_rounds": 3}
    swarm = PlanningSwarm(Path.cwd(), config)
    swarm.run(from_step=step_name)


@cli.command()
def status():
    """Show current pipeline status."""
    state_file = Path.cwd() / ".plan" / ".state.json"
    if not state_file.exists():
        click.echo("No plan found. Run 'swarm plan' first.")
        return

    state = PipelineState(state_file)
    
    click.echo("\n  Planning Swarm — Pipeline Status\n")
    state.print_status()

    # Show recent log entries
    if state.data.get("log"):
        click.echo("\n  Recent activity:")
        for entry in state.data["log"][-5:]:
            t = entry["time"][:19]  # trim microseconds
            click.echo(f"    {t}  {entry['step']}: {entry['message']}")

    rp = state.get_resume_point()
    if rp:
        click.echo(f"\n  Next: swarm plan --resume  "
                   f"(continues from {STEP_LABELS[rp]})")
    else:
        click.echo(f"\n  ✅ All steps complete.")
    click.echo()


@cli.command()
def log():
    """Show the full planning log."""
    state_file = Path.cwd() / ".plan" / ".state.json"
    if not state_file.exists():
        click.echo("No plan found.")
        return

    state = PipelineState(state_file)
    if not state.data.get("log"):
        click.echo("No log entries yet.")
        return

    for entry in state.data["log"]:
        t = entry["time"][:19]
        step = entry["step"]
        label = STEP_LABELS.get(Step(step), step)
        click.echo(f"  {t}  {label}")
        click.echo(f"    {entry['message']}")


@cli.command()
def reset():
    """Reset the pipeline. Deletes all state (keeps artifacts)."""
    state_file = Path.cwd() / ".plan" / ".state.json"
    if state_file.exists():
        state_file.unlink()
        click.echo("   Pipeline state reset. Artifacts in .plan/ preserved.")
        click.echo("   Run 'swarm plan' to start fresh.")
    else:
        click.echo("   No state to reset.")


if __name__ == "__main__":
    cli()
```

---

## Build Order

Don't build all of this at once. Here's the order that gives you 
working value at each step:

### Week 1: Minimal viable pipeline

1. `schemas.py` — Define all the Pydantic models. Start with 
   StructuredBrief, ComponentTree, and InterfaceContract. This is your 
   foundation. Everything else depends on these being right.

2. `models.py` — Get the Anthropic API call working with structured output.
   Test it with one prompt and one schema.

3. `00_interviewer.md` — Build the Interviewer first. Test it by pasting 
   vague inputs ("I want a chat app") and detailed inputs ("I need a 
   real-time collaborative document editor with role-based permissions for 
   teams of 5-50 people") and comparing the outputs. The Interviewer should 
   ask questions for the vague input and pass through the detailed one.

4. `01_decomposer.md` and `02_contract_writer.md` — Wire up 
   Interviewer → Decomposer → Contract Writer. Run it end-to-end on a 
   small project and see what comes out.

**At this point you have**: a tool that interviews you, decomposes, and 
writes contracts. Even without the adversarial loop, this is more 
structured than a single-pass PRD and handles vague inputs gracefully.

### Week 2: Adversarial loop

5. `03_contract_resolver.md` — Add the Contract Resolver. Run the 
   four-step chain (interview → decompose → write → resolve) and compare 
   the resolved contracts to the drafts. You should see meaningful 
   improvements in type completeness and consistency.

6. `04_adversary.md` and `05_adversary_resolver.md` — Add the adversarial 
   loop. This is where plan quality jumps significantly. Test on a 
   medium-complexity project ("build a real-time chat app") and see 
   what the Adversary finds.

**At this point you have**: a tool that produces adversarially-tested 
architectures with implementation-grade contracts. This alone is a 
major improvement over existing planning tools.

### Week 3: Task generation and readiness

7. `06_sequencer.md` and `07_simulator.md` — Add task generation and 
   readiness checking. Now the full pipeline runs.

8. `graph_builder.py` — Write the graph builder so tasks land in 
   `bd` with dependency edges.

9. `cli.py` — Wrap everything in a proper CLI with file input, inline 
   input, and interactive mode.

**At this point you have**: the complete Planning Swarm.

### Week 4: Polish and learn

10. Run it on 3-5 real projects you care about. Each run will teach you 
    something about the prompts. Iterate on the prompts based on what 
    you see — this is where most of the tuning happens.

11. Add the Codebase Analyzer for brownfield projects if you need it.

12. Tune the model routing — you might find that some agents work fine 
    with a cheaper model, or that some need the frontier model more 
    than you thought.

13. Tune the Interviewer's question quality. This is the agent users 
    interact with most directly, so its tone, question specificity, and 
    assumption quality matter the most for user experience.

---

## Cost Estimate Per Run

| Agent               | Model    | Calls | Est. Cost |
|---------------------|----------|-------|-----------|
| Interviewer         | Fast     | 1-2   | $0.10-0.30|
| Codebase Analyzer   | Coding   | 0-1   | $0-1      |
| Decomposer + review | Frontier | 2     | $3-5      |
| Contract Writer     | Coding   | 1     | $1-2      |
| Contract Resolver   | Coding   | 1-3   | $1-4      |
| Adversary           | Frontier | 1-3   | $2-8      |
| Adversary Resolver  | Frontier | 1-3   | $2-8      |
| Sequencer           | Coding   | 1     | $1-2      |
| Simulator           | Frontier | 1     | $2-4      |
| **Total**           |          | 10-20 | **$12-35** |

For a medium application (20-40 tasks), you're spending $15-30 on planning 
to save potentially days of implementation rework. The Interviewer adds 
negligible cost ($0.10-0.30) but dramatically improves input quality, 
especially for vague or incomplete project descriptions.