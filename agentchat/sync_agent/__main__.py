"""
agentchat-sync-stage — runnable entry point for the sync agent's
change-detection + local commit stage.

Subcommands:

    once      Run one poll cycle, commit any changes, exit.
    watch     Run forever, committing changes as they accumulate.
    status    Print the current change set without committing.

Stdlib only. No third-party runtime deps.

This CLI does **not** push — that is delivered in t_11537e05. The
separation is intentional: the dev20 push stage needs an SSH
deploy-key (or PAT) and a network round-trip; the commit stage does
not. Running the commit stage on its own is useful for local testing
and for CI verification.

Usage examples:

    # One-shot: detect + commit any pending changes.
    agentchat-sync-stage once \\
        --repo /home/waynec/agentchat \\
        --root /home/waynec/agentchat \\
        --root ~/.hermes/memory/agents \\
        --root ~/.hermes/memory/team

    # Daemon mode (will be replaced by the full orchestrator in
    # t_0105ff20; for now it just exercises the watch loop).
    agentchat-sync-stage watch --repo ... --root ...

    # Just look at what would be committed.
    agentchat-sync-stage status --repo ...
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from .commit import ChangeSet, CommitStage, build_commit_message, collect_changes
from .config import SyncConfig
from .watcher import PollingEmitter, watch_and_commit


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentchat-sync-stage",
        description=(
            "agentchat sync agent — change detection + local commit. "
            "Does not push (see sync_agent.push for that)."
        ),
    )
    p.add_argument(
        "--repo",
        required=True,
        help="Path to the git working tree (the repo to commit into).",
    )
    p.add_argument(
        "--root",
        action="append",
        default=[],
        help="Watched filesystem root (repeatable). Defaults to --repo if none given.",
    )
    p.add_argument(
        "--debounce-seconds",
        type=float,
        default=5.0,
        help="Coalesce bursts of writes within this many seconds (default: 5.0).",
    )
    p.add_argument(
        "--author",
        default=None,
        help="Override git author (e.g. 'agentchat-sync <agentchat-sync@localhost>').",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("once", help="One poll cycle, then exit.")
    sub.add_parser("watch", help="Run forever; commit on each debounced change.")
    sp_status = sub.add_parser("status", help="Show the change set, do not commit.")
    sp_status.add_argument("--json", action="store_true", help="Print the ChangeSet as JSON.")
    return p


def _parse_author(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    if "<" not in raw or ">" not in raw:
        raise SystemExit(f"--author must be in 'Name <email>' form, got: {raw!r}")
    name, _, email = raw.partition("<")
    return name.strip(), email.rstrip(">")


def _format_change_set(cs: ChangeSet) -> str:
    if cs.is_empty():
        return "(no changes)"
    lines = [cs.summary_line()]
    for r in cs.records:
        if r.old_path:
            lines.append(f"  {r.kind:8s} {r.old_path} -> {r.path}")
        else:
            lines.append(f"  {r.kind:8s} {r.path}")
    return "\n".join(lines)


def cmd_once(args: argparse.Namespace, config: SyncConfig) -> int:
    result = watch_and_commit(config, once=True)
    if result is None:
        print("no changes")
        return 0
    if result.committed:
        print(f"committed {result.sha[:8] if result.sha else '?'}:")
        print(_format_change_set(result.change_set))
        return 0
    print("no commit produced:")
    print(_format_change_set(result.change_set))
    return 1


def cmd_watch(args: argparse.Namespace, config: SyncConfig) -> int:
    watch_and_commit(config, once=False)
    return 0


def cmd_status(args: argparse.Namespace, config: SyncConfig) -> int:
    cs = collect_changes(config.repo_dir)
    if args.json:
        import json

        print(json.dumps(
            {
                "repo_dir": str(config.repo_dir),
                "watched_roots": [str(r) for r in config.watched_roots],
                "summary_line": cs.summary_line(),
                "records": [
                    {
                        "path": r.path,
                        "kind": r.kind,
                        "old_path": r.old_path,
                    }
                    for r in cs.records
                ],
                "origin": cs.origin,
            },
            indent=2,
            sort_keys=True,
        ))
        return 0
    print(f"repo: {config.repo_dir}")
    print(f"watched: {[str(r) for r in config.watched_roots]}")
    print(_format_change_set(cs))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"not a git repo: {repo}", file=sys.stderr)
        return 2

    roots = [Path(r).expanduser().resolve() for r in args.root] if args.root else [repo]
    author_name, author_email = _parse_author(args.author)

    config = SyncConfig(
        repo_dir=repo,
        watched_roots=tuple(roots),
        debounce_seconds=args.debounce_seconds,
        author_name=author_name,
        author_email=author_email,
    )

    if args.cmd == "once":
        return cmd_once(args, config)
    if args.cmd == "watch":
        return cmd_watch(args, config)
    if args.cmd == "status":
        return cmd_status(args, config)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())