import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "gitomb"


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise ValueError("Invalid scan or batch ID")
    return value


def save(path: Path, value: BaseModel | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(path: Path, model: type[BaseModel]):
    return model.model_validate_json(path.read_text())


def latest_scan(directory: Path) -> Path:
    candidates = list((directory / "scans").glob("*.json"))
    if not candidates:
        raise ValueError("No saved scan. Run gitomb scan first.")
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)
