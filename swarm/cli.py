"""CLI argument parsing and command routing."""

import shutil
import webbrowser

import click
from pathlib import Path

from .config import (
    CONFIG_FILE,
    config_exists,
    load_config,
    require_auth,
    save_config,
)
from .graph_builder import (
    add_discovery,
    get_ready_tasks,
    get_task_packet,
    get_unresolved_discoveries,
    load_graph,
    mark_task_done,
    mark_task_in_progress,
)
from .runner import PlanningSwarm, PipelineState, Step, STEP_LABELS, STEP_ORDER
from .schemas import Discovery, DiscoveryType, Severity

API_KEY_URL = "https://console.anthropic.com/settings/keys"


@click.group()
def cli():
    """Planning Swarm -- Multi-agent planning orchestrator"""
    pass


@cli.command()
@click.option(
    "--opencode", "use_opencode", is_flag=True,
    help="Use OpenCode + your Claude subscription instead of an API key",
)
def init(use_opencode):
    """Set up authentication and preferences.

    Two auth methods:

        swarm init             # use an Anthropic API key (pay-as-you-go)

        swarm init --opencode  # use OpenCode + Claude subscription (no API key)
    """
    if config_exists():
        cfg = load_config()
        method = cfg.get("auth_method", "none")
        if method == "opencode":
            click.echo(f"\n  Existing config: OpenCode (Claude subscription)")
        elif cfg["api_key"]:
            masked = cfg["api_key"][:10] + "..."
            click.echo(f"\n  Existing config: API key {masked}")
        else:
            click.echo(f"\n  Existing config found (no auth)")
        click.echo(f"  Config file: {CONFIG_FILE}\n")
        if not click.confirm("  Overwrite?", default=False):
            click.echo("  Cancelled.")
            return
        click.echo()

    if use_opencode:
        _init_opencode()
    else:
        _init_api_key()


def _init_opencode():
    """Set up swarm to use OpenCode as the LLM backend."""
    if not shutil.which("opencode"):
        click.echo("\n  Error: 'opencode' is not installed.\n")
        click.echo("  Install it from: https://opencode.ai")
        click.echo("  Then run: opencode auth login")
        click.echo("  Then run: swarm init --opencode")
        return

    click.echo("\n  Planning Swarm Setup (OpenCode)")
    click.echo("  --------------------------------\n")
    click.echo("  LLM calls will run through OpenCode using your")
    click.echo("  Claude subscription. No API key needed.\n")
    click.echo("  Make sure you're logged in: opencode auth login\n")

    click.echo("  Model configuration (press Enter for defaults):\n")

    frontier = click.prompt(
        "  Frontier model (architecture, adversary)",
        default="claude-opus-4-6",
        show_default=True,
    )
    coding = click.prompt(
        "  Coding model (contracts, sequencing)",
        default="claude-sonnet-4-6",
        show_default=True,
    )
    fast = click.prompt(
        "  Fast model (interviewer)",
        default="claude-haiku-4-5-20251001",
        show_default=True,
    )

    save_config(
        auth_method="opencode",
        models={"frontier": frontier, "coding": coding, "fast": fast},
    )

    click.echo(f"\n  Config saved to {CONFIG_FILE}")
    click.echo("  Auth: OpenCode (Claude subscription)")
    click.echo("\n  You're ready to go:")
    click.echo("    swarm plan -i \"build a chat app\"")
    click.echo("    swarm plan brief.md\n")


def _init_api_key():
    """Set up swarm with an Anthropic API key."""
    click.echo("\n  Planning Swarm Setup")
    click.echo("  --------------------\n")

    click.echo(f"  Opening Anthropic Console to create an API key...\n")
    click.echo(f"  If your browser doesn't open, visit:")
    click.echo(f"  {API_KEY_URL}\n")

    try:
        webbrowser.open(API_KEY_URL)
    except Exception:
        pass

    click.echo("  1. Log in to your Anthropic account")
    click.echo("  2. Click \"Create Key\"")
    click.echo("  3. Copy the key and paste it below\n")

    api_key = click.prompt("  Paste your API key", hide_input=True)

    if not api_key.strip():
        click.echo("\n  Error: API key cannot be empty.")
        return

    click.echo("\n  Model configuration (press Enter for defaults):\n")

    frontier = click.prompt(
        "  Frontier model (architecture, adversary)",
        default="claude-opus-4-6",
        show_default=True,
    )
    coding = click.prompt(
        "  Coding model (contracts, sequencing)",
        default="claude-sonnet-4-6",
        show_default=True,
    )
    fast = click.prompt(
        "  Fast model (interviewer)",
        default="claude-haiku-4-5-20251001",
        show_default=True,
    )

    save_config(
        auth_method="api_key",
        api_key=api_key.strip(),
        models={"frontier": frontier, "coding": coding, "fast": fast},
    )

    click.echo(f"\n  Config saved to {CONFIG_FILE}")
    click.echo("  Permissions set to owner-only (600).")
    click.echo("\n  You're ready to go:")
    click.echo("    swarm plan -i \"build a chat app\"")
    click.echo("    swarm plan brief.md\n")


