"""
Watch daemon for agentchat GitHub sync (v1.2.0.dev21).

This module owns the **daemon lifecycle** on top of ``sync_agent.watcher``:

* PID file management (write on start, delete on stop, detect stale)
* Foreground vs. detached (POSIX double-fork)
* Signal handling (SIGTERM / SIGINT → graceful shutdown, cancel debounce,
  attempt final push, remove PID file)
* ``--status`` / ``--stop`` subcommands for operators

The actual polling, debouncing, and committing are delegated to
``sync_agent.watcher.watch_and_commit``. The post-commit push is
delegated to ``agentchat.sync_github.push``.

The seam between "what to do after a commit lands" and "how to keep the
daemon alive" is the ``on_commit`` callback passed into
``watch_and_commit``. The daemon supplies a callback that:

1. Logs the commit (so detached operators can grep the log file).
2. Calls ``sync_github.push`` (with throttling — see ``min_push_interval``).
3. Records the push in the audit log.

The callback never raises into the watch loop. Push failures are logged
and counted; the daemon stays alive so transient GitHub / network
issues do not silently drop future syncs.

Cross-platform note
-------------------
``detach`` uses ``os.fork()`` and is therefore POSIX-only. The watch
CLI degrades gracefully on Windows: ``--detach`` is rejected with a
helpful error and the user is told to use ``start /b`` or a scheduled
task instead.
"""

from __future__ import annotations

import dataclasses
import errno
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .sync_agent.config import DEFAULT_EXCLUDE, SyncConfig
from .sync_agent.watcher import DebouncedEmitter, PollingEmitter, ChangeSet
from . import sync_github

log = logging.getLogger("agentchat.watch")


# ---------------------------------------------------------------------------
# WatchConfig
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class WatchConfig:
    """All knobs the watch CLI exposes.

    Defaults are tuned for "drop it in cron / systemd, forget about it":
    5-second poll cadence, 5-second debounce window, 30-second minimum
    gap between pushes (so a burst of memory writes does not hammer
    GitHub), PID + log in ``~/.hermes/sync/``.
    """

    # Where the local mirror repo lives. Must already be initialised
    # via ``agentchat-sync init`` — the watcher does not auto-init.
    mirror_root: Path

    # Filesystem roots to watch for changes. The memory tree is the
    # primary target; other roots can be added for config files.
    watched_roots: tuple[Path, ...]

    # Sync agent knobs (passed through to SyncConfig).
    debounce_seconds: float = 5.0
    poll_interval_seconds: float = 1.0
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    include_hidden: bool = False

    # Watch-daemon-only knobs.
    min_push_interval_seconds: float = 30.0

    # Commit author (overrides the git config of whoever runs the
    # daemon — important for detached mode where there's no human
    # git identity in the env).
    author_name: str = "agentchat-sync"
    author_email: str = "agentchat-sync@localhost"

    # Daemon lifecycle paths.
    pid_file: Path = dataclasses.field(default_factory=lambda: Path("~/.hermes/sync/watch.pid").expanduser())
    log_file: Path = dataclasses.field(default_factory=lambda: Path("~/.hermes/sync/watch.log").expanduser())

    # Internal — set by the daemon to coordinate the post-commit push
    # callback. Not part of the user-facing CLI surface.
    workspace_slug: str = "default"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


_DEFAULT_CONFIG_PATH = Path("~/.hermes/sync/config.yaml").expanduser()


