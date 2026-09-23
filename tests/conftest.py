from pathlib import Path

import pytest

from gitomb.git import git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Gitomb Tests")
    git(path, "config", "user.email", "gitomb@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (path / "file.txt").write_text("initial\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "Initial commit")
    return path


def commit(repo: Path, text: str, message: str = "Work") -> str:
    (repo / "file.txt").write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")
