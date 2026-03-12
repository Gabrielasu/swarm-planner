"""Core orchestration engine for the Planning Swarm."""

import json
import os
import sys
import threading
import time
from datetime import datetime
from enum import Enum as PyEnum
from pathlib import Path
from typing import Optional

from .artifacts import save_artifact, load_artifact
from .beads_bridge import write_to_beads
from .context import assemble_context
from .models import call_model, ModelTier
from .schemas import (
    Assumption,
    ComponentTree,
    Finding,
    InterfaceContract,
    InterviewerOutput,
    Readiness,
    Resolution,
    Severity,
    StructuredBrief,
    Task,
    TaskVerdict,
)


# -- Pipeline Steps -----------------------------------------------------------


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
    BEADS = "beads_export"


STEP_ORDER = list(Step)

STEP_LABELS = {
    Step.INTERVIEW: "Interviewer",
    Step.CODEBASE: "Codebase Analysis",
    Step.DECOMPOSE: "Decomposer",
    Step.WRITE_CONTRACTS: "Contract Writer",
    Step.RESOLVE_CONTRACTS: "Contract Resolver",
    Step.ADVERSARY: "Adversary Loop",
    Step.HUMAN_REVIEW: "Human Review",
    Step.SEQUENCE: "Sequencer",
    Step.SIMULATE: "Simulator",
    Step.REFINE: "Refinement",
    Step.BEADS: "Beads Export",
}


# -- Progress Spinner ---------------------------------------------------------


class ProgressSpinner:
    """Non-blocking spinner with elapsed time. Shows you it's not stuck."""

    FRAMES = ["*", "*", "*", "*", "*", "*", "*", "*", "*", "*"]

    def __init__(self, agent_name: str, detail: str = ""):
        self.agent_name = agent_name
        self.detail = detail
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._start_time: Optional[float] = None

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
        sys.stderr.write("\r\033[K")  # clear spinner line
        sys.stderr.flush()
        if summary:
            print(f"   > {summary} ({elapsed:.1f}s)")

    def _spin(self):
        i = 0
        frames = ["-", "\\", "|", "/"]
        while self._running:
            elapsed = time.time() - self._start_time
            frame = frames[i % len(frames)]
            detail = f" - {self.detail}" if self.detail else ""
            sys.stderr.write(
                f"\r   {frame} {self.agent_name}{detail} [{elapsed:.0f}s]"
            )
            sys.stderr.flush()
            time.sleep(0.1)
            i += 1


# -- Pipeline State -----------------------------------------------------------


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
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.data, indent=2))

    def is_complete(self, step: Step) -> bool:
        return step.value in self.data["completed_steps"]

    def mark_complete(
        self, step: Step, output_data: dict = None, log_msg: str = ""
    ):
        if step.value not in self.data["completed_steps"]:
            self.data["completed_steps"].append(step.value)
        if output_data:
            self.data["outputs"][step.value] = output_data
        self.data["current_step"] = None
        if log_msg:
            self.data["log"].append(
                {
                    "time": datetime.now().isoformat(),
                    "step": step.value,
                    "message": log_msg,
                }
            )
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
                icon = "[done]"
            elif self.data["current_step"] == step.value:
                icon = "[....]"
            else:
                icon = "[    ]"
            print(f"  {icon} {STEP_LABELS[step]}")