def load_config(path: Path | None = None) -> WatchConfig:
    """Load a WatchConfig from YAML, falling back to defaults.

    The YAML format is intentionally minimal — only the fields the
    operator is likely to override are supported. Everything else
    takes the dataclass default.
    """
    import yaml  # local import — pyyaml is in our dep set

    cfg_path = (path or _DEFAULT_CONFIG_PATH).expanduser()
    data: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if isinstance(loaded, dict):
            data = loaded

    # Mirror root: derive from workspace slug if not set explicitly.
    workspace_slug = str(data.get("workspace_slug", "default"))
    mirror_root_raw = data.get("mirror_root")
    if mirror_root_raw:
        mirror_root = Path(mirror_root_raw).expanduser()
    else:
        mirror_root = (
            Path("~/.hermes/sync/mirror").expanduser() / workspace_slug
        )

    watched_roots_raw = data.get("watched_roots") or data.get("memory_root")
    if watched_roots_raw:
        if isinstance(watched_roots_raw, (str, Path)):
            watched_roots = (Path(watched_roots_raw).expanduser(),)
        else:
            watched_roots = tuple(
                Path(p).expanduser() for p in watched_roots_raw
            )
    else:
        watched_roots = (Path("~/.hermes/memory").expanduser(),)

    exclude_raw = data.get("exclude")
    if exclude_raw:
        exclude = tuple(str(p) for p in exclude_raw)
    else:
        exclude = DEFAULT_EXCLUDE

    return WatchConfig(
        mirror_root=mirror_root,
        watched_roots=watched_roots,
        debounce_seconds=float(data.get("debounce_seconds", 5.0)),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 1.0)),
        exclude=exclude,
        include_hidden=bool(data.get("include_hidden", False)),
        min_push_interval_seconds=float(data.get("min_push_interval_seconds", 30.0)),
        author_name=str(data.get("author_name", "agentchat-sync")),
        author_email=str(data.get("author_email", "agentchat-sync@localhost")),
        workspace_slug=workspace_slug,
    )


