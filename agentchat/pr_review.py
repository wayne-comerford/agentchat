"""
agentchat.pr_review — GitHub PR review flow (v1.2.0.dev25).

Two-way bridge between agentchat (local SQLite + CLI) and GitHub PR
review via the ``gh`` CLI. Standard scope: list PRs, view diff, post
threaded comments, receive webhook events.

NOT in scope (deferred to keep tight):
- Inline code suggestions
- PR templates / boilerplate
- CI status integration
- Slack/Discord notifications

Local DB: ~/.hermes/agent_chat/pr_reviews.db (created on first use).
Schema: review_sessions, review_comments, webhook_events.

Public API (callable from CLI, web UI, or external):
    list_open_prs(repo) -> list[dict]
    get_pr(repo, number) -> dict (head + base + body + diff)
    list_comments(repo, number) -> list[dict]
    post_comment(repo, number, body, *, path=None, line=None,
                 in_reply_to=None, agent=None) -> dict
    record_webhook(event_type, payload) -> int (event_id)
    list_recent_webhook_events(limit=50) -> list[dict]

Comments support threading via ``in_reply_to`` (a comment id from
GitHub). Local copies are kept in SQLite for offline review and audit.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

DB_PATH = Path(
    os.environ.get(
        "AGENTCHAT_PR_REVIEW_DB",
        str(Path.home() / ".hermes" / "agent_chat" / "pr_reviews.db"),
    )
)
DEFAULT_REPO = os.environ.get("AGENTCHAT_PR_REVIEW_REPO", "wayne-comerford/agentchat")
GH_BIN = os.environ.get("AGENTCHAT_GH_BIN") or shutil.which("gh") or "gh"


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_sessions (
    repo        TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    title       TEXT,
    state       TEXT,
    head_sha    TEXT,
    PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS review_comments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    gh_comment_id   INTEGER,         -- may be null until posted
    path            TEXT,            -- file path in PR diff
    line            INTEGER,         -- line number (if applicable)
    in_reply_to     INTEGER,         -- gh_comment_id of parent
    agent           TEXT,            -- posting agent
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|posted|failed
    created_at      INTEGER NOT NULL,
    posted_at       INTEGER,
    error           TEXT,
    raw_response    TEXT             -- JSON of gh response on post
);

CREATE INDEX IF NOT EXISTS idx_rc_pr ON review_comments(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_rc_status ON review_comments(status);
CREATE INDEX IF NOT EXISTS idx_rc_in_reply ON review_comments(in_reply_to);

CREATE TABLE IF NOT EXISTS webhook_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    repo        TEXT,
    pr_number   INTEGER,
    action      TEXT,                -- opened, closed, synchronize, etc.
    payload     TEXT NOT NULL,       -- raw JSON
    processed   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_we_pr ON webhook_events(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_we_received ON webhook_events(received_at);
"""


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# gh CLI wrapper
# ---------------------------------------------------------------------------


@dataclass
class GhResult:
    ok: bool
    status: int  # subprocess returncode; 0=ok
    stdout: str
    stderr: str
    data: Any = None  # parsed JSON if --json used


def _run_gh(
    args: list[str],
    *,
    timeout: int = 30,
    check: bool = False,
) -> GhResult:
    """Run ``gh <args>`` and return the result. Never raises on non-zero exit."""
    cmd = [GH_BIN, *args]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return GhResult(ok=False, status=127, stdout="", stderr=f"gh not found at {GH_BIN}")
    except subprocess.TimeoutExpired:
        return GhResult(ok=False, status=-1, stdout="", stderr=f"gh timeout after {timeout}s")
    return GhResult(
        ok=(r.returncode == 0) if check else (r.returncode == 0),
        status=r.returncode,
        stdout=r.stdout,
        stderr=r.stderr,
    )