# -- The Swarm ----------------------------------------------------------------


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

    # -- Rendering helpers ----------------------------------------------------

    def _render_brief(self, brief: StructuredBrief) -> str:
        """Render a StructuredBrief as readable markdown."""
        lines = ["# Structured Brief\n"]
        lines.append(f"## System Purpose\n\n{brief.system_purpose}\n")

        lines.append("## User Types\n")
        for u in brief.user_types:
            lines.append(f"### {u.type}\n")
            lines.append(f"- **Access Level:** {u.access_level}")
            lines.append("- **Primary Actions:**")
            for a in u.primary_actions:
                lines.append(f"  - {a}")
            lines.append("")

        lines.append("## Core Data Entities\n")
        for e in brief.core_data_entities:
            lines.append(f"### {e.name}\n")
            lines.append(f"{e.description}\n")
            if e.relationships:
                lines.append("**Relationships:**")
                for r in e.relationships:
                    lines.append(f"- {r}")
            lines.append("")

        lines.append("## Scope Boundary (NOT building)\n")
        for s in brief.scope_boundary:
            lines.append(f"- {s}")
        lines.append("")

        lines.append("## Technical Constraints\n")
        for k, v in brief.technical_constraints.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

        if brief.assumptions_made:
            lines.append("## Assumptions Made\n")
            for a in brief.assumptions_made:
                override = " (overridable)" if a.overridable else ""
                lines.append(f"- {a.assumption}{override}")
                lines.append(f"  - Basis: {a.basis}")
            lines.append("")

        if brief.deferred_decisions:
            lines.append("## Deferred Decisions\n")
            for d in brief.deferred_decisions:
                lines.append(f"- {d}")
            lines.append("")

        return "\n".join(lines)

    def _render_architecture(self, tree: ComponentTree) -> str:
        """Render a ComponentTree as readable markdown."""
        lines = ["# Architecture\n"]
        lines.append(f"## Rationale\n\n{tree.rationale}\n")

        lines.append("## Components\n")
        for c in tree.components:
            lines.append(f"### {c.name} (`{c.id}`)\n")
            lines.append(f"**Responsibility:** {c.responsibility}\n")
            if c.owns_data:
                lines.append(f"**Owns data:** {', '.join(c.owns_data)}")
            if c.depends_on:
                lines.append(f"**Depends on:** {', '.join(c.depends_on)}")
            if c.exposes_to:
                lines.append(f"**Exposes to:** {', '.join(c.exposes_to)}")
            lines.append("")

        if tree.data_flows:
            lines.append("## Data Flows\n")
            for flow in tree.data_flows:
                src = flow.get("from", "?")
                dst = flow.get("to", "?")
                desc = flow.get("data_description", "")
                lines.append(f"- **{src}** -> **{dst}**: {desc}")
            lines.append("")

        return "\n".join(lines)

    def _render_contract(self, contract: InterfaceContract) -> str:
        """Render an InterfaceContract as readable markdown."""
        lines = [f"# Contract: {contract.boundary_id}\n"]
        lines.append(
            f"**{contract.from_component}** -> **{contract.to_component}**"
        )
        lines.append(
            f"**Pattern:** {contract.communication_pattern.value}\n"
        )

        lines.append("## Functions\n")
        for fn in contract.functions:
            name = fn.get("name", "unnamed")
            lines.append(f"### `{name}`\n")
            if "params" in fn:
                lines.append("**Parameters:**")
                lines.append(f"```json\n{json.dumps(fn['params'], indent=2)}\n```\n")
            if "returns" in fn:
                lines.append("**Returns:**")
                lines.append(f"```json\n{json.dumps(fn['returns'], indent=2)}\n```\n")
            if "preconditions" in fn:
                lines.append(f"**Preconditions:** {fn['preconditions']}")
            if "postconditions" in fn:
                lines.append(f"**Postconditions:** {fn['postconditions']}")
            lines.append("")

        if contract.error_cases:
            lines.append("## Error Cases\n")
            for ec in contract.error_cases:
                lines.append(f"### {ec.error_type}\n")
                lines.append(f"- **Condition:** {ec.condition}")
                lines.append(f"- **Propagation:** {ec.propagation}")
                lines.append(
                    f"- **Shape:** `{json.dumps(ec.response_shape)}`"
                )
                lines.append("")

        if contract.data_schemas:
            lines.append("## Data Schemas\n")
            lines.append(
                f"```json\n{json.dumps(contract.data_schemas, indent=2)}\n```\n"
            )

        lines.append("## Stub Strategy\n")
        can = "Yes" if contract.stub_strategy.can_stub else "No"
        lines.append(f"- **Can stub:** {can}")
        lines.append(f"- **Description:** {contract.stub_strategy.stub_description}")
        lines.append("")

        return "\n".join(lines)

    def _render_decisions(self, resolutions: list[Resolution]) -> str:
        """Render Resolution list as decisions.md."""
        lines = ["# Architecture Decisions\n"]
        for r in resolutions:
            action_label = r.action.upper()
            lines.append(f"## Finding #{r.finding_index} [{action_label}]\n")
            lines.append(f"**Changes:** {r.changes_made}\n")
            lines.append(f"**Rationale:** {r.rationale}\n")
        return "\n".join(lines)

    def _render_task(self, task: Task) -> str:
        """Render a Task as readable markdown."""
        lines = [f"# Task {task.id}: {task.title}\n"]
        lines.append(f"**Component:** {task.component}")
        lines.append(f"**Complexity:** {task.complexity.value}")
        if task.estimated_tokens:
            lines.append(f"**Estimated tokens:** {task.estimated_tokens}")
        lines.append(f"\n## Description\n\n{task.description}\n")

        if task.contracts_to_satisfy:
            lines.append("## Contracts to Satisfy\n")
            for c in task.contracts_to_satisfy:
                lines.append(f"- {c}")
            lines.append("")

        if task.files_to_create:
            lines.append("## Files to Create\n")
            for f in task.files_to_create:
                lines.append(f"- `{f}`")
            lines.append("")

        if task.files_to_modify:
            lines.append("## Files to Modify\n")
            for f in task.files_to_modify:
                lines.append(f"- `{f}`")
            lines.append("")

        if task.context_files:
            lines.append("## Context Files\n")
            for f in task.context_files:
                lines.append(f"- `{f}`")
            lines.append("")

        lines.append("## Acceptance Criteria\n")
        for ac in task.acceptance_criteria:
            lines.append(f"- [ ] {ac}")
        lines.append("")

        if task.not_in_scope:
            lines.append("## Not In Scope\n")
            for n in task.not_in_scope:
                lines.append(f"- {n}")
            lines.append("")

        if task.depends_on:
            lines.append("## Dependencies\n")
            for d in task.depends_on:
                lines.append(f"- Task {d}")
            lines.append("")

        return "\n".join(lines)

    def _render_simulation(self, verdicts: list[TaskVerdict]) -> str:
        """Render simulation report."""
        lines = ["# Simulation Report\n"]

        ready = [v for v in verdicts if v.readiness == Readiness.READY]
        needs_work = [
            v for v in verdicts if v.readiness == Readiness.NEEDS_REFINEMENT
        ]
        blocked = [v for v in verdicts if v.readiness == Readiness.BLOCKED]

        lines.append(f"- **Ready:** {len(ready)}")
        lines.append(f"- **Needs refinement:** {len(needs_work)}")
        lines.append(f"- **Blocked:** {len(blocked)}")
        lines.append("")

        for v in verdicts:
            status = v.readiness.value.upper()
            lines.append(f"## Task {v.task_id} [{status}]\n")
            if v.gaps:
                lines.append("**Gaps:**")
                for g in v.gaps:
                    lines.append(f"- {g}")
            if v.refinement_target:
                lines.append(f"\n**Refinement target:** {v.refinement_target}")
            lines.append("")

        return "\n".join(lines)

    # -- Codebase scanning helpers -------------------------------------------

    def _scan_directory_tree(self, codebase_path: str) -> str:
        """Scan directory tree for structural understanding."""
        root = Path(codebase_path)
        lines = []
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".nuxt", "coverage", ".tox",
            ".eggs", "*.egg-info",
        }

        for path in sorted(root.rglob("*")):
            # Skip hidden and common ignore dirs
            parts = path.relative_to(root).parts
            if any(p in skip_dirs or p.startswith(".") for p in parts):
                continue
            rel = path.relative_to(root)
            if path.is_dir():
                lines.append(f"{rel}/")
            else:
                lines.append(str(rel))

        # Limit to first 500 entries
        if len(lines) > 500:
            lines = lines[:500]
            lines.append(f"... ({len(lines)} more files)")

        return "\n".join(lines)

    def _read_key_files(self, codebase_path: str) -> str:
        """Read key structural files from a codebase."""
        root = Path(codebase_path)
        key_patterns = [
            "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
            "tsconfig.json", "requirements.txt", "setup.py", "setup.cfg",
            "Makefile", "docker-compose.yml", "Dockerfile",
        ]
        parts = []
        for pattern in key_patterns:
            for match in root.glob(pattern):
                try:
                    content = match.read_text(encoding="utf-8")
                    rel = match.relative_to(root)
                    parts.append(f"--- {rel} ---\n{content}")
                except Exception:
                    pass

        # Also look for schema/model files
        for pattern in ["**/schema*", "**/models*", "**/types*"]:
            for match in sorted(root.glob(pattern))[:5]:
                if match.is_file() and match.stat().st_size < 10000:
                    try:
                        content = match.read_text(encoding="utf-8")
                        rel = match.relative_to(root)
                        parts.append(f"--- {rel} ---\n{content}")
                    except Exception:
                        pass

        return "\n\n".join(parts) if parts else "No key files found."

    # -- Step 0a: Interview ---------------------------------------------------

    def interview(self, raw_input: str) -> StructuredBrief:
        """Analyze input, ask clarifying questions if needed,
        produce a normalized StructuredBrief."""

        # Phase 1: Analyze the raw input
        spinner = ProgressSpinner("Interviewer", "analyzing input")
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/00_interviewer.md",
                variables={"raw_input": raw_input},
            )
            result = call_model(
                context, tier=ModelTier.FAST, response_format=InterviewerOutput
            )
        finally:
            spinner.stop()

        if result.brief_sufficient:
            # Input was detailed enough - no questions needed
            print("   > Brief is sufficient. No clarification needed.")
            save_artifact(
                self.plan_dir / "brief.md",
                self._render_brief(result.structured_brief),
            )
            return result.structured_brief

        # Phase 2: Present questions to the human
        brief = result.structured_brief
        questions = result.questions

        print(f"\n   Found {len(questions)} areas that need clarification.\n")
        print("   For each question, type your answer or press Enter")
        print("   to accept the default assumption.\n")

        answers = {}
        for i, q in enumerate(questions):
            print(f"   [{i + 1}/{len(questions)}] {q.question}")
            print(f"   Default: {q.default_assumption}")
            answer = input("   > ").strip()
            if answer:
                answers[q.dimension] = answer
            else:
                print("   (using default)")
            print()

        # Phase 3: Rebuild the brief with answers incorporated
        if answers:
            refine_spinner = ProgressSpinner(
                "Interviewer", "incorporating answers"
            )
            refine_spinner.start()
            try:
                refine_context = assemble_context(
                    prompt_file="prompts/00b_interviewer_refine.md",
                    variables={
                        "original_brief": brief.model_dump_json(indent=2),
                        "questions_and_answers": json.dumps(
                            {
                                q.question: answers.get(
                                    q.dimension, q.default_assumption
                                )
                                for q in questions
                            },
                            indent=2,
                        ),
                    },
                )
                brief = call_model(
                    refine_context,
                    tier=ModelTier.FAST,
                    response_format=StructuredBrief,
                )
            finally:
                refine_spinner.stop()

        # Show assumptions for quick review
        if brief.assumptions_made:
            print("   Assumptions made (review quickly):")
            for a in brief.assumptions_made:
                print(f"   - {a.assumption}")
            print()
            override = input(
                "   Any corrections? (type or Enter to accept all): "
            ).strip()
            if override:
                # Quick re-run with the correction as additional context
                brief.assumptions_made = [
                    a
                    for a in brief.assumptions_made
                    if override.lower() not in a.assumption.lower()
                ]

        save_artifact(
            self.plan_dir / "brief.md", self._render_brief(brief)
        )
        return brief

    # -- Step 0b (optional): Codebase Analysis --------------------------------

    def analyze_codebase(self) -> str | None:
        """For brownfield projects. Scans existing code, produces summary."""
        if not self.config.get("existing_codebase"):
            return None

        codebase_path = self.config["existing_codebase"]
        tree = self._scan_directory_tree(codebase_path)
        key_files = self._read_key_files(codebase_path)

        spinner = ProgressSpinner("Codebase Analyzer", "scanning project")
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/00_codebase_analyzer.md",
                variables={"directory_tree": tree, "key_files": key_files},
            )
            result = call_model(context, tier=ModelTier.CODING)
        finally:
            spinner.stop("Analysis complete")

        save_artifact(self.plan_dir / "codebase_summary.md", result)
        return result

    # -- Step 1: Decompose ----------------------------------------------------

    def decompose(
        self, brief: StructuredBrief, codebase_summary: str = None
    ) -> ComponentTree:
        """Break the system into components."""
        spinner = ProgressSpinner("Decomposer", "breaking into components")
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/01_decomposer.md",
                variables={
                    "brief": brief.model_dump_json(indent=2),
                    "codebase_summary": codebase_summary
                    or "Greenfield project.",
                },
            )
            result = call_model(
                context, tier=ModelTier.FRONTIER, response_format=ComponentTree
            )
        except Exception:
            spinner.stop("failed")
            raise
        spinner.stop(f"{len(result.components)} components identified")

        # Self-review: Decomposer checks its own output
        review_spinner = ProgressSpinner("Decomposer", "self-review")
        review_spinner.start()
        try:
            review_context = assemble_context(
                prompt_file="prompts/01b_decomposer_review.md",
                variables={
                    "component_tree": result.model_dump_json(indent=2)
                },
            )
            reviewed = call_model(
                review_context,
                tier=ModelTier.FRONTIER,
                response_format=ComponentTree,
            )
        finally:
            review_spinner.stop("Self-review complete")

        save_artifact(
            self.plan_dir / "architecture.md",
            self._render_architecture(reviewed),
        )
        return reviewed

    # -- Step 2: Write Contracts ----------------------------------------------

    def write_contracts(
        self, tree: ComponentTree
    ) -> list[InterfaceContract]:
        """Create draft interface contracts for every boundary."""
        spinner = ProgressSpinner("Contract Writer", "drafting contracts")
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/02_contract_writer.md",
                variables={
                    "component_tree": tree.model_dump_json(indent=2)
                },
            )
            result = call_model(
                context,
                tier=ModelTier.CODING,
                response_format=list[InterfaceContract],
            )
        except Exception:
            spinner.stop("failed")
            raise
        spinner.stop(f"{len(result)} draft contracts")

        for contract in result:
            save_artifact(
                self.plan_dir
                / "contracts"
                / f"{contract.boundary_id}.md",
                self._render_contract(contract),
            )
        return result

    # -- Step 3: Resolve Contracts --------------------------------------------

    def resolve_contracts(
        self,
        tree: ComponentTree,
        contracts: list[InterfaceContract],
    ) -> list[InterfaceContract]:
        """Make contracts implementation-grade."""
        spinner = ProgressSpinner(
            "Contract Resolver", "resolving contracts"
        )
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/03_contract_resolver.md",
                variables={
                    "component_tree": tree.model_dump_json(indent=2),
                    "contracts": json.dumps(
                        [c.model_dump() for c in contracts], indent=2
                    ),
                },
            )
            result = call_model(
                context,
                tier=ModelTier.CODING,
                response_format=list[InterfaceContract],
            )
        except Exception:
            spinner.stop("failed")
            raise
        spinner.stop(f"{len(result)} contracts resolved")

        # Overwrite drafts with resolved versions
        for contract in result:
            save_artifact(
                self.plan_dir
                / "contracts"
                / f"{contract.boundary_id}.md",
                self._render_contract(contract),
            )
        return result

    # -- Step 4: Adversarial Review -------------------------------------------

    def run_adversary(
        self,
        tree: ComponentTree,
        contracts: list[InterfaceContract],
    ) -> list[Finding]:
        """Try to break the plan."""
        spinner = ProgressSpinner("Adversary", "attacking the plan")
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/04_adversary.md",
                variables={
                    "component_tree": tree.model_dump_json(indent=2),
                    "contracts": json.dumps(
                        [c.model_dump() for c in contracts], indent=2
                    ),
                },
            )
            result = call_model(
                context,
                tier=ModelTier.FRONTIER,
                response_format=list[Finding],
            )
        except Exception:
            spinner.stop("failed")
            raise
        spinner.stop(f"{len(result)} findings")

        return result

    # -- Step 5: Resolve Adversary Findings -----------------------------------

    def resolve_adversary(
        self,
        tree: ComponentTree,
        contracts: list[InterfaceContract],
        findings: list[Finding],
    ) -> tuple[ComponentTree, list[InterfaceContract], list[Resolution]]:
        """Address each finding from the Adversary."""
        spinner = ProgressSpinner(
            "Adversary Resolver", "addressing findings"
        )
        spinner.start()
        try:
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
                },
            )
            result = call_model(
                context, tier=ModelTier.FRONTIER, response_format=dict
            )
        except Exception:
            spinner.stop("failed")
            raise
        spinner.stop("Findings resolved")

        revised_tree = ComponentTree(**result["revised_tree"])
        revised_contracts = [
            InterfaceContract(**c) for c in result["revised_contracts"]
        ]
        resolutions = [Resolution(**r) for r in result["resolutions"]]

        # Save updated artifacts
        save_artifact(
            self.plan_dir / "architecture.md",
            self._render_architecture(revised_tree),
        )
        for contract in revised_contracts:
            save_artifact(
                self.plan_dir
                / "contracts"
                / f"{contract.boundary_id}.md",
                self._render_contract(contract),
            )
        save_artifact(
            self.plan_dir / "decisions.md",
            self._render_decisions(resolutions),
        )

        return revised_tree, revised_contracts, resolutions

    # -- Step 6: Sequence into Tasks ------------------------------------------

    def sequence(
        self,
        tree: ComponentTree,
        contracts: list[InterfaceContract],
    ) -> list[Task]:
        """Convert architecture into ordered task graph."""
        spinner = ProgressSpinner("Sequencer", "generating task graph")
        spinner.start()
        try:
            context = assemble_context(
                prompt_file="prompts/06_sequencer.md",
                variables={
                    "component_tree": tree.model_dump_json(indent=2),
                    "contracts": json.dumps(
                        [c.model_dump() for c in contracts], indent=2
                    ),
                },
            )
            result = call_model(
                context,
                tier=ModelTier.CODING,
                response_format=list[Task],
            )
        except Exception:
            spinner.stop("failed")
            raise
        spinner.stop(f"{len(result)} tasks generated")

        for task in result:
            slug = task.title.lower().replace(" ", "-")
            save_artifact(
                self.plan_dir / "tasks" / f"{task.id}-{slug}.md",
                self._render_task(task),
            )
        return result

    # -- Step 7: Simulate Readiness -------------------------------------------

    def simulate(
        self,
        tree: ComponentTree,
        contracts: list[InterfaceContract],
        tasks: list[Task],
    ) -> list[TaskVerdict]:
        """Check if each task can be one-shotted."""
        spinner = ProgressSpinner("Simulator", "checking readiness")
        spinner.start()
        try:
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
                },
            )
            result = call_model(
                context,
                tier=ModelTier.FRONTIER,
                response_format=list[TaskVerdict],
            )
        except Exception:
            spinner.stop("failed")
            raise
        ready = sum(1 for v in result if v.readiness == Readiness.READY)
        spinner.stop(f"{ready}/{len(result)} tasks ready")

        save_artifact(
            self.plan_dir / "simulation-report.md",
            self._render_simulation(result),
        )
        return result

    # -- Serialization helpers ------------------------------------------------

    def _serialize(self, **kwargs) -> dict:
        """Serialize Pydantic models for state storage."""
        out = {}
        for k, v in kwargs.items():
            if isinstance(v, list):
                out[k] = [
                    x.model_dump() if hasattr(x, "model_dump") else x
                    for x in v
                ]
            elif hasattr(v, "model_dump"):
                out[k] = v.model_dump()
            else:
                out[k] = v
        return out

    def _load_brief(self) -> StructuredBrief:
        return StructuredBrief(
            **self.state.get_output(Step.INTERVIEW)["brief"]
        )

    def _load_tree(self) -> ComponentTree:
        for step in [Step.ADVERSARY, Step.DECOMPOSE]:
            data = self.state.get_output(step)
            if data and "tree" in data:
                return ComponentTree(**data["tree"])
        raise ValueError("No component tree found in state")

    def _load_contracts(self) -> list[InterfaceContract]:
        for step in [
            Step.ADVERSARY,
            Step.RESOLVE_CONTRACTS,
            Step.WRITE_CONTRACTS,
        ]:
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

    # -- Full Orchestration ---------------------------------------------------

    def run(
        self,
        raw_input: str = None,
        resume: bool = False,
        from_step: str = None,
    ):
        """Run the complete planning pipeline.

        Three modes:
          swarm plan brief.md         -> fresh run
          swarm plan --resume         -> pick up where we left off
          swarm rerun decompose       -> rerun from a specific step
        """
        if from_step:
            step = Step(from_step)
            self.state.invalidate_from(step)
            print(
                f"   Rerunning from {STEP_LABELS[step]}. "
                f"Subsequent steps invalidated.\n"
            )

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
            msg = f"  >> {STEP_LABELS[step]} (cached)"
            if info:
                msg += f": {info}"
            print(msg)

        # -- INTERVIEW -------------------------------------------------------
        if not self.state.is_complete(Step.INTERVIEW):
            header(Step.INTERVIEW)
            self.state.mark_started(Step.INTERVIEW)
            brief = self.interview(raw_input)
            self.state.mark_complete(
                Step.INTERVIEW,
                output_data=self._serialize(brief=brief),
                log_msg=f"Brief: {brief.system_purpose}",
            )
        else:
            brief = self._load_brief()
            cached(Step.INTERVIEW, brief.system_purpose)

        # -- CODEBASE ANALYSIS ------------------------------------------------
        if not self.state.is_complete(Step.CODEBASE):
            header(Step.CODEBASE)
            self.state.mark_started(Step.CODEBASE)
            summary = self.analyze_codebase()
            self.state.mark_complete(
                Step.CODEBASE,
                output_data={"summary": summary},
                log_msg="Analyzed" if summary else "Greenfield (skipped)",
            )
        else:
            summary = (self.state.get_output(Step.CODEBASE) or {}).get(
                "summary"
            )
            cached(Step.CODEBASE)

        # -- DECOMPOSE --------------------------------------------------------
        if not self.state.is_complete(Step.DECOMPOSE):
            header(Step.DECOMPOSE)
            self.state.mark_started(Step.DECOMPOSE)
            tree = self.decompose(brief, summary)
            self.state.mark_complete(
                Step.DECOMPOSE,
                output_data=self._serialize(tree=tree),
                log_msg=f"{len(tree.components)} components",
            )
        else:
            tree = self._load_tree()
            cached(Step.DECOMPOSE, f"{len(tree.components)} components")

        # -- WRITE CONTRACTS --------------------------------------------------
        if not self.state.is_complete(Step.WRITE_CONTRACTS):
            header(Step.WRITE_CONTRACTS)
            self.state.mark_started(Step.WRITE_CONTRACTS)
            contracts = self.write_contracts(tree)
            self.state.mark_complete(
                Step.WRITE_CONTRACTS,
                output_data=self._serialize(contracts=contracts),
                log_msg=f"{len(contracts)} drafts",
            )
        else:
            contracts = self._load_contracts()
            cached(Step.WRITE_CONTRACTS, f"{len(contracts)} contracts")

        # -- RESOLVE CONTRACTS ------------------------------------------------
        if not self.state.is_complete(Step.RESOLVE_CONTRACTS):
            header(Step.RESOLVE_CONTRACTS)
            self.state.mark_started(Step.RESOLVE_CONTRACTS)
            contracts = self.resolve_contracts(tree, contracts)
            self.state.mark_complete(
                Step.RESOLVE_CONTRACTS,
                output_data=self._serialize(contracts=contracts),
                log_msg=f"{len(contracts)} resolved",
            )
        else:
            contracts = self._load_contracts()
            cached(Step.RESOLVE_CONTRACTS)

        # -- ADVERSARY LOOP ---------------------------------------------------
        if not self.state.is_complete(Step.ADVERSARY):
            header(Step.ADVERSARY)
            self.state.mark_started(Step.ADVERSARY)
            max_rounds = self.config.get("max_adversary_rounds", 3)
            rounds_completed = 0

            for i in range(
                self.state.data.get("adversary_round", 0), max_rounds
            ):
                print(f"\n   Round {i + 1}/{max_rounds}")
                findings = self.run_adversary(tree, contracts)
                critical = [
                    f
                    for f in findings
                    if f.severity in (Severity.CRITICAL, Severity.HIGH)
                ]
                print(
                    f"   -> {len(findings)} findings "
                    f"({len(critical)} critical/high)"
                )
                rounds_completed = i + 1

                if not critical:
                    print("   > Plan is stable.")
                    break

                tree, contracts, _ = self.resolve_adversary(
                    tree, contracts, findings
                )
                contracts = self.resolve_contracts(tree, contracts)
                self.state.data["adversary_round"] = i + 1
                self.state.save()  # checkpoint within the loop

            self.state.mark_complete(
                Step.ADVERSARY,
                output_data=self._serialize(tree=tree, contracts=contracts),
                log_msg=f"Stable after {rounds_completed} round(s)",
            )
        else:
            tree = self._load_tree()
            contracts = self._load_contracts()
            cached(Step.ADVERSARY)

        # -- HUMAN REVIEW -----------------------------------------------------
        if not self.state.is_complete(Step.HUMAN_REVIEW):
            header(Step.HUMAN_REVIEW)
            self.state.mark_started(Step.HUMAN_REVIEW)
            print(f"\n   Review the plan in: {self.plan_dir}/")
            print(f"   - architecture.md     -- component tree")
            print(f"   - contracts/          -- interface contracts")
            print(f"   - decisions.md        -- rationale")
            print(f"\n   Then run:")
            print(f"     swarm approve         -> continue pipeline")
            print(f"     swarm rerun adversary -> re-run adversary loop")
            return  # Pipeline pauses here

        # -- SEQUENCE ---------------------------------------------------------
        if not self.state.is_complete(Step.SEQUENCE):
            header(Step.SEQUENCE)
            self.state.mark_started(Step.SEQUENCE)
            tasks = self.sequence(tree, contracts)
            self.state.mark_complete(
                Step.SEQUENCE,
                output_data=self._serialize(tasks=tasks),
                log_msg=f"{len(tasks)} tasks",
            )
        else:
            tasks = self._load_tasks()
            cached(Step.SEQUENCE, f"{len(tasks)} tasks")

        # -- SIMULATE ---------------------------------------------------------
        if not self.state.is_complete(Step.SIMULATE):
            header(Step.SIMULATE)
            self.state.mark_started(Step.SIMULATE)
            verdicts = self.simulate(tree, contracts, tasks)
            ready = sum(
                1 for v in verdicts if v.readiness == Readiness.READY
            )
            self.state.mark_complete(
                Step.SIMULATE,
                output_data={
                    "verdicts": [v.model_dump() for v in verdicts]
                },
                log_msg=f"{ready}/{len(verdicts)} ready",
            )
        else:
            vdata = self.state.get_output(Step.SIMULATE)["verdicts"]
            verdicts = [TaskVerdict(**v) for v in vdata]
            cached(Step.SIMULATE)

        # -- REFINE -----------------------------------------------------------
        needs_work = [
            v
            for v in verdicts
            if v.readiness == Readiness.NEEDS_REFINEMENT
        ]
        if needs_work and not self.state.is_complete(Step.REFINE):
            header(Step.REFINE)
            self.state.mark_started(Step.REFINE)
            print(f"   {len(needs_work)} tasks need refinement")
            contracts = self.resolve_contracts(tree, contracts)
            tasks = self.sequence(tree, contracts)
            verdicts = self.simulate(tree, contracts, tasks)
            still_bad = sum(
                1
                for v in verdicts
                if v.readiness == Readiness.NEEDS_REFINEMENT
            )
            self.state.mark_complete(
                Step.REFINE,
                output_data=self._serialize(
                    tasks=tasks, contracts=contracts, verdicts=verdicts
                ),
                log_msg=(
                    f"{still_bad} still need work"
                    if still_bad
                    else "All ready"
                ),
            )
        elif not needs_work:
            self.state.mark_complete(Step.REFINE, log_msg="Not needed")

        # -- BEADS EXPORT -----------------------------------------------------
        if not self.state.is_complete(Step.BEADS):
            header(Step.BEADS)
            self.state.mark_started(Step.BEADS)
            write_to_beads(tasks, verdicts, self.project_dir)
            self.state.mark_complete(
                Step.BEADS, log_msg=f"{len(tasks)} tasks exported"
            )

        # -- DONE -------------------------------------------------------------
        elapsed = time.time() - start_time
        ready = sum(
            1 for v in verdicts if v.readiness == Readiness.READY
        )
        print(f"\n{'=' * 50}")
        print(f"  Planning complete ({elapsed:.0f}s)")
        print(f"{'=' * 50}")
        print(f"  Plan:    {self.plan_dir}/")
        print(f"  Tasks:   {len(tasks)} total, {ready} ready")
        print(f"  Next:    bd ready --json")
        print()
        self.state.print_status()
