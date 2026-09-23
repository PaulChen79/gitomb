import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

from dotenv import dotenv_values
from rich.console import Console
from rich.table import Table

from gitomb import __version__
from gitomb.assessment import evaluate
from gitomb.cleanup import clean, restore, validate
from gitomb.git import GitError, git
from gitomb.models import Batch, Item, Scan
from gitomb.scan import scan
from gitomb.storage import latest_scan, load, safe_id, save, state_dir

console = Console(markup=False, highlight=False)
err = Console(stderr=True, markup=False, highlight=False)


def display(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", value)


def table(items: list[Item]) -> None:
    result = Table("#", "ID", "Repository / item", "Age", "Action", "Evidence / Jev")
    for index, item in enumerate(items, 1):
        reasons = list(item.reasons)
        if item.assessment:
            ai = item.assessment
            probability = ai.probabilities.get(ai.purpose, 0)
            reasons.append(f"{ai.purpose}: {probability:.0%} (model probability)")
        if item.ai_error:
            reasons.append(f"Jev unavailable: {item.ai_error}; Git rules only")
        result.add_row(
            str(index),
            item.id,
            display(f"{Path(item.repo).name}\n{item.kind}: {item.name}"),
            f"{item.age_days}d",
            item.recommendation,
            display("\n".join(reasons)),
        )
    console.print(result)


def scan_path(args) -> Path:
    return (
        args.state_dir / "scans" / f"{safe_id(args.scan)}.json"
        if args.scan
        else latest_scan(args.state_dir)
    )


def selected(items: list[Item], identifiers: list[str]) -> list[Item]:
    result = []
    for identifier in identifiers:
        matches = [item for item in items if item.id.startswith(identifier)]
        if len(matches) != 1:
            raise ValueError(f"Item ID is missing or ambiguous: {identifier}")
        if matches[0] not in result:
            result.append(matches[0])
    return result


def run_scan(args) -> int:
    config = {}
    if args.config:
        config = tomllib.loads(args.config.read_text())
        unknown = set(config) - {"roots", "base", "protect", "stale_days", "max_depth", "model"}
        if unknown:
            raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    roots = args.roots or [Path(p) for p in config.get("roots", ["."])]
    base = args.base or config.get("base")
    patterns = config.get("protect", []) + args.protect
    stale_days = args.stale_days if args.stale_days is not None else config.get("stale_days", 90)
    max_depth = args.max_depth if args.max_depth is not None else config.get("max_depth", 6)
    if stale_days < 0 or max_depth < 0:
        raise ValueError("stale-days and max-depth must be nonnegative")
    key = (
        os.environ.get("TYPESAFE_API_KEY")
        or dotenv_values(args.env_file).get("TYPESAFE_API_KEY")
        or ""
    ).strip()
    if args.ai and not key:
        raise ValueError("--ai requires TYPESAFE_API_KEY in the environment")
    if args.include_diff and (args.no_ai or not key):
        raise ValueError("--include-diff requires enabled Jev and TYPESAFE_API_KEY")
    err.print("Scanning local Git refs (no fetch, checkout, or deletion)…")
    report = scan(roots, base=base, protected=patterns, stale_days=stale_days, max_depth=max_depth)
    path = args.state_dir / "scans" / f"{report.id}.json"
    save(path, report)
    if key and not args.no_ai:
        err.print(
            "Jev: sending branch/stash names, commit messages, file names and diff statistics."
            + (" Filtered patch excerpts enabled." if args.include_diff else " No patch bodies.")
        )
        evaluate(
            report,
            args.state_dir,
            api_key=key,
            model=args.model or config.get("model", "jev-latest"),
            include_diff=args.include_diff,
            workers=args.workers,
            refresh=args.refresh,
        )
        save(path, report)
    elif not args.no_ai:
        report.warnings.append("TYPESAFE_API_KEY is not set; using Git rules only")
        save(path, report)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        table(report.items)
        console.print(f"Scan: {report.id}\nSaved: {path}")
        for warning in report.warnings:
            err.print(display(f"Warning: {warning}"))
    return 0


def run_show(args) -> int:
    report = load(scan_path(args), Scan)
    if not args.item:
        table(report.items)
        return 0
    item = selected(report.items, [args.item])[0]
    table([item])
    console.print(display(f"Repository: {item.repo}\nCommit: {item.oid}\n{item.subject}"))
    console.print(display(item.diff_stat))
    if args.diff:
        if item.kind == "stash":
            patch = git(
                item.repo,
                "stash",
                "show",
                "--include-untracked",
                "--patch",
                "--no-ext-diff",
                "--no-textconv",
                item.oid,
            )
        elif item.base_oid:
            patch = git(
                item.repo,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                f"{item.base_oid}...{item.oid}",
                "--",
            )
        else:
            patch = git(
                item.repo,
                "show",
                "--format=fuller",
                "--no-ext-diff",
                "--no-textconv",
                item.oid,
                "--",
            )
        console.print(display(patch), soft_wrap=True)
    return 0


def run_clean(args) -> int:
    report = load(scan_path(args), Scan)
    if args.ids:
        items = selected(report.items, args.ids)
    else:
        if args.yes or not sys.stdin.isatty():
            raise ValueError("Use --ids with explicit item IDs for noninteractive cleanup")
        table(report.items)
        raw = input("Select row numbers (comma-separated; empty cancels): ").strip()
        if not raw:
            console.print("Cancelled.")
            return 0
        numbers = list(dict.fromkeys(int(part.strip()) for part in raw.split(",")))
        if any(n < 1 or n > len(report.items) for n in numbers):
            raise ValueError("Selection is outside the displayed rows")
        items = [report.items[n - 1] for n in numbers]
    if not items:
        raise ValueError("No items selected")

    for item in items:
        try:
            validate(item, report.protected_patterns, args.allow_unmerged)
        except (ValueError, GitError) as exc:
            raise ValueError(f"{item.name}: {exc}") from exc
    table(items)
    if args.dry_run:
        console.print("Dry run passed. No refs or stashes changed.")
        return 0
    console.print(
        "Each item will receive a recovery ref before deletion."
        " Avoid concurrent Git writes during cleanup."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            raise ValueError("Confirmation needs a terminal, or explicit --ids and --yes")
        if input(f"Type 'clean {len(items)}' to continue: ").strip() != f"clean {len(items)}":
            console.print("Cancelled.")
            return 0
    batch = clean(report, items, args.state_dir, allow_unmerged=args.allow_unmerged)
    for entry in batch.entries:
        console.print(
            display(
                f"{entry.item.name}: {entry.status}" + (f" — {entry.error}" if entry.error else "")
            )
        )
    console.print(
        f"Batch: {batch.id}\nRestore: gitomb --state-dir {args.state_dir} restore {batch.id}"
    )
    return 1 if any(entry.status != "deleted" for entry in batch.entries) else 0


def run_restore(args) -> int:
    batch = restore(args.state_dir, args.batch)
    for entry in batch.entries:
        console.print(
            display(
                f"{entry.item.name}: {entry.status}" + (f" — {entry.error}" if entry.error else "")
            )
        )
    return 1 if any(entry.status != "restored" for entry in batch.entries) else 0


def run_batches(args) -> int:
    for path in sorted((args.state_dir / "batches").glob("*.json"), reverse=True):
        batch = load(path, Batch)
        statuses = ", ".join(entry.status for entry in batch.entries)
        console.print(display(f"{batch.id}  {statuses}"))
    return 0


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 16:
        raise argparse.ArgumentTypeError("must be between 1 and 16")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Review and retire local Git branches and stashes.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument(
        "--state-dir", type=Path, default=state_dir(), help="scan/cache/recovery directory"
    )
    commands = root.add_subparsers(dest="command", required=True)
    scan_cmd = commands.add_parser(
        "scan", help="scan repositories and optionally evaluate with Jev"
    )
    scan_cmd.add_argument("roots", nargs="*", type=Path)
    scan_cmd.add_argument("--config", type=Path, help="explicit TOML config file")
    scan_cmd.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="API key file (default: .env in current directory)",
    )
    scan_cmd.add_argument("--base", help="comparison branch, e.g. develop or origin/main")
    scan_cmd.add_argument(
        "--protect", action="append", default=[], help="additional protected glob"
    )
    scan_cmd.add_argument("--stale-days", type=int)
    scan_cmd.add_argument("--max-depth", type=int)
    mode = scan_cmd.add_mutually_exclusive_group()
    mode.add_argument("--no-ai", action="store_true", help="local Git evidence only")
    mode.add_argument("--ai", action="store_true", help="require an API key")
    scan_cmd.add_argument(
        "--include-diff", action="store_true", help="send filtered patch excerpts"
    )
    scan_cmd.add_argument("--model", help="Jev model ID (default: jev-latest)")
    scan_cmd.add_argument("--workers", type=positive, default=4)
    scan_cmd.add_argument("--refresh", action="store_true", help="bypass local Jev cache")
    scan_cmd.add_argument("--json", action="store_true", help="emit JSON to stdout")
    scan_cmd.set_defaults(run=run_scan)
    show = commands.add_parser("show", help="inspect a saved scan or one item")
    show.add_argument("item", nargs="?", help="item ID (unique prefix accepted)")
    show.add_argument("--scan", help="scan ID (default: latest)")
    show.add_argument(
        "--diff", action="store_true", help="show local patch without sending it to Jev"
    )
    show.set_defaults(run=run_show)
    clean_cmd = commands.add_parser("clean", help="select, confirm, back up and delete")
    clean_cmd.add_argument("--scan", help="scan ID (default: latest)")
    clean_cmd.add_argument("--ids", nargs="+", help="explicit item IDs (unique prefixes accepted)")
    clean_cmd.add_argument(
        "--allow-unmerged", action="store_true", help="allow branches with unique work"
    )
    clean_cmd.add_argument("--dry-run", action="store_true")
    clean_cmd.add_argument("--yes", action="store_true", help="confirm explicitly selected items")
    clean_cmd.set_defaults(run=run_clean)
    restore_cmd = commands.add_parser("restore", help="restore one cleanup batch")
    restore_cmd.add_argument("batch")
    restore_cmd.set_defaults(run=run_restore)
    batches = commands.add_parser("batches", help="list cleanup batches")
    batches.set_defaults(run=run_batches)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.state_dir = args.state_dir.expanduser().resolve()
        return args.run(args)
    except (ValueError, OSError, GitError) as exc:
        err.print(display(f"Error: {exc}"))
        return 1
    except (KeyboardInterrupt, EOFError):
        err.print("Interrupted. Run gitomb batches to check any cleanup journal.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
