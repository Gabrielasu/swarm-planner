"""Read/write/validate .plan/ artifacts."""

import json
from pathlib import Path


def save_artifact(path: Path, content: str) -> None:
    """Write an artifact file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_artifact(path: Path) -> str | None:
    """Read an artifact file, returning None if it doesn't exist."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def artifact_exists(path: Path) -> bool:
    """Check if an artifact file exists."""
    return path.exists()


def save_json(path: Path, data: dict | list) -> None:
    """Write a JSON artifact file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_json(path: Path) -> dict | list | None:
    """Read a JSON artifact file, returning None if it doesn't exist."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None
