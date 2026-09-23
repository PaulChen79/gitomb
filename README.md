# gitomb

Turn forgotten Git branches and stashes into a cleanup list you can review.

gitomb scans local repositories, combines Git evidence with optional [TypeSafe Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) assessments, and helps you decide what to keep. You select the items to remove. gitomb backs them up before deletion and can restore them by batch.

No API key is required for Git-only scans.

| Command | Purpose | Changes Git refs? |
| --- | --- | --- |
| `gitomb scan` | Scan repositories and save a report | No |
| `gitomb show` | Inspect a saved report or a local diff | No |
| `gitomb clean` | Select, confirm, back up, and remove items | Yes, unless `--dry-run` |
| `gitomb batches` | List cleanup batches | No |
| `gitomb restore` | Restore a cleanup batch | Yes |

## Quick start

Requires **Python 3.11+, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and macOS or Linux**. Windows is not supported yet because cleanup uses POSIX file locks.

### 1. Install

```bash
uv tool install gitomb
gitomb --help
gitomb scan /path/to/your/repo --no-ai
```

Replace `/path/to/your/repo` with a repository you want to inspect. If your shell cannot find `gitomb`, run `uv tool update-shell` and restart the terminal.

To upgrade, run `uv tool upgrade gitomb`. To uninstall, run `uv tool uninstall gitomb`.

Scanning does not delete, fetch, or check out anything. It saves a report for subsequent `show` and `clean` commands.

### 2. Enable Jev assessments (optional)