@cli.command()
@click.argument("brief_file", type=click.Path(exists=True), required=False)
@click.option(
    "--codebase",
    type=click.Path(exists=True),
    default=None,
    help="Path to existing codebase for brownfield projects",
)
@click.option(
    "--max-rounds", default=None, type=int,
    help="Maximum adversarial review rounds",
)
@click.option(
    "--inline",
    "-i",
    default=None,
    help="Provide brief as inline text instead of a file",
)
@click.option(
    "--resume", is_flag=True, help="Resume from last completed step"
)
def plan(brief_file, codebase, max_rounds, inline, resume):
    """Run the full planning pipeline.

    Examples:

        swarm plan brief.md                  # from a file

        swarm plan -i "build a chat app"     # inline

        swarm plan --resume                  # pick up where you left off

        swarm plan brief.md --codebase ./src # brownfield
    """
    require_auth()

    cfg = load_config()
    config = {
        "existing_codebase": codebase,
        "max_adversary_rounds": max_rounds or cfg["max_adversary_rounds"],
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

    Run this after reviewing .plan/review.md during the human review
    checkpoint.
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

    require_auth()

    state.mark_complete(Step.HUMAN_REVIEW, log_msg="Approved by human")
    click.echo("   Plan approved. Continuing pipeline...\n")

    cfg = load_config()
    config = {"max_adversary_rounds": cfg["max_adversary_rounds"]}
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

    require_auth()

    cfg = load_config()
    config = {"max_adversary_rounds": cfg["max_adversary_rounds"]}
    swarm = PlanningSwarm(Path.cwd(), config)
    swarm.run(from_step=step_name)


@cli.command()
@click.option("--graph", is_flag=True, help="Show task graph status instead of pipeline status")
def status(graph):
    """Show current pipeline and task graph status.

    Examples:

        swarm status          # pipeline steps + task graph summary

        swarm status --graph  # detailed task graph view
    """
    state_file = Path.cwd() / ".plan" / ".state.json"
    if not state_file.exists():
        click.echo("No plan found. Run 'swarm plan' first.")
        return

    state = PipelineState(state_file)

    cfg = load_config()
    method = cfg.get("auth_method", "")

    click.echo("\n  Planning Swarm -- Status\n")
    if method == "opencode":
        click.echo("  Auth: OpenCode (Claude subscription)\n")
    elif method == "api_key":
        click.echo("  Auth: API key\n")

    # Always show pipeline status
    state.print_status()

    # Show task graph if available
    graph_data = load_graph(Path.cwd())
    if graph_data:
        meta = graph_data["meta"]
        click.echo(f"\n  Task Graph (v{meta['version']}):")
        click.echo(
            f"    {meta['total_tasks']} total | "
            f"{meta['done']} done | "
            f"{meta['ready']} ready | "
            f"{meta['blocked']} blocked"
        )

        # Show unresolved discoveries
        unresolved = [
            d for d in graph_data.get("discoveries", [])
            if not d.get("resolved", False)
        ]
        if unresolved:
            click.echo(f"\n    Unresolved discoveries: {len(unresolved)}")
            for d in unresolved:
                click.echo(
                    f"      [{d['severity'].upper()}] {d['description']} "
                    f"(found during {d['found_during']})"
                )

        if graph or True:  # Always show ready tasks summary
            ready_tasks = [
                t for t in graph_data["tasks"]
                if t["status"] == "ready"
            ]
            if ready_tasks:
                click.echo(f"\n    Ready tasks:")
                for t in ready_tasks:
                    tokens = f" ~{t['tokens']}tok" if t.get("tokens") else ""
                    click.echo(
                        f"      {t['id']}  {t['title']}"
                        f"  [{t['complexity']}]{tokens}"
                    )

        if graph:
            # Full task listing
            click.echo(f"\n    All tasks:")
            for t in graph_data["tasks"]:
                status_icon = {
                    "done": "[done]",
                    "ready": "[READY]",
                    "in_progress": "[....]",
                    "pending": "[    ]",
                    "invalidated": "[!!!!]",
                    "needs_update": "[upd ]",
                }.get(t["status"], f"[{t['status']}]")
                deps = f" <- {','.join(t['depends'])}" if t["depends"] else ""
                click.echo(
                    f"      {status_icon} {t['id']}  {t['title']}"
                    f"  ({t['component']}){deps}"
                )

    else:
        rp = state.get_resume_point()
        if rp:
            click.echo(
                f"\n  Next: swarm plan --resume  "
                f"(continues from {STEP_LABELS[rp]})"
            )

    if state.data.get("log"):
        click.echo("\n  Recent activity:")
        for entry in state.data["log"][-5:]:
            t = entry["time"][:19]
            click.echo(
                f"    {t}  {entry['step']}: {entry['message']}"
            )

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

    # Also show graph changelog if available
    graph_data = load_graph(Path.cwd())
    if graph_data and graph_data["meta"].get("changelog"):
        click.echo("\n  Graph changelog:")
        for entry in graph_data["meta"]["changelog"]:
            click.echo(f"  v{entry['v']}  {entry['action']}")
            click.echo(f"    {entry['detail']}")


@cli.command()
def reset():
    """Reset the pipeline. Deletes all state (keeps artifacts)."""
    state_file = Path.cwd() / ".plan" / ".state.json"
    if state_file.exists():
        state_file.unlink()
        click.echo(
            "   Pipeline state reset. Artifacts in .plan/ preserved."
        )
        click.echo("   Run 'swarm plan' to start fresh.")
    else:
        click.echo("   No state to reset.")


# ---------------------------------------------------------------------------
# Task graph management commands
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
def done(task_id):
    """Mark a task as completed and update the graph.

    Automatically recomputes which blocked tasks become ready.

    Examples:

        swarm done 001

        swarm done 003
    """
    try:
        graph = mark_task_done(Path.cwd(), task_id)
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"   Error: {e}")
        return

    meta = graph["meta"]
    click.echo(f"   Task {task_id} marked as done.")
    click.echo(
        f"   Graph: {meta['done']} done, "
        f"{meta['ready']} ready, "
        f"{meta['blocked']} blocked"
    )

    # Show newly ready tasks
    newly_ready = [
        t for t in graph["tasks"]
        if t["status"] == "ready"
    ]
    if newly_ready:
        click.echo(f"\n   Ready tasks:")
        for t in newly_ready:
            click.echo(f"     {t['id']}  {t['title']}")


