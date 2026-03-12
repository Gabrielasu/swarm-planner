"""Write task graph to Beads format for downstream consumption."""

import json
from pathlib import Path

from .schemas import Task, TaskVerdict, Readiness


def write_to_beads(
    tasks: list[Task],
    verdicts: list[TaskVerdict],
    project_dir: Path,
) -> None:
    """Export the task graph in a Beads-compatible JSON format.

    Creates a .beads/ directory with task files that can be consumed by
    the Beads task runner (bd CLI).
    """
    beads_dir = project_dir / ".beads"
    beads_dir.mkdir(parents=True, exist_ok=True)

    # Build a verdict lookup
    verdict_map = {v.task_id: v for v in verdicts}

    # Build the task graph manifest
    manifest = {
        "version": "1.0",
        "tasks": [],
    }

    for task in tasks:
        verdict = verdict_map.get(task.id)
        readiness = verdict.readiness.value if verdict else "unknown"

        task_entry = {
            "id": task.id,
            "title": task.title,
            "component": task.component,
            "description": task.description,
            "complexity": task.complexity.value,
            "readiness": readiness,
            "depends_on": task.depends_on,
            "contracts": task.contracts_to_satisfy,
            "files_to_create": task.files_to_create,
            "files_to_modify": task.files_to_modify,
            "context_files": task.context_files,
            "acceptance_criteria": task.acceptance_criteria,
            "not_in_scope": task.not_in_scope,
        }

        if task.estimated_tokens:
            task_entry["estimated_tokens"] = task.estimated_tokens

        if verdict and verdict.gaps:
            task_entry["gaps"] = verdict.gaps

        manifest["tasks"].append(task_entry)

        # Also write individual task files
        task_file = beads_dir / f"{task.id}.json"
        task_file.write_text(json.dumps(task_entry, indent=2), encoding="utf-8")

    # Write the manifest
    manifest_file = beads_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    ready_count = sum(
        1
        for v in verdicts
        if v.readiness == Readiness.READY
    )
    print(f"   Exported {len(tasks)} tasks to {beads_dir}/ "
          f"({ready_count} ready)")
