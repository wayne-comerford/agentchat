"""
agentchat v1.2 — `agentchat-sync` CLI entry point.

Subcommands:
    push        Build the mirror tree, scrub it, commit, push to GitHub.
    status      Show what would be pushed, without pushing.
    init        One-time setup: write a sample sync config and audit.
    doctor      Sanity-check the local workspace + remote + git binary.
    audit tail  Print the most recent N audit entries (default 20).
    audit show  Print a single audit entry by timestamp.

Stdlib only. No third-party deps.

Usage:
    agentchat-sync push [--remote <url>] [--workspace <slug>] [--message <msg>] [--dry-run]
    agentchat-sync status [--workspace <slug>]
    agentchat-sync init
    agentchat-sync doctor
    agentchat-sync audit {tail|show} [...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from agentchat import sync_github as sg


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentchat-sync",
        description=(
            "agentchat GitHub sync — one-shot mirror of memory + scrubbed config "
            "to a per-workspace GitHub repo. Stdlib only."
        ),
    )
    p.add_argument(
        "--workspace",
        default=None,
        help=(
            "Workspace slug (default: derived from HERMES_HOME basename, or 'default' "
            "if that doesn't look like a slug)."
        ),
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_push = sub.add_parser("push", help="Build + scrub + commit + push to GitHub.")
    sp_push.add_argument(
        "--remote",
        default=None,
        help=(
            "Git remote URL (default: read from $AGENTCHAT_SYNC_REMOTE, else built from "
            "DEFAULT_REMOTE_TEMPLATE with workspace_slug)."
        ),
    )
    sp_push.add_argument("--message", "-m", default=None, help="Commit message override.")
    sp_push.add_argument(
        "--author-name", default="agentchat-sync", help="git author.name (default: agentchat-sync)."
    )
    sp_push.add_argument(
        "--author-email", default="agentchat-sync@localhost", help="git author.email."
    )
    sp_push.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the mirror tree, scrub it, but don't commit or push.",
    )
    sp_push.set_defaults(func=cmd_push)

    sp_status = sub.add_parser("status", help="Show what would be pushed, without pushing.")
    sp_status.add_argument("--json", action="store_true", help="Print as JSON.")
    sp_status.set_defaults(func=cmd_status)

    sp_init = sub.add_parser("init", help="One-time setup: write audit + sample config.")
    sp_init.add_argument(
        "--remote", default=None, help="Override the sample remote URL written to config.example.json."
    )
    sp_init.set_defaults(func=cmd_init)

    sp_doctor = sub.add_parser("doctor", help="Sanity-check workspace + remote + git.")
    sp_doctor.set_defaults(func=cmd_doctor)

    sp_audit = sub.add_parser("audit", help="Read the local audit log.")
    audit_sub = sp_audit.add_subparsers(dest="audit_cmd", required=True)
    sp_audit_tail = audit_sub.add_parser("tail", help="Print the most recent N entries.")
    sp_audit_tail.add_argument("-n", type=int, default=20, help="Number of entries (default 20).")
    sp_audit_tail.set_defaults(func=cmd_audit_tail)
    sp_audit_show = audit_sub.add_parser("show", help="Show a single entry by timestamp.")
    sp_audit_show.add_argument("ts", help="Timestamp substring to match (e.g. 2026-08-18T12:34:56).")
    sp_audit_show.set_defaults(func=cmd_audit_show)

    return p


def _workspace_slug(args: argparse.Namespace) -> str:
    if args.workspace:
        return args.workspace
    env = sg.HERMES_HOME.name
    # Slug-friendly normalization: lowercase, replace non-alnum with -
    slug = re.sub(r"[^a-z0-9-]+", "-", env.lower()).strip("-") or "default"
    return slug


def _resolve_remote(args: argparse.Namespace, workspace_slug: str) -> str:
    if args.remote:
        return args.remote
    env_remote = os.environ.get("AGENTCHAT_SYNC_REMOTE")
    if env_remote:
        return env_remote
    owner = "wayne-comerford"  # default owner; override via --remote or env
    repo = f"agentchat-mirror-{workspace_slug}"
    return sg.DEFAULT_REMOTE_TEMPLATE.format(owner=owner, repo=repo)


def cmd_push(args: argparse.Namespace) -> int:
    workspace_slug = _workspace_slug(args)
    remote = _resolve_remote(args, workspace_slug)
    try:
        result = sg.push(
            workspace_slug=workspace_slug,
            remote=remote,
            commit_message=args.message,
            author_name=args.author_name,
            author_email=args.author_email,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"PUSH FAILED: {exc}", file=sys.stderr)
        return 2
    print(sg.format_result(result))
    return 0 if result.pushed or args.dry_run else 1


def cmd_status(args: argparse.Namespace) -> int:
    workspace_slug = _workspace_slug(args)
    d = sg.status(workspace_slug=workspace_slug)
    if args.json:
        print(json.dumps(d, indent=2, sort_keys=True))
    else:
        print(sg.format_status(d))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    workspace_slug = _workspace_slug(args)
    sg.SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    audit_log = sg.AUDIT_LOG
    if not audit_log.exists():
        audit_log.touch()
    sample = sg.SYNC_ROOT / "config.example.json"
    sample.write_text(
        json.dumps(
            {
                "workspace_slug": workspace_slug,
                "remote": _resolve_remote(args, workspace_slug),
                "author": {"name": "agentchat-sync", "email": "agentchat-sync@localhost"},
                "exclude_extra": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        textwrap.dedent(
            f"""
            Initialized agentchat sync state under {sg.SYNC_ROOT}.

            Next steps:
              1. Verify the remote exists on GitHub (agentchat-mirror-{workspace_slug}).
                 It must be empty (or empty-repo) — the sync agent pushes a single
                 `main` branch.
              2. Make sure your SSH key is added to GitHub:
                   ssh -T git@github.com
              3. Try a dry run:
                   agentchat-sync push --dry-run
              4. If that looks right:
                   agentchat-sync push
            """
        ).strip()
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    workspace_slug = _workspace_slug(args)
    issues: list[str] = []
    ok: list[str] = []

    # 1. HERMES_HOME exists and is readable.
    if not sg.HERMES_HOME.exists():
        issues.append(f"HERMES_HOME missing: {sg.HERMES_HOME}")
    else:
        ok.append(f"HERMES_HOME present: {sg.HERMES_HOME}")

    # 2. Memory root present.
    if not sg.MEMORY_ROOT.exists():
        issues.append(f"memory root missing: {sg.MEMORY_ROOT}")
    else:
        agents = list((sg.MEMORY_ROOT / "agents").iterdir()) if (sg.MEMORY_ROOT / "agents").exists() else []
        ok.append(f"memory root: {sg.MEMORY_ROOT} ({len(agents)} agents)")

    # 3. git installed.
    g = shutil.which("git")
    if g is None:
        issues.append("git is not installed or not on PATH")
    else:
        ok.append(f"git: {g}")

    # 4. SYNC_ROOT writable.
    try:
        sg.SYNC_ROOT.mkdir(parents=True, exist_ok=True)
        (sg.SYNC_ROOT / ".write-test").write_text("ok", encoding="utf-8")
        (sg.SYNC_ROOT / ".write-test").unlink()
        ok.append(f"sync root writable: {sg.SYNC_ROOT}")
    except OSError as exc:
        issues.append(f"sync root not writable ({exc}): {sg.SYNC_ROOT}")

    # 5. SSH to GitHub (best-effort).
    if g is not None:
        probe = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "git@github.com"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # github prints "Hi <user>!..." on stderr; success exits with 1 but
        # the greeting is the actual signal. Treat any greeting as ok.
        if "successfully authenticated" in (probe.stderr or "").lower():
            ok.append("GitHub SSH: authenticated")
        else:
            issues.append(f"GitHub SSH probe failed: rc={probe.returncode} stderr={probe.stderr.strip()[:200]}")

    # 6. gh CLI token status (informational only — sync does not require it).
    gh = shutil.which("gh")
    if gh is not None:
        probe = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=10)
        if probe.returncode == 0:
            ok.append("gh CLI: authenticated (PR review flow is unlocked if you need it)")
        else:
            ok.append(
                f"gh CLI: not authenticated (rc={probe.returncode}). "
                f"v1.2.0.dev20 does not need it; required for PR review flow in dev21+."
            )
    else:
        ok.append("gh CLI: not installed (optional)")

    # Print report.
    print(f"agentchat-sync doctor — workspace '{workspace_slug}'")
    print(f"HERMES_HOME: {sg.HERMES_HOME}")
    print()
    print("OK:")
    for line in ok:
        print(f"  ✓ {line}")
    if issues:
        print()
        print("ISSUES:")
        for line in issues:
            print(f"  ✗ {line}")
        return 1
    return 0


def cmd_audit_tail(args: argparse.Namespace) -> int:
    if not sg.AUDIT_LOG.exists():
        print(f"no audit log yet: {sg.AUDIT_LOG}")
        return 0
    lines = sg.AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.n :]:
        try:
            d = json.loads(line)
        except ValueError:
            print(line)
            continue
        print(json.dumps(d, sort_keys=True))
    return 0


def cmd_audit_show(args: argparse.Namespace) -> int:
    if not sg.AUDIT_LOG.exists():
        print(f"no audit log yet: {sg.AUDIT_LOG}")
        return 1
    for line in sg.AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        if args.ts in line:
            try:
                d = json.loads(line)
                print(json.dumps(d, indent=2, sort_keys=True))
                return 0
            except ValueError:
                print(line)
                return 0
    print(f"no audit entry matches: {args.ts}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
