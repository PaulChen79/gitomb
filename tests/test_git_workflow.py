import pytest
from conftest import commit

import gitomb.cleanup as cleanup
from gitomb.cleanup import clean, restore, validate
from gitomb.git import discover, git, oid, stashes
from gitomb.models import Batch
from gitomb.scan import scan
from gitomb.storage import load


def find(report, name):
    return next(item for item in report.items if item.name == name)


def test_discovery_deduplicates_worktrees_and_overlapping_roots(repo, tmp_path):
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "active", str(worktree))
    repos, warnings = discover([tmp_path, repo], max_depth=6)
    assert len(repos) == 1
    assert not warnings
    report = scan([tmp_path])
    assert find(report, "active").in_use
    assert find(report, "main").protected
    with pytest.raises(ValueError, match="worktree"):
        validate(find(report, "active"), report.protected_patterns, True)


def test_unknown_base_and_squash_merge_are_not_cleanup_candidates(repo):
    git(repo, "checkout", "-b", "feature")
    commit(repo, "feature\n")
    git(repo, "checkout", "main")
    git(repo, "merge", "--squash", "feature")
    git(repo, "commit", "-m", "Squashed feature")
    report = scan([repo])
    feature = find(report, "feature")
    assert feature.merged is False
    assert feature.unique_commits == 1
    assert feature.recommendation == "review"
    missing = scan([repo], base="does-not-exist")
    assert find(missing, "feature").base is None
    with pytest.raises(ValueError, match="comparison branch"):
        validate(find(missing, "feature"), missing.protected_patterns, False)


def test_branch_round_trip_retains_objects_after_gc_and_is_idempotent(repo, tmp_path):
    git(repo, "checkout", "-b", "experiment")
    tip = commit(repo, "unique content\n")
    git(repo, "checkout", "main")
    report = scan([repo])
    item = find(report, "experiment")
    with pytest.raises(ValueError, match="unique commits"):
        validate(item, report.protected_patterns, False)
    directory = tmp_path / "state"
    batch = clean(report, [item], directory, allow_unmerged=True)
    assert batch.entries[0].status == "deleted"
    assert not oid(repo, "refs/heads/experiment")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "--prune=now")
    assert oid(repo, batch.entries[0].backup_ref) == tip
    assert restore(directory, batch.id).entries[0].status == "restored"
    assert oid(repo, "refs/heads/experiment") == tip
    assert restore(directory, batch.id).entries[0].status == "restored"


def test_stash_multiple_deletion_reindexes_and_restores_untracked_files(repo, tmp_path):
    (repo / "file.txt").write_text("first stash\n")
    (repo / "untracked.txt").write_text("must survive\n")
    git(repo, "stash", "push", "-u", "-m", "first")
    first_oid = oid(repo, "refs/stash")
    (repo / "file.txt").write_text("second stash\n")
    git(repo, "stash", "push", "-m", "second")
    report = scan([repo])
    items = sorted([i for i in report.items if i.kind == "stash"], key=lambda i: i.name)
    assert "untracked.txt" in next(i for i in items if i.oid == first_oid).files

    (repo / "file.txt").write_text("keep newer stash\n")
    git(repo, "stash", "push", "-m", "newer")
    newer_oid = oid(repo, "refs/stash")
    directory = tmp_path / "state"
    batch = clean(report, items, directory)
    assert all(entry.status == "deleted" for entry in batch.entries)
    assert [sha for _, sha, _, _ in stashes(repo)] == [newer_oid]
    restored = restore(directory, batch.id)
    assert all(entry.status == "restored" for entry in restored.entries)
    assert len(stashes(repo)) == 3
    restore(directory, batch.id)
    assert len(stashes(repo)) == 3
    git(repo, "stash", "apply", first_oid)
    assert (repo / "untracked.txt").read_text() == "must survive\n"
    assert (repo / "file.txt").read_text() == "first stash\n"