Get an API key from [TypeSafe](https://console.typesafe.ai/). Create or edit `.env` in the directory where you will run gitomb:

```dotenv
TYPESAFE_API_KEY=your-api-key
```

Then scan with Jev:

```bash
gitomb scan /path/to/your/repo --ai
```

An existing `TYPESAFE_API_KEY` environment variable takes precedence over `.env`. The default `.env` is read from the **directory where you run the command**, not from each scanned repository. Use `--env-file /path/to/.env` for a different location.

Keep `.env` out of version control. The gitomb source repository already ignores it; configure your own repository accordingly. Do not commit your key. Jev calls use your own TypeSafe account quota.

| Mode | Behavior |
| --- | --- |
| Default | Use Jev when a key is available; otherwise use Git rules |
| `--no-ai` | Use local Git evidence only; make no Jev requests |
| `--ai` | Require a key; individual API failures still fall back to Git rules |

### Install from source instead

```bash
git clone https://github.com/PaulChen79/gitomb.git
cd gitomb
uv sync --locked
uv tool install .
gitomb scan /path/to/your/repo --env-file /path/to/gitomb/.env
```

After updating the source checkout, reinstall with `uv tool install --reinstall .`. The examples below use the standalone `gitomb` command.

## Usage

### Scan one or more repositories

```bash
gitomb scan ~/projects ~/work --no-ai
gitomb scan /path/to/repo --base develop
gitomb scan /path/to/repo --ai --include-diff
```

Without a path, gitomb scans the current directory. Discovery searches six directory levels by default, deduplicates linked worktrees, skips directories such as `.git`, `.venv`, and `node_modules`, and does not follow directory symlinks.

Example report, simplified for readability. IDs and data are illustrative:

```text
#  ID            Repository / item           Age   Action             Evidence / Jev
1  a1b2c3d4e5f6  billing / branch: fix/log    90d   cleanup-candidate  merged; no unique commits
2  b2c3d4e5f6a1  portal / branch: experiment  120d  review             3 unique commits
3  c3d4e5f6a1b2  infra / stash: stash@{0}     45d   review             stash contents require review

Scan: 20260923T080000Z-abcd1234
Saved: .../gitomb/scans/20260923T080000Z-abcd1234.json
```

### Review the report and diffs

```bash
gitomb show
gitomb show ITEM_ID --diff
gitomb show ITEM_ID --scan SCAN_ID --diff
```

Replace `ITEM_ID` with an ID from the report; unique prefixes are accepted. `show --diff` displays the full diff locally without sending it to Jev.

`show` and `clean` use the **latest saved scan**, not a fresh scan of the current directory. Specify `--scan SCAN_ID` to use a particular report.

### Select and confirm cleanup

```bash
gitomb clean
```

Enter comma-separated **row numbers**, such as `1,3`. Review the selected items, then type `clean 2` to confirm two deletions. An empty selection cancels. Protected branches and branches in use by a worktree cannot be removed.

For explicit selection, use item IDs. Preview the operation first:

```bash
gitomb clean --ids ITEM_ID ANOTHER_ID --dry-run
gitomb clean --ids ITEM_ID ANOTHER_ID
```

Branches with commits not reachable from the comparison branch require an additional flag:

```bash
gitomb clean --ids ITEM_ID --allow-unmerged
```

`--allow-unmerged` does not bypass branch protection, worktree checks, or confirmation. Stashes always require your review, but do not use the branch-specific `--allow-unmerged` flag.

`--dry-run` validates eligibility without creating backups or deleting anything. Actual cleanup checks the state again. Noninteractive cleanup requires both explicit `--ids` and `--yes`.

### Restore a batch

```bash
gitomb batches
gitomb restore BATCH_ID
```

Use the batch ID printed by `clean`. Restoration recreates branches and adds stashes back to the stash list. It does **not** apply stashes to your working tree.

Interrupted or partially successful operations also appear in `batches`. Use the same batch ID to restore any items with valid backups.

## How it works

```text
Local repositories
        |
        v
Git evidence + optional Jev assessments
        |
        v
Saved report -> Your selection -> State checks -> Backup -> Cleanup
                                                    |
                                                    v
                                              Batch restoration
```

Git provides facts: commit age, ancestry, unique commits, upstream refs, worktree usage, file names, and diff statistics. Jev handles narrower semantic questions about the changes:

| Question | Primitive | Result |
| --- | --- | --- |
| What is the purpose of this work? | Choice | `temporary-debug`, `experiment`, `feature-or-fix`, `maintenance`, or `unknown` |
| Does the evidence show unfinished work? | Noul | Probability from 0 to 1 |
| Is the context insufficient to determine the purpose? | Noul | Probability from 0 to 1 |

The application combines these answers with Git rules. Jev can influence review order and suggest keeping unfinished work; it never authorizes deletion.

| Recommendation | Meaning |
| --- | --- |
| `cleanup-candidate` | Git confirms the branch is merged into the comparison branch with no unique commits; selection is still manual |
| `review` | Unique work, a stash, or evidence that needs your judgment |
| `keep` | Protected or checked-out branch, or model evidence of unfinished work |

Percentages describe **model probabilities about purpose**, not deletion safety or the author's intent to abandon work. Confidence is stored separately in the report JSON. Thresholds are initial policy choices, not a calibration established on your repositories.

The comparison branch defaults to the locally recorded `origin/HEAD`, then local `main`, `master`, or `develop`. Override it with `--base`. Missing comparison branches are not treated as proof of a merge. Remote-tracking refs may be stale because gitomb does not fetch.

Squash merges are not considered proven ancestry merges. Age is time since the last commit, not the last checkout, edit, or review.

## Data sent to Jev

When enabled, gitomb assesses items that are not protected, checked out, or already proven merged. Each item gets one request containing all three questions. Requests run with bounded concurrency, four at a time by default.

By default, requests contain names, commit messages, age, Git evidence, file names, and diff statistics. gitomb does not add the repository's absolute path or patch bodies to the request.

`--include-diff` additionally sends filtered excerpts from up to 20 files, capped at 12,000 characters. Common sensitive paths are excluded and recognizable credential patterns are redacted. **This is best-effort filtering, not a guarantee that all secrets are detected.** Names, messages, paths, and source code can contain private information; choose the appropriate mode for your repositories.

Untracked stash files are listed, but their contents are not currently included in Jev patch excerpts. `show --diff` is local only.

Assessments are cached by model name and input. Use `--refresh` to bypass the cache, including after the `jev-latest` alias changes, or use `--model` to select a specific version. Reports record the actual model returned by the API. API failures appear beside the affected item, with Git rules still available.

## Configuration

```bash
gitomb scan --config gitomb.example.toml
gitomb scan ~/projects --no-ai --json > scan.json
gitomb --state-dir .gitomb scan /path/to/repo --no-ai
gitomb --state-dir .gitomb clean
```

Configuration files are loaded only when supplied through `--config`. See [gitomb.example.toml](https://github.com/PaulChen79/gitomb/blob/main/gitomb.example.toml).

| Setting / flag | Default | Meaning |
| --- | --- | --- |
| `roots` / positional paths | Current directory | Discovery roots |
| `base` / `--base` | Automatic | Comparison branch for this scan |
| `protect` / `--protect` | Built-in patterns | Additional protected globs; quote `*`; the flag can be repeated |
| `stale_days` / `--stale-days` | `90` | Age annotation threshold, not a deletion threshold |
| `max_depth` / `--max-depth` | `6` | Discovery depth; `0` checks only the roots |
| `model` / `--model` | `jev-latest` | Model name or version |
| `--workers` | `4` | Concurrent Jev requests, from `1` to `16`; CLI only |
| `--refresh` | Off | Bypass the Jev cache; CLI only |
| `--env-file` | `.env` in the current directory | Key file; the environment variable takes precedence |
| `--include-diff` | Off | Send filtered patch excerpts; requires Jev and a key |
| `--json` | Off | Report JSON on stdout; progress on stderr |
| `--state-dir` | See below | Global option; place it before the subcommand |

CLI arguments override configuration values; protected patterns are combined. Use separate scans and scan IDs when repositories need different comparison branches.

State lives in `$XDG_STATE_HOME/gitomb`, or `~/.local/state/gitomb` when unset:

| Directory | Contents |
| --- | --- |
| `scans/` | Saved evidence and assessments |
| `cache/` | Reusable Jev assessments |
| `batches/` | Cleanup and recovery journals |

Data files are created with owner-only read/write permissions. Keep both the journals and the original repositories' `.git` directories for recovery.

## Recovery behavior and limitations

- `main`, `master`, `develop`, `development`, `release`, and `release/*` are protected, along with a scan's local comparison branch and symbolic branch refs. Add patterns with `--protect`.
- Cleanup rechecks branch tips, comparison refs, and worktrees. Branch deletion compares the expected object ID before removing the ref.
- Before deleting each item, gitomb creates `refs/gitomb/<batch>/<item>` and persists a journal. These refs keep reachable Git objects alive through garbage collection, including stash index and untracked-file parents.
- Each stash is located again by object ID before deletion. Ambiguous duplicate object IDs are rejected.
- gitomb operations on the same repository are locked against each other. External Git commands do not share this lock. Avoid concurrent checkouts, worktree changes, or stash push/drop operations during cleanup.
- Batches can partially succeed. Failures are reported per item; operations across repositories are not atomic.
- Restoration will not overwrite a branch name now pointing to a different commit. Restored stash positions can change. Retrying a restored batch does not duplicate its stashes.
- Branch tips and reachable contents are retained; old branch reflog history is not. Local branch configuration is left in place for restoration.
- Backup refs remain after restoration. There is no automatic backup pruning, so cleanup may not reclaim disk space.
- gitomb does not delete remote branches, push changes, or discover bare repositories.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| Key not found | Check the current directory or use `--env-file`; use `--no-ai` for local-only operation |
| `Jev unavailable` | Check the reported error class, key, network, or quota, then rescan with `--refresh` |
| No comparison branch | Set `scan --base develop` or another explicit branch |
| Branch or comparison branch changed | Scan again and review the new result before cleaning |
| Branch checked out in a worktree | Keep it; gitomb will not switch or remove worktrees for you |
| Squash-merged branch remains `review` | Inspect its diff; ancestry alone does not prove the merge |
| Restore reports a conflicting branch name | Resolve the name conflict, then retry the same batch |

## Development

Clone the repository and run these commands from the checkout. To run source changes without reinstalling the standalone tool, use `uv run gitomb ...`.

```bash
uv sync --locked
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

Tests use temporary Git repositories for cleanup, restoration, untracked stash contents, garbage collection, worktrees, changed state, interrupted operations, and name conflicts. SDK contract tests use the real TypeSafe SDK with a mock HTTP transport. They do not require an API key or spend API credits.

## Contributing

**Contributions are welcome!** Bug reports, feature ideas, documentation fixes, usability feedback, and pull requests all help.

Use [Issues](https://github.com/PaulChen79/gitomb/issues) to report a problem or discuss an idea. Include your OS, Python/Git/gitomb versions, reproduction steps, expected behavior, and sanitized output.

For a pull request, fork the repository, create a focused branch from `main`, make your change, and run the checks above. Small fixes can go straight to a PR; discuss larger behavior or design changes in an issue first. Explain the problem, resulting behavior, and validation in the PR description.

Useful areas to contribute include terminal interaction, squash-merge evidence, Jev assessment quality, platform support, and examples. Please keep each PR focused and include behavior tests for changes to cleanup or recovery.

## License

Licensed under the [MIT License](https://github.com/PaulChen79/gitomb/blob/main/LICENSE).
