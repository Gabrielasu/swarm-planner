"""Read/write/validate .plan/ artifacts."""

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
