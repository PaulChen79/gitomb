import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: str | Path, *args: str, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"Git invocation failed: {type(exc).__name__}") from exc
    if check and result.returncode:
        raise GitError(result.stderr.strip() or f"git {args[0]} exited {result.returncode}")
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def common_dir(repo: str | Path) -> str:
    return str(Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


def oid(repo: str | Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref, check=False)


def in_use_branches(repo: str | Path) -> set[str]:
    return {
        line.removeprefix("branch refs/heads/")
        for line in git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("branch refs/heads/")
    }


def stashes(repo: str | Path) -> list[tuple[str, str, int, str]]:
    output = git(repo, "stash", "list", "--format=%gd%x00%H%x00%ct%x00%gs")
    return [
        (selector, sha, int(timestamp), subject)
        for line in output.splitlines()
        for selector, sha, timestamp, subject in [line.split("\0", 3)]
    ]


def discover(roots: list[Path], max_depth: int) -> tuple[list[Path], list[str]]:
    repos: dict[str, Path] = {}
    warnings = []
    ignored = {".git", ".venv", "node_modules", "vendor", ".cache", "__pycache__"}
    for root in roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            warnings.append(f"Not a directory: {root}")
            continue
        for directory, children, _ in os.walk(
            root, onerror=lambda exc: warnings.append(str(exc)), followlinks=False
        ):
            path = Path(directory)
            depth = len(path.relative_to(root).parts)
            children[:] = sorted(
                child for child in children if child not in ignored and depth < max_depth
            )
            if (path / ".git").exists():
                try:
                    repos.setdefault(common_dir(path), path)
                except GitError as exc:
                    warnings.append(f"{path}: {exc}")
    return sorted(repos.values()), warnings