# ---------------------------------------------------------------------------
# PID-file lifecycle
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists and is alive (POSIX only).

    We use ``kill(pid, 0)`` which is the POSIX idiom for "is this PID
    yours / does it exist". It sends no signal but does the existence
    check. EPERM means it exists but we lack permission — we treat
    that as "alive" so we don't double-start.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_file(pid_file: Path) -> int | None:
    """Return the PID stored in ``pid_file``, or None if missing / malformed.

    Does NOT verify the PID is alive — that is the caller's job. We
    separate the two because ``status`` wants to show "PID file says X
    but the process is gone" rather than silently hiding it.
    """
    if not pid_file.exists():
        return None
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def write_pid_file(pid_file: Path, pid: int) -> None:
    """Atomically write ``pid`` to ``pid_file``.

    We write to a sibling temp file then ``os.replace`` so a crashed
    write never leaves a half-formed PID file behind.
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_file.with_suffix(pid_file.suffix + ".tmp")
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    os.replace(tmp, pid_file)


def remove_pid_file(pid_file: Path) -> None:
    """Remove the PID file if it exists. Errors are swallowed.

    The rationale: the PID file is the daemon's bookkeeping, not
    critical state. If we cannot remove it (e.g. permissions), we
    would rather leave a stale file (which a future ``start`` will
    detect and overwrite) than crash the daemon on its way down.
    """
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove PID file %s: %s", pid_file, exc)


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


class WatchDaemon:
    """Owns the lifecycle of a single watch process.

    One ``WatchDaemon`` instance is created per ``agentchat-sync watch``
    invocation. In foreground mode it runs the loop in the calling
    thread; in detached mode the child process does the same and the
    parent returns immediately after forking.
    """

    def __init__(self, config: WatchConfig):
        self.config = config
        self._stop_requested = False
        self._last_push_at: float = 0.0
        self._push_count = 0
        self._commit_count = 0

    # -- public surface ----------------------------------------------------

    def is_running(self) -> tuple[bool, int | None]:
        """Return ``(running, pid)``.

        ``running`` is True iff the PID file points at a live process.
        ``pid`` is the PID from the file (or None if the file is
        missing). When ``running`` is False but ``pid`` is not None,
        the caller knows the file is stale and should be cleaned up.
        """
        pid = read_pid_file(self.config.pid_file)
        if pid is None:
            return False, None
        if _pid_alive(pid):
            return True, pid
        return False, pid

    def request_stop(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        """Signal handler — sets the stop flag. The loop checks the flag."""
        signame = signal.Signals(signum).name if signum in [s.value for s in signal.Signals] else str(signum)
        log.info("received %s — requesting graceful shutdown", signame)
        self._stop_requested = True

    def run_foreground(self) -> int:
        """Run the watch loop in the current process.

        Returns the process exit code: 0 on clean shutdown, 1 on
        unexpected error, 2 on configuration error (e.g. mirror not
        initialised).
        """
        try:
            self._validate_preconditions()
        except _PreconditionError as exc:
            log.error("precondition failed: %s", exc)
            return 2

        running, stale_pid = self.is_running()
        if running:
            log.error(
                "another watch daemon is already running (pid=%d, pid_file=%s)",
                stale_pid,
                self.config.pid_file,
            )
            return 1
        if stale_pid is not None:
            log.warning(
                "stale PID file (pid=%d no longer alive) — cleaning up",
                stale_pid,
            )
            remove_pid_file(self.config.pid_file)

        write_pid_file(self.config.pid_file, os.getpid())
        log.info(
            "watch daemon started (pid=%d, mirror=%s, watched=%s, debounce=%.1fs, push_interval=%.1fs)",
            os.getpid(),
            self.config.mirror_root,
            [str(r) for r in self.config.watched_roots],
            self.config.debounce_seconds,
            self.config.min_push_interval_seconds,
        )

        # Install signal handlers. ``signal.signal`` only works in
        # the main thread of the main interpreter; if a caller
        # invokes the daemon from a worker thread (e.g. in tests)
        # the install fails with ValueError. We catch and log so
        # the daemon still works in those contexts — the test
        # can set ``_stop_requested`` directly to trigger shutdown.
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)
        except ValueError as exc:
            log.warning(
                "could not install signal handlers (likely running in a worker thread): %s; "
                "use _stop_requested flag to trigger shutdown",
                exc,
            )

        try:
            self._run_loop()
            return 0
        except Exception:
            log.exception("watch loop crashed")
            return 1
        finally:
            self._shutdown()

    # -- detached mode -----------------------------------------------------

    def detach(self) -> int:
        """Fork into the background and return the child's PID.

        Returns the child's PID (>0) on success, or 0 if we are the
        child (and should exit), or -1 on fork failure.

        POSIX only. On Windows this raises ``OSError`` so the CLI can
        surface a clear "use a scheduled task" message.
        """
        if sys.platform == "win32":
            raise OSError(
                errno.ENOSYS,
                "agentchat-sync watch --detach is POSIX-only; on Windows use "
                "a scheduled task or `start /b` instead",
            )

        # First fork.
        try:
            pid = os.fork()
        except OSError as exc:
            log.error("first fork failed: %s", exc)
            return -1
        if pid > 0:
            # Parent — return child PID to caller. Caller will print it.
            return pid

        # Child — detach from controlling terminal and session.
        os.setsid()
        try:
            pid = os.fork()
        except OSError as exc:
            log.error("second fork failed: %s", exc)
            os._exit(1)
        if pid > 0:
            # First child exits so the grandchild is reparented to init.
            os._exit(0)

        # Grandchild — the actual daemon.
        return 0

    # -- internals ---------------------------------------------------------

    def _validate_preconditions(self) -> None:
        """Make sure the mirror is initialised and reachable.

        We do not auto-init; auto-creating a GitHub repo on the
        operator's behalf would be a footgun (a stray `gh repo create`
        against the wrong account, for example). The operator must
        have run ``agentchat-sync init`` first.
        """
        if not self.config.mirror_root.exists():
            raise _PreconditionError(
                f"mirror_root does not exist: {self.config.mirror_root}. "
                f"Run `agentchat-sync init` first."
            )
        git_dir = self.config.mirror_root / ".git"
        if not git_dir.exists():
            raise _PreconditionError(
                f"mirror_root is not a git repo: {self.config.mirror_root}. "
                f"Run `agentchat-sync init` first."
            )
        for root in self.config.watched_roots:
            if not root.exists():
                raise _PreconditionError(
                    f"watched_root does not exist: {root}. "
                    f"Create it, fix the config, or remove it from watched_roots."
                )

    def _run_loop(self) -> None:
        """The actual watch + push loop.

        Architectural note: we do NOT use
        ``watch_and_commit(once=False)`` here. Two reasons:

        1. ``watch_and_commit``'s daemon loop sleeps for the full
           ``poll_interval_seconds`` between polls, which gives SIGTERM
           a worst-case multi-second response. We want sub-second
           SIGTERM response so detached daemons stop cleanly when
           ``agentchat-sync watch --stop`` is invoked.
        2. ``sync_github.push()`` *already* does its own commit when
           it materialises the mirror tree. So the commit step that
           ``watch_and_commit`` performs is redundant — the push
           itself will commit any new mirror state. We keep the
           ``PollingEmitter`` and ``DebouncedEmitter`` to detect
           WHEN to trigger a push, but we let ``sync_github.push``
           own the commit.

        This also means the watch loop is a thinner wrapper: detect
        change, debounce, push. The mirror's own git history is
        authoritative.
        """
        sync_config = self._build_sync_config()
        emitter = PollingEmitter(sync_config)
        # Establish the initial baseline so the first detected change
        # is a real diff rather than the entire tree.
        emitter.poll()

        # The callback runs in a daemon thread (DebouncedEmitter uses
        # threading.Timer). It must be thread-safe with respect to
        # ``_stop_requested`` reads, but since ``_stop_requested`` is a
        # plain bool and we only ever set it to True, Python's GIL
        # gives us a happens-before edge on read.
        def _on_change(change: ChangeSet) -> None:
            self._commit_count += 1
            try:
                self._maybe_push(change)
            finally:
                # Reset the polling baseline so the next poll diffs
                # against the state we just observed + (potentially)
                # pushed, not the original pre-change state. Without
                # this, the polling emitter would keep returning the
                # same diff on every poll, the debouncer would keep
                # restarting its timer, and the callback would fire
                # in a tight loop (or never, if the push is slow and
                # the timer keeps getting cancelled).
                emitter.reset_baseline()

        debouncer = DebouncedEmitter(
            emitter,
            debounce_seconds=self.config.debounce_seconds,
            callback=_on_change,
        )

        # Tight shutdown loop. Poll the emitter on the configured
        # cadence, but also check the stop flag every 0.5s so SIGTERM
        # is honoured promptly.
        poll_sleep = min(self.config.poll_interval_seconds, 0.5)
        elapsed_in_window = 0.0
        while not self._stop_requested:
            debouncer.poll()
            time.sleep(poll_sleep)
            elapsed_in_window += poll_sleep
            if elapsed_in_window >= self.config.poll_interval_seconds:
                elapsed_in_window = 0.0
            # Stop check is inside the loop, so we exit on the next
            # iteration once the flag flips. With poll_sleep=0.5s
            # that gives SIGTERM a 1-second worst-case response time.

        # Graceful shutdown: cancel any pending debounce.
        debouncer.cancel()
        log.info(
            "watch loop exiting (triggers=%d, pushes=%d)",
            self._commit_count,
            self._push_count,
        )

    def _maybe_push(self, change: ChangeSet) -> None:
        """Trigger a ``sync_github.push`` for the current state of the
        mirror tree, throttled by ``min_push_interval_seconds``.

        Throttling rationale: a burst of memory writes (e.g. a user
        editing 20 memory files at once) can produce many change
        events in quick succession. Each push to GitHub is a full
        network round-trip; we don't want to spam. The throttle does
        not drop pushes — it *defers* them by skipping the current
        push. Because ``sync_github.push()`` reads the current state
        of ``~/.hermes/memory/`` each time, the next push always
        captures the latest state regardless of how many events
        were skipped in between.

        In practice the debounce in front of the trigger means we
        never see more than one trigger per debounce window anyway,
        so the throttle is a belt-and-braces guard rather than the
        primary line of defence.
        """
        now = time.monotonic()
        since_last = now - self._last_push_at
        if since_last < self.config.min_push_interval_seconds and self._last_push_at > 0:
            log.info(
                "skipping push (triggered %ds after last push, throttle=%.1fs); "
                "the next trigger or shutdown will push the latest state",
                int(since_last),
                self.config.min_push_interval_seconds,
            )
            return

        try:
            self._do_push(change)
            self._last_push_at = time.monotonic()
            self._push_count += 1
        except Exception:
            # Push failures must never kill the daemon. The next
            # trigger will retry on the same memory state, so a
            # transient network blip self-heals on the next change.
            log.exception(
                "push failed for trigger %s; daemon continues",
                change.summary_line(),
            )

    def _do_push(self, change: ChangeSet) -> None:
        """Call ``sync_github.push`` with our config.

        ``sync_github.push`` always rebuilds the mirror tree from
        ``~/.hermes/memory/`` before pushing, so the ``change`` arg
        is purely for logging — the actual content pushed reflects
        whatever is on disk in the memory tree at push time. This
        is a feature, not a bug: the push is always authoritative
        for the full state, not just the diff.
        """
        remote = self._resolve_remote()
        if not remote:
            log.error(
                "cannot push: mirror %s has no 'origin' remote configured. "
                "Run `agentchat-sync init --remote <url>` first.",
                self.config.mirror_root,
            )
            return

        result = sync_github.push(
            workspace_slug=self.config.workspace_slug,
            remote=remote,
            commit_message=(
                f"watch: {change.summary_line()}"
                if change.records
                else "watch: auto-sync (no detected changes)"
            ),
            author_name=self.config.author_name,
            author_email=self.config.author_email,
        )
        scrub_total = (
            sum(result.scrub_stats.counts.values()) if result.scrub_stats else 0
        )
        log.info(
            "pushed: commit=%s files=%d scrubbed=%d pushed=%s",
            result.commit_sha[:8] if result.commit_sha else "?",
            result.files_mirrored,
            scrub_total,
            result.pushed,
        )

    def _resolve_remote(self) -> str | None:
        """Read the mirror's ``origin`` remote URL via ``git remote get-url``.

        Returns the URL string or None if the command fails (no
        remote configured, git not in PATH, etc.). The CLI surfaces
        a clear error in that case via the caller of ``_do_push``.
        """
        try:
            out = subprocess.run(
                ["git", "-C", str(self.config.mirror_root), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.warning("could not resolve 'origin' remote: %s", exc)
            return None
        return out.stdout.strip() or None

    def _build_sync_config(self) -> SyncConfig:
        """Translate WatchConfig into the SyncConfig the watch loop uses."""
        return SyncConfig(
            repo_dir=self.config.mirror_root,
            watched_roots=self.config.watched_roots,
            debounce_seconds=self.config.debounce_seconds,
            exclude=self.config.exclude,
            include_hidden=self.config.include_hidden,
            author_name=self.config.author_name,
            author_email=self.config.author_email,
        )

    def _shutdown(self) -> None:
        """Cleanup hook called from the foreground runner's finally block."""
        remove_pid_file(self.config.pid_file)
        log.info("watch daemon stopped (pid=%d)", os.getpid())


