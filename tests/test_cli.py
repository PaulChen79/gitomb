import json
import os
import subprocess
import sys

from gitomb.git import git, oid


def cli(repo, directory, *args):
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "gitomb.cli", "--state-dir", str(directory), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_cli_scan_dry_run_explicit_cleanup_restore(repo, tmp_path):
    git(repo, "branch", "merged")
    directory = tmp_path / "state"
    result = cli(repo, directory, "scan", ".", "--no-ai", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    item = next(i for i in report["items"] if i["name"] == "merged")
    tip = item["oid"]
    result = cli(repo, directory, "show", item["id"], "--diff")
    assert result.returncode == 0, result.stderr
    result = cli(repo, directory, "clean", "--ids", item["id"], "--dry-run")
    assert result.returncode == 0, result.stderr
    assert oid(repo, "refs/heads/merged") == tip
    assert not (directory / "batches").exists()
    result = cli(repo, directory, "clean", "--yes")
    assert result.returncode == 1
    assert "explicit item IDs" in result.stderr
    result = cli(repo, directory, "clean", "--ids", item["id"], "--yes")
    assert result.returncode == 0, result.stderr
    assert not oid(repo, "refs/heads/merged")
    batch = next((directory / "batches").glob("*.json")).stem
    result = cli(repo, directory, "restore", batch)
    assert result.returncode == 0, result.stderr
    assert oid(repo, "refs/heads/merged") == tip
    assert cli(repo, directory, "batches").returncode == 0


def test_missing_key_and_path_traversal_rejected(repo, tmp_path):
    directory = tmp_path / "state"
    result = cli(repo, directory, "scan", "--ai")
    assert result.returncode == 1
    assert "TYPESAFE_API_KEY" in result.stderr
    result = cli(repo, directory, "show", "--scan", "../../secret")
    assert result.returncode == 1
    assert "Invalid scan" in result.stderr
