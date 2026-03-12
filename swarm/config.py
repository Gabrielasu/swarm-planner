"""Configuration management for Planning Swarm.

Config is read from (highest priority wins):
  1. Environment variables (ANTHROPIC_API_KEY, SWARM_FRONTIER_MODEL, etc.)
  2. Config file at ~/.config/swarm/config.toml
  3. Built-in defaults

Two auth methods:
  - api_key: Direct Anthropic API key (pay-as-you-go)
  - opencode: Shell out to `opencode run` (uses Claude subscription)
"""

import os
import shutil
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # Fallback
    except ImportError:
        tomllib = None


CONFIG_DIR = Path.home() / ".config" / "swarm"
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULTS = {
    "api": {
        "anthropic_api_key": "",
        "auth_method": "",  # "api_key" or "opencode"
    },
    "models": {
        "frontier": "claude-opus-4-6",
        "coding": "claude-sonnet-4-6",
        "fast": "claude-haiku-4-5-20251001",
    },
    "defaults": {
        "max_adversary_rounds": 3,
    },
}


def _read_config_file() -> dict:
    """Read the TOML config file, returning empty dict if missing."""
    if not CONFIG_FILE.exists():
        return {}
    if tomllib is None:
        return _read_config_fallback()
    return tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _read_config_fallback() -> dict:
    """Minimal key=value parser when tomllib is unavailable."""
    config: dict = {}
    section = ""
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            config.setdefault(section, {})
        elif "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if section:
                config.setdefault(section, {})[key] = value
            else:
                config[key] = value
    return config


def load_config() -> dict:
    """Load merged config from file + env vars + defaults.

    Returns a flat dict with keys:
      - api_key: str
      - auth_method: str  ("api_key" or "opencode")
      - frontier_model: str
      - coding_model: str
      - fast_model: str
      - max_adversary_rounds: int
    """
    file_cfg = _read_config_file()

    api_section = file_cfg.get("api", {})
    models_section = file_cfg.get("models", {})
    defaults_section = file_cfg.get("defaults", {})

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or api_section.get("anthropic_api_key")
        or ""
    )

    auth_method = api_section.get("auth_method", "")
    # If there's an env var API key, that takes priority
    if os.environ.get("ANTHROPIC_API_KEY"):
        auth_method = "api_key"
    elif not auth_method and api_key:
        auth_method = "api_key"

    return {
        "api_key": api_key,
        "auth_method": auth_method,
        "frontier_model": (
            os.environ.get("SWARM_FRONTIER_MODEL")
            or models_section.get("frontier")
            or DEFAULTS["models"]["frontier"]
        ),
        "coding_model": (
            os.environ.get("SWARM_CODING_MODEL")
            or models_section.get("coding")
            or DEFAULTS["models"]["coding"]
        ),
        "fast_model": (
            os.environ.get("SWARM_FAST_MODEL")
            or models_section.get("fast")
            or DEFAULTS["models"]["fast"]
        ),
        "max_adversary_rounds": int(
            os.environ.get("SWARM_MAX_ROUNDS")
            or defaults_section.get("max_adversary_rounds")
            or DEFAULTS["defaults"]["max_adversary_rounds"]
        ),
    }


def save_config(
    auth_method: str = "api_key",
    api_key: str = "",
    models: dict = None,
    defaults: dict = None,
):
    """Write config to ~/.config/swarm/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Planning Swarm configuration",
        "# This file contains your API key -- do NOT commit it to git.",
        "",
        "[api]",
        f'auth_method = "{auth_method}"',
    ]

    if api_key:
        lines.append(f'anthropic_api_key = "{api_key}"')
    else:
        lines.append('anthropic_api_key = ""')

    lines.append("")
    lines.append("[models]")

    m = models or {}
    lines.append(f'frontier = "{m.get("frontier", DEFAULTS["models"]["frontier"])}"')
    lines.append(f'coding = "{m.get("coding", DEFAULTS["models"]["coding"])}"')
    lines.append(f'fast = "{m.get("fast", DEFAULTS["models"]["fast"])}"')

    lines.append("")
    lines.append("[defaults]")
    d = defaults or {}
    lines.append(
        f'max_adversary_rounds = {d.get("max_adversary_rounds", DEFAULTS["defaults"]["max_adversary_rounds"])}'
    )
    lines.append("")

    CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")

    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def require_auth():
    """Validate that auth is configured. Exits with helpful message if not."""
    cfg = load_config()

    if cfg["auth_method"] == "opencode":
        if not shutil.which("opencode"):
            print("Error: auth_method is 'opencode' but opencode is not installed.\n")
            print("Install it: https://opencode.ai")
            print("Or switch to API key: swarm init")
            sys.exit(1)
        return

    if cfg["api_key"]:
        return

    print("Error: No authentication configured.\n")
    print("Option 1 -- Use your Claude subscription (via OpenCode):")
    print("  swarm init --opencode\n")
    print("Option 2 -- Use an Anthropic API key:")
    print("  swarm init\n")
    sys.exit(1)


def config_exists() -> bool:
    """Check if a config file exists."""
    return CONFIG_FILE.exists()