def test_changed_tip_base_and_new_worktree_block_cleanup(repo, tmp_path):
    git(repo, "branch", "old")
    report = scan([repo])
    item = find(report, "old")
    commit(repo, "new main\n")
    with pytest.raises(ValueError, match="Comparison branch changed"):
        validate(item, report.protected_patterns, False)
    git(repo, "branch", "-f", "old", "main")
    with pytest.raises(ValueError, match="Branch changed"):
        validate(item, report.protected_patterns, False)
    report = scan([repo])
    item = find(report, "old")
    git(repo, "worktree", "add", str(tmp_path / "linked"), "old")
    batch = clean(report, [item], tmp_path / "state")
    assert batch.entries[0].status == "failed"
    assert oid(repo, "refs/heads/old")


def test_compare_and_delete_rejects_tip_race(repo, tmp_path, monkeypatch):
    git(repo, "branch", "old")
    report = scan([repo])
    item = find(report, "old")
    git(repo, "checkout", "-b", "other")
    newer = commit(repo, "other\n")
    git(repo, "checkout", "main")
    original_git = cleanup.git

    def racing_git(path, *args, **kwargs):
        if args[:3] == ("update-ref", "--no-deref", "-d"):
            original_git(path, "update-ref", "refs/heads/old", newer)
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(cleanup, "git", racing_git)
    batch = clean(report, [item], tmp_path / "state")
    assert batch.entries[0].status == "failed"
    assert oid(repo, "refs/heads/old") == newer


def test_restore_wont_overwrite_reused_branch_name(repo, tmp_path):
    git(repo, "branch", "old")
    report = scan([repo])
    directory = tmp_path / "state"
    batch = clean(report, [find(report, "old")], directory)
    newer = commit(repo, "new main\n")
    git(repo, "branch", "old")
    restored = restore(directory, batch.id)
    assert "not overwritten" in restored.entries[0].error
    assert oid(repo, "refs/heads/old") == newer


def test_interruption_after_delete_can_restore_from_durable_journal(repo, tmp_path, monkeypatch):
    git(repo, "branch", "old")
    report = scan([repo])
    directory = tmp_path / "state"
    original_git = cleanup.git

    def interrupted_git(path, *args, **kwargs):
        result = original_git(path, *args, **kwargs)
        if args[:3] == ("update-ref", "--no-deref", "-d"):
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(cleanup, "git", interrupted_git)
    with pytest.raises(KeyboardInterrupt):
        clean(report, [find(report, "old")], directory)
    journal = next((directory / "batches").glob("*.json"))
    batch = load(journal, Batch)
    assert batch.entries[0].status == "backed-up"
    assert not oid(repo, "refs/heads/old")
    assert restore(directory, batch.id).entries[0].status == "restored"


def test_protection_cannot_be_bypassed_with_allow_unmerged(repo, tmp_path):
    git(repo, "branch", "release/v1")
    report = scan([repo], protected=["custom/*"])
    batch = clean(report, [find(report, "release/v1")], tmp_path / "state", allow_unmerged=True)
    assert batch.entries[0].status == "failed"
    assert "Protected" in batch.entries[0].error


def test_duplicate_stash_oid_is_rejected(repo):
    (repo / "file.txt").write_text("stash\n")
    git(repo, "stash", "push", "-m", "one")
    sha = oid(repo, "refs/stash")
    (repo / "file.txt").write_text("another stash\n")
    git(repo, "stash", "push", "-m", "two")
    git(repo, "stash", "store", "-m", "duplicate", sha)
    report = scan([repo])
    item = next(i for i in report.items if i.kind == "stash" and i.oid == sha)
    with pytest.raises(ValueError, match="duplicate"):
        validate(item, report.protected_patterns, False)


def test_branch_name_colliding_with_tag_and_symbolic_refs(repo, tmp_path):
    git(repo, "branch", "topic")
    git(repo, "tag", "topic")
    git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    report = scan([repo])
    item = find(report, "topic")
    alias = find(report, "alias")
    assert alias.protected
    with pytest.raises(ValueError, match="Symbolic"):
        validate(alias, report.protected_patterns, True)
    batch = clean(report, [item], tmp_path / "state")
    assert batch.entries[0].status == "deleted"
    assert oid(repo, "refs/tags/topic")
    assert oid(repo, "refs/heads/main")
    assert not oid(repo, "refs/heads/topic")