@cli.command()
@click.argument("task_id")
def start(task_id):
    """Mark a task as in-progress.

    Example:

        swarm start 002
    """
    try:
        graph = mark_task_in_progress(Path.cwd(), task_id)
    except (FileNotFoundError, ValueError) as e:
        click.echo(f"   Error: {e}")
        return

    click.echo(f"   Task {task_id} marked as in-progress.")

    # Show the task packet path
    click.echo(f"   Packet: .plan/tasks/{task_id}.json")


@cli.command()
@click.argument("task_id")
def show(task_id):
    """Show a task's full prompt packet.

    Displays the self-contained prompt packet that a coding agent
    would receive for this task.

    Example:

        swarm show 002
    """
    import json

    packet = get_task_packet(Path.cwd(), task_id)
    if packet is None:
        click.echo(f"   Task {task_id} not found.")
        return

    click.echo(json.dumps(packet, indent=2))


@cli.command()
@click.argument("task_id")
@click.argument("description")
@click.option(
    "--type", "-t", "disc_type",
    type=click.Choice([dt.value for dt in DiscoveryType]),
    default=DiscoveryType.SCOPE_CHANGE.value,
    help="Type of discovery",
)
@click.option(
    "--affects", "-a", multiple=True,
    help="Task IDs affected by this discovery (can specify multiple)",
)
@click.option(
    "--severity", "-s",
    type=click.Choice([s.value for s in Severity]),
    default=Severity.HIGH.value,
    help="Severity of the discovery",
)
def discover(task_id, description, disc_type, affects, severity):
    """Report a discovery made during task implementation.

    When a coding agent finds something during implementation that
    affects the plan, use this command to record it. Affected tasks
    will be flagged for review.

    Examples:

        swarm discover 003 "auth-to-api needs revokeToken" -t missing_contract_fn -a 002

        swarm discover 005 "database schema needs migration step" -t task_split_needed -a 004
    """
    disc = Discovery(
        found_during=task_id,
        type=DiscoveryType(disc_type),
        description=description,
        affects=list(affects) if affects else [],
        severity=Severity(severity),
    )

    try:
        graph = add_discovery(Path.cwd(), disc)
    except FileNotFoundError as e:
        click.echo(f"   Error: {e}")
        return

    unresolved = [
        d for d in graph["discoveries"]
        if not d.get("resolved", False)
    ]
    click.echo(f"   Discovery recorded.")
    click.echo(f"   Unresolved discoveries: {len(unresolved)}")

    if disc.affects:
        click.echo(f"   Affected tasks flagged for update: {', '.join(disc.affects)}")


@cli.command()
def ready():
    """List all tasks that are ready to be worked on.

    Shows tasks whose dependencies are all complete and that
    haven't been invalidated.
    """
    tasks = get_ready_tasks(Path.cwd())
    if not tasks:
        click.echo("   No ready tasks.")

        graph = load_graph(Path.cwd())
        if graph:
            meta = graph["meta"]
            if meta["done"] == meta["total_tasks"]:
                click.echo("   All tasks complete!")
            else:
                click.echo(
                    f"   {meta['done']}/{meta['total_tasks']} done. "
                    f"Remaining tasks are blocked on dependencies."
                )
        return

    click.echo(f"\n   Ready tasks ({len(tasks)}):\n")
    for t in tasks:
        tokens = f" ~{t['tokens']}tok" if t.get("tokens") else ""
        deps = f" (after: {', '.join(t['depends'])})" if t["depends"] else ""
        click.echo(
            f"   {t['id']}  {t['title']}  "
            f"[{t['complexity']}]{tokens}{deps}"
        )

    click.echo(f"\n   Run: swarm show <id>   to see full task packet")
    click.echo(f"        swarm start <id>  to mark as in-progress")


if __name__ == "__main__":
    cli()
