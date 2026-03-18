"""Build and manage the stateful task graph (graph.json + prompt packets).

This module replaces the old dual-output system (markdown .plan/ + JSON .beads/)
with a single stateful task graph optimized for AI agent consumption.

The graph has two parts:
  1. graph.json  — compact index with task DAG, status, and metadata.
                   Always loaded first by the consuming agent.
  2. tasks/*.json — self-contained prompt packets, one per task.
                    Loaded on demand for the selected task only.

Design principles:
  - Two-phase loading: agent reads index (tiny), then one packet (small).
  - Self-contained packets: contracts inlined, no cross-referencing needed.
  - Token-budgeted: each packet knows its approximate token cost.
  - Stateful: tasks have status, discoveries track issues, changelog tracks
    mutations. The graph is a living document, not write-once.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .artifacts import save_json, load_json
from .schemas import (
    ChangelogEntry,
    ComponentTree,
    Complexity,
    Discovery,
    GraphTask,
    InlineContract,
    InlineContractFn,
    InterfaceContract,
    PromptPacket,
    Readiness,
    StructuredBrief,
    Task,
    TaskStatus,
    TaskVerdict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(packet: dict) -> int:
    """Rough token estimate for a prompt packet.

    Uses a simple heuristic: ~4 characters per token for JSON content.
    This is intentionally conservative (over-estimates) so the consuming
    agent doesn't run out of context.
    """
    raw = json.dumps(packet, separators=(",", ":"))
    return max(len(raw) // 3, 100)  # ~3 chars/token for dense JSON


def _inline_contract(contract: InterfaceContract) -> InlineContract:
    """Convert a full InterfaceContract to a compact InlineContract.

    Strips verbose fields and compresses function signatures into the
    minimal form a coding agent needs.
    """
    fns = []
    for fn_dict in contract.functions:
        fns.append(InlineContractFn(
            name=fn_dict.get("name", "unnamed"),
            params=fn_dict.get("params", {}),
            returns=fn_dict.get("returns", {}),
            errors=[
                ec.error_type for ec in contract.error_cases
                if not fn_dict.get("name")
                or ec.condition.lower().find(fn_dict["name"].lower()) != -1
            ] if contract.error_cases else [],
        ))

    # Build a common error shape from the first error case, or empty
    error_shape = {}
    if contract.error_cases:
        error_shape = contract.error_cases[0].response_shape

    stub_text = ""
    if contract.stub_strategy and contract.stub_strategy.can_stub:
        stub_text = contract.stub_strategy.stub_description

    return InlineContract(
        boundary=contract.boundary_id,
        pattern=contract.communication_pattern.value,
        fns=fns,
        error_shape=error_shape,
        stub=stub_text,
    )


def _compute_unlocks(tasks: list[Task]) -> dict[str, list[str]]:
    """Build reverse dependency map: task_id -> list of tasks it unlocks."""
    unlocks: dict[str, list[str]] = {}
    for task in tasks:
        for dep_id in task.depends_on:
            unlocks.setdefault(dep_id, []).append(task.id)
    return unlocks


def _initial_status(
    task: Task,
    verdict: Optional[TaskVerdict],
    done_tasks: set[str],
) -> TaskStatus:
    """Determine initial status for a task based on verdict and deps."""
    # If the verdict says blocked, respect that
    if verdict and verdict.readiness == Readiness.BLOCKED:
        return TaskStatus.PENDING

    # Check if all dependencies are satisfied
    all_deps_done = all(d in done_tasks for d in task.depends_on)

    if not task.depends_on or all_deps_done:
        return TaskStatus.READY

    return TaskStatus.PENDING


# ---------------------------------------------------------------------------
# Build graph from pipeline output
# ---------------------------------------------------------------------------


def build_graph(
    brief: StructuredBrief,
    tree: ComponentTree,
    tasks: list[Task],
    contracts: list[InterfaceContract],
    verdicts: list[TaskVerdict],
    project_dir: Path,
) -> None:
    """Build graph.json + task prompt packets from pipeline output.

    This is the primary export function, called at the end of the
    planning pipeline (replaces the old beads_bridge.write_to_beads).
    """
    plan_dir = project_dir / ".plan"
    tasks_dir = plan_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Build lookup tables
    contract_map: dict[str, InterfaceContract] = {
        c.boundary_id: c for c in contracts
    }
    verdict_map: dict[str, TaskVerdict] = {
        v.task_id: v for v in verdicts
    }
    unlocks_map = _compute_unlocks(tasks)
    done_tasks: set[str] = set()  # Fresh plan — nothing done yet

    # Build components summary for graph.json
    # Keyed by comp.name (matches task.component field) with ID as a field
    components = {}
    for comp in tree.components:
        # Find interfaces this component participates in
        interfaces = set()
        for c in contracts:
            if c.from_component == comp.id or c.to_component == comp.id:
                interfaces.add(c.boundary_id)
        components[comp.name] = {
            "id": comp.id,
            "responsibility": comp.responsibility,
            "owns": comp.owns_data,
            "interfaces": sorted(interfaces),
        }

    # Build graph tasks and prompt packets
    graph_tasks: list[dict] = []
    ready_count = 0

    for task in tasks:
        verdict = verdict_map.get(task.id)
        status = _initial_status(task, verdict, done_tasks)
        if status == TaskStatus.READY:
            ready_count += 1

        # Build the prompt packet (self-contained)
        task_contracts = []
        for boundary_id in task.contracts_to_satisfy:
            if boundary_id in contract_map:
                task_contracts.append(
                    _inline_contract(contract_map[boundary_id])
                )

        packet = PromptPacket(
            id=task.id,
            title=task.title,
            v=1,
            instruction=task.description,
            component=task.component,
            complexity=task.complexity.value,
            create=task.files_to_create,
            modify=task.files_to_modify,
            context=task.context_files,
            contracts=task_contracts,
            done_when=task.acceptance_criteria,
            not_this=task.not_in_scope,
            depends=task.depends_on,
            unlocks=unlocks_map.get(task.id, []),
        )

        packet_dict = packet.model_dump()
        packet_dict["tokens"] = _estimate_tokens(packet_dict)

        # Write individual task packet
        save_json(tasks_dir / f"{task.id}.json", packet_dict)

        # Build compact graph entry
        graph_tasks.append(GraphTask(
            id=task.id,
            title=task.title,
            component=task.component,
            complexity=task.complexity.value,
            status=status,
            depends=task.depends_on,
            tokens=packet_dict["tokens"],
        ).model_dump())

    # Build the graph
    graph = {
        "project": brief.system_purpose,
        "constraints": brief.technical_constraints,
        "components": components,
        "tasks": graph_tasks,
        "discoveries": [],
        "meta": {
            "total_tasks": len(tasks),
            "done": 0,
            "ready": ready_count,
            "blocked": len(tasks) - ready_count,
            "version": 1,
            "last_updated": _now_iso(),
            "changelog": [
                ChangelogEntry(
                    v=1,
                    action="plan_created",
                    detail=f"Created {len(tasks)} tasks across "
                           f"{len(components)} components",
                    affected=[t.id for t in tasks],
                    ts=_now_iso(),
                ).model_dump()
            ],
        },
    }

    save_json(plan_dir / "graph.json", graph)

    print(f"   Exported {len(tasks)} tasks to graph.json "
          f"({ready_count} ready)")


# ---------------------------------------------------------------------------
# Graph mutations
# ---------------------------------------------------------------------------


def load_graph(project_dir: Path) -> dict | None:
    """Load graph.json from the project directory."""
    return load_json(project_dir / ".plan" / "graph.json")


def save_graph(project_dir: Path, graph: dict) -> None:
    """Save graph.json to the project directory."""
    save_json(project_dir / ".plan" / "graph.json", graph)


def _next_version(graph: dict) -> int:
    """Get the next version number for the graph."""
    return graph["meta"]["version"] + 1


def _add_changelog(graph: dict, action: str, detail: str,
                   affected: list[str] = None) -> None:
    """Append a changelog entry and bump version."""
    v = _next_version(graph)
    graph["meta"]["version"] = v
    graph["meta"]["last_updated"] = _now_iso()
    graph["meta"]["changelog"].append(ChangelogEntry(
        v=v,
        action=action,
        detail=detail,
        affected=affected or [],
        ts=_now_iso(),
    ).model_dump())


def _recount_statuses(graph: dict) -> None:
    """Recompute the meta status counts from task data."""
    tasks = graph["tasks"]
    graph["meta"]["total_tasks"] = len(tasks)
    graph["meta"]["done"] = sum(
        1 for t in tasks if t["status"] == TaskStatus.DONE.value
    )
    graph["meta"]["ready"] = sum(
        1 for t in tasks if t["status"] == TaskStatus.READY.value
    )
    graph["meta"]["blocked"] = sum(
        1 for t in tasks
        if t["status"] in (
            TaskStatus.PENDING.value,
            TaskStatus.INVALIDATED.value,
            TaskStatus.NEEDS_UPDATE.value,
        )
    )


def recompute_statuses(graph: dict) -> dict:
    """Recompute all task statuses based on dependency state.

    Rules:
      - DONE tasks stay DONE (unless explicitly invalidated)
      - IN_PROGRESS tasks stay IN_PROGRESS
      - PENDING tasks become READY if all deps are DONE
      - READY tasks become PENDING if any dep is not DONE
      - INVALIDATED / NEEDS_UPDATE are only set explicitly
    """
    task_map = {t["id"]: t for t in graph["tasks"]}
    changed = True

    while changed:
        changed = False
        for task in graph["tasks"]:
            status = task["status"]

            # Don't auto-change these statuses
            if status in (
                TaskStatus.DONE.value,
                TaskStatus.IN_PROGRESS.value,
                TaskStatus.INVALIDATED.value,
                TaskStatus.NEEDS_UPDATE.value,
            ):
                continue

            deps = task.get("depends", [])
            all_deps_done = all(
                task_map.get(d, {}).get("status") == TaskStatus.DONE.value
                for d in deps
            )
            any_dep_broken = any(
                task_map.get(d, {}).get("status") in (
                    TaskStatus.INVALIDATED.value,
                    TaskStatus.NEEDS_UPDATE.value,
                )
                for d in deps
            )

            if status == TaskStatus.PENDING.value:
                if not deps or all_deps_done:
                    task["status"] = TaskStatus.READY.value
                    changed = True
            elif status == TaskStatus.READY.value:
                if any_dep_broken or (deps and not all_deps_done):
                    task["status"] = TaskStatus.PENDING.value
                    changed = True

    _recount_statuses(graph)
    return graph


def mark_task_done(project_dir: Path, task_id: str) -> dict:
    """Mark a task as done and cascade status changes.

    Returns the updated graph.
    """
    graph = load_graph(project_dir)
    if graph is None:
        raise FileNotFoundError("No graph.json found. Run 'swarm plan' first.")

    task_map = {t["id"]: t for t in graph["tasks"]}
    if task_id not in task_map:
        raise ValueError(f"Task {task_id} not found in graph.")

    task = task_map[task_id]
    old_status = task["status"]
    task["status"] = TaskStatus.DONE.value

    _add_changelog(graph, "task_completed",
                   f"Task {task_id} ({task['title']}) completed "
                   f"(was {old_status})",
                   affected=[task_id])

    # Cascade: dependents may become ready
    graph = recompute_statuses(graph)
    save_graph(project_dir, graph)

    return graph


def mark_task_in_progress(project_dir: Path, task_id: str) -> dict:
    """Mark a task as in-progress.

    Returns the updated graph.
    """
    graph = load_graph(project_dir)
    if graph is None:
        raise FileNotFoundError("No graph.json found. Run 'swarm plan' first.")

    task_map = {t["id"]: t for t in graph["tasks"]}
    if task_id not in task_map:
        raise ValueError(f"Task {task_id} not found in graph.")

    task = task_map[task_id]
    task["status"] = TaskStatus.IN_PROGRESS.value

    _add_changelog(graph, "task_started",
                   f"Task {task_id} ({task['title']}) started",
                   affected=[task_id])

    save_graph(project_dir, graph)
    return graph


def invalidate_task(project_dir: Path, task_id: str,
                    reason: str = "") -> dict:
    """Invalidate a completed task (upstream change broke it).

    Cascades: downstream tasks that depend on this become PENDING.
    Returns the updated graph.
    """
    graph = load_graph(project_dir)
    if graph is None:
        raise FileNotFoundError("No graph.json found.")

    task_map = {t["id"]: t for t in graph["tasks"]}
    if task_id not in task_map:
        raise ValueError(f"Task {task_id} not found in graph.")

    task = task_map[task_id]
    task["status"] = TaskStatus.INVALIDATED.value

    detail = f"Task {task_id} ({task['title']}) invalidated"
    if reason:
        detail += f": {reason}"

    # Find all downstream tasks that depend on this one
    downstream = [
        t["id"] for t in graph["tasks"]
        if task_id in t.get("depends", [])
    ]

    _add_changelog(graph, "task_invalidated", detail,
                   affected=[task_id] + downstream)

    # Cascade
    graph = recompute_statuses(graph)
    save_graph(project_dir, graph)

    return graph


# ---------------------------------------------------------------------------
# Discovery management
# ---------------------------------------------------------------------------


def add_discovery(
    project_dir: Path,
    discovery: Discovery,
) -> dict:
    """Add a discovery to the graph and cascade status changes.

    A discovery is something the coding agent found during implementation
    that affects the plan. The planner must resolve it before affected
    tasks can proceed.

    Returns the updated graph.
    """
    graph = load_graph(project_dir)
    if graph is None:
        raise FileNotFoundError("No graph.json found.")

    graph["discoveries"].append(discovery.model_dump())

    # Mark affected tasks as needs_update
    task_map = {t["id"]: t for t in graph["tasks"]}
    for tid in discovery.affects:
        if tid in task_map:
            t = task_map[tid]
            if t["status"] != TaskStatus.DONE.value:
                t["status"] = TaskStatus.NEEDS_UPDATE.value
            # Even done tasks get flagged — they need re-implementation
            elif t["status"] == TaskStatus.DONE.value:
                t["status"] = TaskStatus.INVALIDATED.value

    _add_changelog(
        graph, "discovery_added",
        f"[{discovery.severity.value.upper()}] {discovery.description} "
        f"(found during {discovery.found_during})",
        affected=discovery.affects,
    )

    graph = recompute_statuses(graph)
    save_graph(project_dir, graph)

    return graph


def resolve_discovery(
    project_dir: Path,
    discovery_idx: int,
    resolution: str,
) -> dict:
    """Mark a discovery as resolved and update affected tasks.

    After resolution, affected tasks move from NEEDS_UPDATE/INVALIDATED
    back to a computable status (READY or PENDING based on deps).

    Returns the updated graph.
    """
    graph = load_graph(project_dir)
    if graph is None:
        raise FileNotFoundError("No graph.json found.")

    if discovery_idx < 0 or discovery_idx >= len(graph["discoveries"]):
        raise IndexError(
            f"Discovery index {discovery_idx} out of range "
            f"(0-{len(graph['discoveries']) - 1})."
        )

    disc = graph["discoveries"][discovery_idx]
    disc["resolved"] = True
    disc["resolution"] = resolution

    # Move affected tasks from INVALIDATED/NEEDS_UPDATE back to PENDING
    # so recompute_statuses can figure out the right state
    task_map = {t["id"]: t for t in graph["tasks"]}
    for tid in disc["affects"]:
        if tid in task_map:
            t = task_map[tid]
            if t["status"] in (
                TaskStatus.INVALIDATED.value,
                TaskStatus.NEEDS_UPDATE.value,
            ):
                t["status"] = TaskStatus.PENDING.value

    _add_changelog(
        graph, "discovery_resolved",
        f"Resolved: {disc['description']} -> {resolution}",
        affected=disc["affects"],
    )

    graph = recompute_statuses(graph)
    save_graph(project_dir, graph)

    return graph


# ---------------------------------------------------------------------------
# Task packet updates
# ---------------------------------------------------------------------------


def update_task_packet(
    project_dir: Path,
    task_id: str,
    updates: dict,
) -> dict:
    """Update fields in a task's prompt packet and bump its version.

    The `updates` dict can contain any PromptPacket field except `id`.
    After updating the packet, the graph task entry is also updated
    if relevant fields changed (title, component, complexity).

    Returns the updated packet.
    """
    tasks_dir = project_dir / ".plan" / "tasks"
    packet_path = tasks_dir / f"{task_id}.json"

    packet = load_json(packet_path)
    if packet is None:
        raise FileNotFoundError(f"Task packet {task_id}.json not found.")

    # Apply updates
    for key, value in updates.items():
        if key == "id":
            continue  # Never change the ID
        if key in packet:
            packet[key] = value

    # Bump version
    packet["v"] = packet.get("v", 1) + 1

    # Recompute token estimate
    packet["tokens"] = _estimate_tokens(packet)

    save_json(packet_path, packet)

    # Update graph entry if needed
    graph = load_graph(project_dir)
    if graph:
        task_map = {t["id"]: t for t in graph["tasks"]}
        if task_id in task_map:
            gt = task_map[task_id]
            if "title" in updates:
                gt["title"] = updates["title"]
            if "component" in updates:
                gt["component"] = updates["component"]
            if "complexity" in updates:
                gt["complexity"] = updates["complexity"]
            gt["tokens"] = packet["tokens"]

            _add_changelog(
                graph, "task_updated",
                f"Task {task_id} packet updated (v{packet['v']}): "
                f"{', '.join(updates.keys())}",
                affected=[task_id],
            )

            # If task was invalidated/needs_update, move to pending
            if gt["status"] in (
                TaskStatus.INVALIDATED.value,
                TaskStatus.NEEDS_UPDATE.value,
            ):
                gt["status"] = TaskStatus.PENDING.value

            graph = recompute_statuses(graph)
            save_graph(project_dir, graph)

    return packet


# ---------------------------------------------------------------------------
# Query helpers (for CLI and agents)
# ---------------------------------------------------------------------------


def get_ready_tasks(project_dir: Path) -> list[dict]:
    """Return all tasks with status 'ready'."""
    graph = load_graph(project_dir)
    if graph is None:
        return []
    return [t for t in graph["tasks"]
            if t["status"] == TaskStatus.READY.value]


def get_unresolved_discoveries(project_dir: Path) -> list[dict]:
    """Return all unresolved discoveries."""
    graph = load_graph(project_dir)
    if graph is None:
        return []
    return [
        {"index": i, **d}
        for i, d in enumerate(graph["discoveries"])
        if not d.get("resolved", False)
    ]


def get_task_packet(project_dir: Path, task_id: str) -> dict | None:
    """Load a single task's prompt packet."""
    return load_json(project_dir / ".plan" / "tasks" / f"{task_id}.json")