class _PreconditionError(RuntimeError):
    """Raised when the watch daemon cannot start due to bad config."""


# ---------------------------------------------------------------------------
# Top-level command helpers (used by the CLI)
# ---------------------------------------------------------------------------


def cmd_watch_foreground(config: WatchConfig) -> int:
    """``agentchat-sync watch`` (no flags) — run in the calling process."""
    daemon = WatchDaemon(config)
    return daemon.run_foreground()


def cmd_watch_detach(config: WatchConfig) -> int:
    """``agentchat-sync watch --detach`` — fork, return child PID.

    The child process re-execs this CLI in foreground mode with stdout
    redirected to the log file. The parent returns immediately.
    """
    # Pre-check: refuse to start a second daemon.
    daemon = WatchDaemon(config)
    running, stale_pid = daemon.is_running()
    if running:
        print(
            f"another watch daemon is already running (pid={stale_pid})",
            file=sys.stderr,
        )
        return 1
    if stale_pid is not None:
        print(
            f"removing stale PID file (pid={stale_pid} no longer alive)",
            file=sys.stderr,
        )
        remove_pid_file(config.pid_file)

    # Re-exec ourselves in a detached child. The double-fork happens
    # in the child; the parent sees the grandchild PID via
    # ``detach()``'s return and exits cleanly.
    config.pid_file.parent.mkdir(parents=True, exist_ok=True)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "agentchat.sync_cli",
        "watch",
        "--foreground",  # child runs the real loop
        "--log-level",
        "INFO",
    ]
    if config.mirror_root:
        cmd.extend(["--mirror-root", str(config.mirror_root)])
    for r in config.watched_roots:
        cmd.extend(["--watched-root", str(r)])
    cmd.extend(["--pid-file", str(config.pid_file)])
    cmd.extend(["--log-file", str(config.log_file)])

    # Open the log file for the child to write to.
    log_fd = os.open(
        str(config.log_file),
        flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        mode=0o644,
    )

    # Fork once. The parent will write the child's PID to the PID
    # file; the child will exec the foreground runner with stdout
    # redirected to the log file.
    try:
        pid = os.fork()
    except OSError as exc:
        log.error("fork failed: %s", exc)
        return 1
    if pid > 0:
        # Parent — write the child's PID, then exit.
        write_pid_file(config.pid_file, pid)
        print(f"watch daemon detached (pid={pid}, log={config.log_file})")
        return 0

    # Child — detach, redirect stdio, exec the foreground runner.
    os.setsid()
    os.dup2(log_fd, 1)  # stdout
    os.dup2(log_fd, 2)  # stderr
    os.close(log_fd)
    with open("/dev/null", "rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    os.execvp(cmd[0], cmd)
    # execvp only returns on failure.
    os._exit(1)


def cmd_watch_stop(config: WatchConfig) -> int:
    """``agentchat-sync watch --stop`` — send SIGTERM, wait, force-kill if needed."""
    pid = read_pid_file(config.pid_file)
    if pid is None:
        print(f"no PID file at {config.pid_file}; nothing to stop")
        return 0
    if not _pid_alive(pid):
        print(f"stale PID file (pid={pid} not alive); removing")
        remove_pid_file(config.pid_file)
        return 0

    print(f"sending SIGTERM to pid {pid}…")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"pid {pid} vanished before we could signal it")
        remove_pid_file(config.pid_file)
        return 0
    except PermissionError:
        print(
            f"permission denied signalling pid {pid} — is the daemon owned by another user?",
            file=sys.stderr,
        )
        return 1

    # Wait up to 5 seconds for graceful shutdown.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            print(f"pid {pid} stopped cleanly")
            remove_pid_file(config.pid_file)
            return 0
        time.sleep(0.1)

    # Force-kill.
    print(f"pid {pid} did not stop in 5s; sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Give it a moment, then clean up.
    time.sleep(0.5)
    if _pid_alive(pid):
        print(f"pid {pid} still alive after SIGKILL — investigate manually", file=sys.stderr)
        return 1
    remove_pid_file(config.pid_file)
    return 0


def cmd_watch_status(config: WatchConfig) -> int:
    """``agentchat-sync watch --status`` — report running / stopped / stale."""
    daemon = WatchDaemon(config)
    running, pid = daemon.is_running()
    if running:
        print(f"running (pid={pid}, pid_file={config.pid_file})")
        if config.log_file.exists():
            mtime = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(config.log_file.stat().st_mtime),
            )
            print(f"log_file={config.log_file} (last modified {mtime})")
        return 0
    if pid is not None:
        print(f"stopped (stale PID file: pid={pid} no longer alive)")
        return 1
    print(f"stopped (no PID file at {config.pid_file})")
    return 1