def list_open_prs(repo: str = DEFAULT_REPO) -> list[dict]:
    """Return list of open PRs for ``repo``."""
    r = _run_gh(
        [
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,author,headRefName,baseRefName,createdAt,url,isDraft",
            "--limit", "50",
        ]
    )
    if not r.ok:
        raise RuntimeError(f"gh pr list failed: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout or "[]")


def get_pr(repo: str, number: int) -> dict:
    """Get PR details + diff. Returns dict with head, body, files, diff."""
    r = _run_gh(
        [
            "pr", "view", str(number),
            "--repo", repo,
            "--json",
            "number,title,state,author,headRefName,baseRefName,headRefOid,"
            "body,url,createdAt,additions,deletions,changedFiles,isDraft,mergeable,"
            "files",
        ]
    )
    if not r.ok:
        raise RuntimeError(f"gh pr view {number} failed: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout or "{}")


def list_comments(repo: str, number: int) -> list[dict]:
    """List all comments on a PR (issue + review comments)."""
    out: list[dict] = []
    # Issue comments (general PR conversation)
    r = _run_gh(
        [
            "pr", "view", str(number),
            "--repo", repo,
            "--json", "comments",
        ]
    )
    if r.ok:
        try:
            data = json.loads(r.stdout or "{}")
            for c in data.get("comments", []):
                out.append({
                    "id": c.get("id"),
                    "type": "issue",
                    "author": (c.get("author") or {}).get("login"),
                    "body": c.get("body", ""),
                    "created_at": c.get("createdAt"),
                    "in_reply_to": None,
                })
        except json.JSONDecodeError:
            pass
    # Review comments (inline on diff)
    r2 = _run_gh(
        [
            "api", f"repos/{repo}/pulls/{number}/comments",
            "--jq", ".[].{id:id,path:path,line:line,body:body,user:user.login,created_at:created_at,in_reply_to_id:in_reply_to_id}",
        ]
    )
    if r2.ok:
        for line in r2.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                out.append({
                    "id": c.get("id"),
                    "type": "review",
                    "path": c.get("path"),
                    "line": c.get("line"),
                    "author": c.get("user"),
                    "body": c.get("body", ""),
                    "created_at": c.get("created_at"),
                    "in_reply_to": c.get("in_reply_to_id"),
                })
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Local comment posting
# ---------------------------------------------------------------------------


def _save_session(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    title: Optional[str] = None,
    state: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO review_sessions (repo, pr_number, last_seen, title, state, head_sha)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, pr_number) DO UPDATE SET
            last_seen = excluded.last_seen,
            title = COALESCE(excluded.title, review_sessions.title),
            state = COALESCE(excluded.state, review_sessions.state),
            head_sha = COALESCE(excluded.head_sha, review_sessions.head_sha)
        """,
        (repo, pr_number, now, title, state, head_sha),
    )


def post_comment(
    repo: str,
    pr_number: int,
    body: str,
    *,
    path: Optional[str] = None,
    line: Optional[int] = None,
    in_reply_to: Optional[int] = None,
    agent: Optional[str] = None,
    post_to_github: bool = True,
) -> dict:
    """
    Save a comment locally; if post_to_github is True, also post via ``gh``.

    Returns dict with keys: id, gh_comment_id, status, posted_at, error.
    """
    if not body or not body.strip():
        raise ValueError("comment body required")
    if path is not None and line is None:
        raise ValueError("line required when path is set")
    if line is not None and path is None:
        raise ValueError("path required when line is set")

    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO review_comments
                (repo, pr_number, path, line, in_reply_to, agent, body, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                repo,
                pr_number,
                path,
                line,
                in_reply_to,
                agent or os.environ.get("AGENTCHAT_DEFAULT_AGENT", "hermes"),
                body,
                int(time.time()),
            ),
        )
        local_id = cur.lastrowid

        if not post_to_github:
            conn.commit()
            return {
                "id": local_id,
                "gh_comment_id": None,
                "status": "pending",
                "posted_at": None,
                "error": None,
            }

        # Post via gh
        if path and line:
            # Inline review comment via REST API (gh CLI pr review has no --path/--line).
            # Use a single-commit PR review payload so the comment is anchored to the
            # head SHA. commit_id is required for inline comments.
            head_sha = ""
            try:
                head_sha = (get_pr(repo, pr_number) or {}).get("headRefOid", "")
            except Exception:  # noqa: BLE001
                head_sha = ""
            payload = {
                "body": "",
                "comments": [
                    {"path": path, "line": int(line), "body": body}
                ],
            }
            if head_sha:
                payload["commit_id"] = head_sha
            args = [
                "api", f"repos/{repo}/pulls/{pr_number}/reviews",
                "--method", "POST",
                "--input", "-",
            ]
            r = _run_gh(args, timeout=30)
            # The above won't work because we need to pipe JSON. Use raw subprocess
            # to feed the payload via stdin.
            cmd = [GH_BIN, "api", f"repos/{repo}/pulls/{pr_number}/reviews",
                   "--method", "POST", "--input", "-"]
            try:
                proc = subprocess.run(
                    cmd,
                    input=json.dumps(payload).encode("utf-8"),
                    capture_output=True,
                    text=False,
                    timeout=30,
                    check=False,
                )
                r = GhResult(
                    ok=(proc.returncode == 0),
                    status=proc.returncode,
                    stdout=(proc.stdout or b"").decode("utf-8", errors="replace"),
                    stderr=(proc.stderr or b"").decode("utf-8", errors="replace"),
                )
            except FileNotFoundError:
                r = GhResult(ok=False, status=127, stdout="", stderr=f"gh not found at {GH_BIN}")
            except subprocess.TimeoutExpired:
                r = GhResult(ok=False, status=-1, stdout="", stderr="gh timeout after 30s")
        else:
            # Issue comment (general PR conversation) via gh pr comment
            args = [
                "pr", "comment", str(pr_number),
                "--repo", repo,
                "--body", body,
            ]
            r = _run_gh(args, timeout=30)
        if r.ok:
            # Parse gh output to extract comment URL/ID. The inline review
            # path returns JSON; the issue-comment path returns a URL.
            gh_id: Optional[int] = None
            stdout = (r.stdout or "").strip()
            # Try JSON first (gh api POST /reviews returns {id, html_url})
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    if "id" in parsed:
                        gh_id = int(parsed["id"])
                    elif "node_id" in parsed and isinstance(parsed["node_id"], str):
                        # Last-resort: parse trailing digits
                        digits = "".join(c for c in parsed["node_id"] if c.isdigit())
                        if digits:
                            gh_id = int(digits)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            # Fall back to URL pattern matching (gh pr comment output)
            if gh_id is None:
                for raw_line in stdout.splitlines():
                    if "/pull/" in raw_line and "#issuecomment-" in raw_line:
                        try:
                            gh_id = int(raw_line.rsplit("-", 1)[-1])
                        except ValueError:
                            pass
                    elif "/pull/" in raw_line and "#discussion_r" in raw_line:
                        try:
                            gh_id = int(raw_line.rsplit("r", 1)[-1])
                        except ValueError:
                            pass

            conn.execute(
                """
                UPDATE review_comments
                SET status='posted', posted_at=?, gh_comment_id=?, raw_response=?
                WHERE id=?
                """,
                (int(time.time()), gh_id, r.stdout[:4000], local_id),
            )
            conn.commit()
            return {
                "id": local_id,
                "gh_comment_id": gh_id,
                "status": "posted",
                "posted_at": int(time.time()),
                "error": None,
            }
        else:
            err = (r.stderr or r.stdout or "").strip()[:500]
            conn.execute(
                """
                UPDATE review_comments
                SET status='failed', error=?, raw_response=?
                WHERE id=?
                """,
                (err, (r.stdout or "")[:4000], local_id),
            )
            conn.commit()
            return {
                "id": local_id,
                "gh_comment_id": None,
                "status": "failed",
                "posted_at": None,
                "error": err,
            }
    finally:
        conn.close()


def list_local_comments(
    repo: str, pr_number: int, *, status: Optional[str] = None
) -> list[dict]:
    conn = _connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM review_comments WHERE repo=? AND pr_number=? AND status=? ORDER BY id",
                (repo, pr_number, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM review_comments WHERE repo=? AND pr_number=? ORDER BY id",
                (repo, pr_number),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------


def record_webhook(
    event_type: str,
    payload: dict,
    *,
    received_at: Optional[int] = None,
) -> int:
    """Store a webhook event. Returns the event id."""
    repo = None
    pr_number = None
    action = None
    if "repository" in payload and isinstance(payload["repository"], dict):
        repo = payload["repository"].get("full_name")
    if "pull_request" in payload and isinstance(payload["pull_request"], dict):
        pr_number = payload["pull_request"].get("number")
    if "issue" in payload and isinstance(payload["issue"], dict):
        pr_number = payload["issue"].get("number")
    action = payload.get("action")

    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO webhook_events
                (event_type, received_at, repo, pr_number, action, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                received_at or int(time.time()),
                repo,
                pr_number,
                action,
                json.dumps(payload)[:65536],
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_recent_webhook_events(limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_list(args: list[str]) -> int:
    repo = DEFAULT_REPO
    if "--repo" in args:
        i = args.index("--repo")
        repo = args[i + 1]
    prs = list_open_prs(repo)
    if not prs:
        print(f"No open PRs on {repo}")
        return 0
    print(f"Open PRs on {repo}:")
    for p in prs:
        draft = " (draft)" if p.get("isDraft") else ""
        author = (p.get("author") or {}).get("login", "?")
        print(
            f"  #{p['number']:>4}  {author:<16}  "
            f"{p.get('headRefName', '?'):<30}  "
            f"{p.get('title', '?')[:60]}{draft}"
        )
    return 0


def _cmd_show(args: list[str]) -> int:
    if not args:
        print("usage: pr-review show <pr-number> [--repo <owner/repo>]", file=sys.stderr)
        return 2
    pr = int(args[0])
    repo = DEFAULT_REPO
    if "--repo" in args:
        i = args.index("--repo")
        repo = args[i + 1]
    info = get_pr(repo, pr)
    author = (info.get("author") or {}).get("login", "?")
    print(f"PR #{info.get('number')}  {info.get('title')}")
    print(f"  state:    {info.get('state')}  mergeable={info.get('mergeable')}")
    print(f"  author:   {author}")
    print(f"  url:      {info.get('url')}")
    print(
        f"  diff:     +{info.get('additions', 0)} -{info.get('deletions', 0)} "
        f"in {info.get('changedFiles', 0)} files"
    )
    print(f"  head:     {info.get('headRefName')} ({info.get('headRefOid', '?')[:12]})")
    print(f"  base:     {info.get('baseRefName')}")
    print()
    print("Body:")
    print(info.get("body", ""))
    return 0


def _cmd_comment(args: list[str]) -> int:
    if not args:
        print(
            "usage: pr-review comment <pr-number> --body <text> "
            "[--path <file> --line <n>] [--reply-to <gh-comment-id>] "
            "[--agent <name>] [--repo <owner/repo>] [--no-post]",
            file=sys.stderr,
        )
        return 2
    pr = int(args[0])
    repo = DEFAULT_REPO
    body = None
    path = None
    line = None
    in_reply_to = None
    agent = None
    post_to_github = True

    i = 1
    while i < len(args):
        a = args[i]
        if a == "--body" and i + 1 < len(args):
            body = args[i + 1]
            i += 2
        elif a == "--path" and i + 1 < len(args):
            path = args[i + 1]
            i += 2
        elif a == "--line" and i + 1 < len(args):
            line = int(args[i + 1])
            i += 2
        elif a == "--reply-to" and i + 1 < len(args):
            in_reply_to = int(args[i + 1])
            i += 2
        elif a == "--agent" and i + 1 < len(args):
            agent = args[i + 1]
            i += 2
        elif a == "--repo" and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
        elif a == "--no-post":
            post_to_github = False
            i += 1
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            return 2

    if not body:
        print("--body required", file=sys.stderr)
        return 2

    result = post_comment(
        repo,
        pr,
        body,
        path=path,
        line=line,
        in_reply_to=in_reply_to,
        agent=agent,
        post_to_github=post_to_github,
    )
    print(json.dumps(result, indent=2))
    if result["status"] == "failed":
        return 1
    return 0


def _cmd_comments(args: list[str]) -> int:
    if not args:
        print("usage: pr-review comments <pr-number> [--repo <owner/repo>]", file=sys.stderr)
        return 2
    pr = int(args[0])
    repo = DEFAULT_REPO
    if "--repo" in args:
        i = args.index("--repo")
        repo = args[i + 1]
    rows = list_local_comments(repo, pr)
    if not rows:
        print(f"No local comments on {repo}#{pr}")
        return 0
    print(f"Local comments on {repo}#{pr}:")
    for r in rows:
        posted = r.get("posted_at") or "-"
        agent = r.get("agent") or "?"
        path = r.get("path") or "(general)"
        if r.get("line"):
            path = f"{path}:{r['line']}"
        print(
            f"  #{r['id']:>4}  {r['status']:<8}  {agent:<10}  "
            f"{path:<40}  posted_at={posted}"
        )
        body = r.get("body", "")
        if len(body) > 80:
            body = body[:77] + "..."
        print(f"         {body}")
    return 0


def _cmd_webhooks(args: list[str]) -> int:
    limit = 20
    if args and args[0].isdigit():
        limit = int(args[0])
    events = list_recent_webhook_events(limit)
    if not events:
        print("No webhook events received yet.")
        return 0
    print(f"Recent webhook events (latest {len(events)}):")
    for e in events:
        pr = f"#{e['pr_number']}" if e.get("pr_number") else "-"
        print(
            f"  id={e['id']:>5}  {e['received_at']}  {e['event_type']:<20}  "
            f"{e.get('repo', '?')}/{pr}  action={e.get('action') or '-'}"
        )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "agentchat-pr-review — GitHub PR review CLI\n"
            "\n"
            "Commands:\n"
            "  list                       List open PRs on --repo (default: AGENTCHAT_PR_REVIEW_REPO)\n"
            "  show <pr>                  Show PR details + body\n"
            "  comments <pr>              List local comments on a PR\n"
            "  comment <pr> [opts]        Post a comment (see below)\n"
            "  webhooks [limit]           List recent webhook events\n"
            "\n"
            "Comment options:\n"
            "  --body <text>              Comment text (required)\n"
            "  --path <file> --line <n>   Inline review comment on diff\n"
            "  --reply-to <gh-id>         Reply to existing comment (threading)\n"
            "  --agent <name>             Posting agent (default: hermes)\n"
            "  --repo <owner/repo>        Target repo\n"
            "  --no-post                  Save locally only; do not post to GitHub\n"
        )
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        return _cmd_list(rest)
    if cmd == "show":
        return _cmd_show(rest)
    if cmd == "comment":
        return _cmd_comment(rest)
    if cmd == "comments":
        return _cmd_comments(rest)
    if cmd == "webhooks":
        return _cmd_webhooks(rest)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
