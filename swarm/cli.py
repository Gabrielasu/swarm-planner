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
from .runner import PlanningSwarm, PipelineState, Step, STEP_LABELS, STEP_ORDER

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
    sequence, simulate, refine, beads_export

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
def status():
    """Show current pipeline status."""
    state_file = Path.cwd() / ".plan" / ".state.json"
    if not state_file.exists():
        click.echo("No plan found. Run 'swarm plan' first.")
        return

    state = PipelineState(state_file)

    cfg = load_config()
    method = cfg.get("auth_method", "")

    click.echo("\n  Planning Swarm -- Pipeline Status\n")
    if method == "opencode":
        click.echo("  Auth: OpenCode (Claude subscription)\n")
    elif method == "api_key":
        click.echo("  Auth: API key\n")

    state.print_status()

    if state.data.get("log"):
        click.echo("\n  Recent activity:")
        for entry in state.data["log"][-5:]:
            t = entry["time"][:19]
            click.echo(
                f"    {t}  {entry['step']}: {entry['message']}"
            )

    rp = state.get_resume_point()
    if rp:
        click.echo(
            f"\n  Next: swarm plan --resume  "
            f"(continues from {STEP_LABELS[rp]})"
        )
    else:
        click.echo("\n  All steps complete.")
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
        click.echo(
            "   Pipeline state reset. Artifacts in .plan/ preserved."
        )
        click.echo("   Run 'swarm plan' to start fresh.")
    else:
        click.echo("   No state to reset.")


if __name__ == "__main__":
    cli()
