import fcntl
from contextlib import contextmanager
from pathlib import Path

from gitomb.git import common_dir, git, in_use_branches, oid, stashes
from gitomb.models import Batch, BatchEntry, Item, Scan
from gitomb.scan import DEFAULT_PROTECTED, is_protected, new_id, now
from gitomb.storage import load, safe_id, save


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another gitomb operation is running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate(item: Item, patterns: list[str], allow_unmerged: bool) -> str:
    if common_dir(item.repo) != item.common_dir:
        raise ValueError("Repository identity changed; scan again")
    if item.kind == "branch":
        ref = f"refs/heads/{item.name}"
        git(item.repo, "check-ref-format", ref)
        if git(item.repo, "symbolic-ref", "--quiet", ref, check=False):
            raise ValueError("Symbolic branch refs cannot be cleaned")
        if item.protected or is_protected(item.name, DEFAULT_PROTECTED + patterns):
            raise ValueError("Protected branch")
        if item.name in in_use_branches(item.repo):
            raise ValueError("Branch is checked out in a worktree")
        if oid(item.repo, ref) != item.oid:
            raise ValueError("Branch changed since scan; scan again")
        if item.base and oid(item.repo, item.base) != item.base_oid:
            raise ValueError("Comparison branch changed since scan; scan again")
        if not allow_unmerged:
            if not item.base or not item.base_oid:
                raise ValueError("No comparison branch; use --base in a new scan")
            unique = int(git(item.repo, "rev-list", "--count", f"{item.base_oid}..{item.oid}"))
            if unique:
                raise ValueError("Branch has unique commits; inspect it and use --allow-unmerged")
        return ref
    matches = [selector for selector, sha, _, _ in stashes(item.repo) if sha == item.oid]
    if len(matches) != 1:
        raise ValueError("Stash is missing or has duplicate object IDs; scan/review manually")
    return matches[0]


def clean(scan: Scan, items: list[Item], directory: Path, *, allow_unmerged: bool = False) -> Batch:
    batch_id = new_id()
    batch = Batch(
        id=batch_id,
        scan_id=scan.id,
        created_at=now(),
        protected_patterns=scan.protected_patterns,
        entries=[
            BatchEntry(item=item, backup_ref=f"refs/gitomb/{batch_id}/{item.id}") for item in items
        ],
    )
    journal = directory / "batches" / f"{batch_id}.json"
    with lock(directory / "operation.lock"):
        save(journal, batch)
        for entry in batch.entries:
            item = entry.item
            try:
                with lock(Path(common_dir(item.repo)) / "gitomb.lock"):
                    validate(item, batch.protected_patterns, allow_unmerged)

                    git(item.repo, "update-ref", "--no-deref", entry.backup_ref, item.oid, "")
                    entry.status = "backed-up"
                    save(journal, batch)
                    target = validate(item, batch.protected_patterns, allow_unmerged)
                    if item.kind == "branch":
                        git(item.repo, "update-ref", "--no-deref", "-d", target, item.oid)
                    else:
                        git(item.repo, "stash", "drop", target)
                    entry.status = "deleted"
            except Exception as exc:
                entry.status = "failed"
                entry.error = str(exc)
            save(journal, batch)
    return batch


def restore(directory: Path, batch_id: str) -> Batch:
    journal = directory / "batches" / f"{safe_id(batch_id)}.json"
    with lock(directory / "operation.lock"):
        batch = load(journal, Batch)
        for entry in batch.entries:
            if entry.status == "restored":
                continue
            item = entry.item
            try:
                if common_dir(item.repo) != item.common_dir:
                    raise ValueError("Repository identity changed")
                expected = f"refs/gitomb/{safe_id(batch.id)}/{safe_id(item.id)}"
                if entry.backup_ref != expected:
                    raise ValueError("Invalid backup ref in journal")
                with lock(Path(item.common_dir) / "gitomb.lock"):
                    if oid(item.repo, entry.backup_ref) != item.oid:
                        raise ValueError("Backup ref missing or changed; no restore attempted")
                    if item.kind == "branch":
                        ref = f"refs/heads/{item.name}"
                        git(item.repo, "check-ref-format", ref)
                        if git(item.repo, "symbolic-ref", "--quiet", ref, check=False):
                            raise ValueError("Branch name is now a symbolic ref; not overwritten")
                        current = oid(item.repo, ref)
                        if current and current != item.oid:
                            raise ValueError(
                                "Branch name now exists at another commit; not overwritten"
                            )
                        if not current:
                            if item.name in in_use_branches(item.repo):
                                raise ValueError("Branch name is in use by a worktree")
                            git(item.repo, "update-ref", "--no-deref", ref, item.oid, "")
                    elif not any(sha == item.oid for _, sha, _, _ in stashes(item.repo)):
                        git(item.repo, "stash", "store", "-m", item.subject, item.oid)
                    entry.status, entry.error = "restored", None
            except Exception as exc:
                entry.error = str(exc)
            save(journal, batch)
    return batch
