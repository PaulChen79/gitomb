import fnmatch
import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from gitomb.git import GitError, common_dir, discover, git, in_use_branches, oid, stashes
from gitomb.models import Item, Scan

DEFAULT_PROTECTED = ["main", "master", "develop", "development", "release", "release/*"]


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]


def is_protected(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def resolve_base(repo: Path, requested: str | None) -> str | None:
    candidates = (
        [requested]
        if requested
        else [
            git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False),
            "main",
            "master",
            "develop",
        ]
    )
    for candidate in candidates:
        if not candidate or candidate.startswith("-"):
            continue
        resolved = git(
            repo,
            "rev-parse",
            "--symbolic-full-name",
            "--verify",
            "--end-of-options",
            candidate,
            check=False,
        )
        if resolved.startswith(("refs/heads/", "refs/remotes/")) and oid(repo, resolved):
            return resolved
    return None


def classify(item: Item, stale_days: int) -> None:
    item.recommendation = "review"
    item.reasons = []
    if item.protected or item.in_use:
        item.recommendation = "keep"
        item.reasons.append("protected branch" if item.protected else "checked out in a worktree")
        return
    if item.kind == "branch":
        if item.base is None:
            item.reasons.append("no comparison branch; set --base")
        elif item.merged and item.unique_commits == 0:
            item.recommendation = "cleanup-candidate"
            item.reasons.append(f"merged into {item.base}; no unique commits")
        else:
            item.reasons.append(f"{item.unique_commits} commits not reachable from {item.base}")
        if item.upstream and not item.upstream_exists:
            item.reasons.append("upstream absent from local refs (no fetch performed)")
    else:
        item.reasons.append("stash contents require review")
    if item.age_days >= stale_days:
        item.reasons.append(f"last commit {item.age_days} days ago")
    ai = item.assessment
    if ai and item.recommendation != "cleanup-candidate":
        if ai.insufficient_context_probability >= 0.5 or ai.confidence < 0.5:
            item.reasons.append("Jev: insufficient or ambiguous evidence")
        elif ai.unfinished_probability >= 0.8:
            item.recommendation = "keep"
            item.reasons.append("Jev: signs of unfinished work")
        elif ai.purpose in {"temporary-debug", "experiment"}:
            item.reasons.append(f"Jev: likely {ai.purpose}; inspect before removing")


def priority(item: Item) -> tuple[int, float, int, str]:
    rank = {"cleanup-candidate": 0, "review": 1, "keep": 2}[item.recommendation]
    temporary = 0.0
    if item.assessment:
        temporary = sum(
            item.assessment.probabilities.get(k, 0) for k in ("temporary-debug", "experiment")
        )
        temporary *= 1 - item.assessment.unfinished_probability
        temporary *= 1 - item.assessment.insufficient_context_probability
    return rank, -temporary, -item.age_days, item.id


def scan_repo(repo: Path, base: str | None, protected: list[str], stale_days: int) -> list[Item]:
    common = common_dir(repo)
    active = in_use_branches(repo)
    target = resolve_base(repo, base)
    target_oid = oid(repo, target) if target else None
    timestamp = time.time()
    items = []
    refs = git(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(committerdate:unix)%00%(subject)%00%(upstream)",
        "refs/heads/",
    )
    rows = []
    for line in refs.splitlines():
        name, sha, date, subject, upstream = line.split("\0", 4)
        name = name.removeprefix("refs/heads/")
        rows.append(("branch", name, sha, int(date), subject, upstream))
    rows.extend(
        ("stash", name, sha, date, subject, "") for name, sha, date, subject in stashes(repo)
    )
    for kind, name, sha, date, subject, upstream in rows:
        item = Item(
            id=hashlib.sha256(f"{common}\0{kind}\0{name}\0{sha}".encode()).hexdigest()[:12],
            repo=str(repo),
            common_dir=common,
            kind=kind,
            name=name,
            oid=sha,
            age_days=max(0, int((timestamp - date) / 86400)),
            subject=subject,
        )
        if kind == "branch":
            item.base, item.base_oid = target, target_oid
            item.protected = (
                is_protected(name, protected)
                or target == f"refs/heads/{name}"
                or bool(git(repo, "symbolic-ref", "--quiet", f"refs/heads/{name}", check=False))
            )
            item.in_use = name in active
            item.upstream = upstream or None
            item.upstream_exists = bool(oid(repo, upstream)) if upstream else None
            if target_oid:
                item.unique_commits = int(git(repo, "rev-list", "--count", f"{target_oid}..{sha}"))
                item.merged = item.unique_commits == 0
                ancestor = git(repo, "merge-base", target_oid, sha, check=False)
                start = ancestor or target_oid
            else:
                start = f"{sha}^"
            item.commits = git(
                repo,
                "log",
                "-8",
                "--format=%s",
                f"{target_oid}..{sha}" if target_oid else sha,
                "--",
            ).splitlines()
        else:
            start = f"{sha}^1"
            item.commits = [subject]
        if oid(repo, start):
            item.files = (
                git(
                    repo,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-only",
                    "-z",
                    start,
                    sha,
                    "--",
                )
                .strip("\0")
                .split("\0")
            )
            item.files = [path for path in item.files if path]
            item.diff_stat = git(
                repo, "diff", "--no-ext-diff", "--no-textconv", "--shortstat", start, sha, "--"
            )
        if kind == "stash" and oid(repo, f"{sha}^3"):
            untracked = git(repo, "ls-tree", "-r", "--name-only", "-z", f"{sha}^3")
            item.files = sorted(set(item.files + untracked.strip("\0").split("\0")))
            item.diff_stat += " (also includes untracked files)"
        classify(item, stale_days)
        items.append(item)
    return items


def scan(
    roots: list[Path],
    *,
    base: str | None = None,
    protected: list[str] | None = None,
    stale_days: int = 90,
    max_depth: int = 6,
) -> Scan:
    patterns = list(dict.fromkeys(DEFAULT_PROTECTED + (protected or [])))
    repos, warnings = discover(roots, max_depth)
    items = []
    for repo in repos:
        try:
            found = scan_repo(repo, base, patterns, stale_days)
            if not found:
                warnings.append(f"{repo}: no branches or stashes (possibly an unborn repository)")
            elif any(i.kind == "branch" and i.base is None for i in found):
                warnings.append(f"{repo}: comparison branch unavailable; choose --base explicitly")
            items.extend(found)
        except (GitError, ValueError) as exc:
            warnings.append(f"{repo}: {exc}")
    return Scan(
        id=new_id(),
        created_at=now(),
        roots=[str(p.expanduser().resolve()) for p in roots],
        protected_patterns=patterns,
        stale_days=stale_days,
        items=sorted(items, key=priority),
        warnings=warnings,
    )
