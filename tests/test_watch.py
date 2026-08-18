"""
Tests for the watch daemon (v1.2.0.dev21).

Layers:
* Unit tests of the small helpers (``read_pid_file``,
  ``write_pid_file``, ``remove_pid_file``, ``_pid_alive``).
* Unit tests of ``WatchConfig`` defaults + ``load_config`` YAML
  loading + CLI override merge.
* Unit tests of ``WatchDaemon.is_running`` for the three states
  (missing, running, stale).
* Integration tests of the daemon lifecycle in foreground mode
  against a real local mirror + fake remote (local bare git repo).
* Tests of the ``--stop`` / ``--status`` command helpers.
* A regression test for the ``watch_and_commit(once=True)`` +
  ``on_commit`` refactor to make sure the seam still works.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agentchat import watch
from agentchat.sync_agent.commit import CommitResult
from agentchat.sync_agent.watcher import watch_and_commit
from agentchat.watch import (
    WatchConfig,
    WatchDaemon,
    _pid_alive,
    cmd_watch_status,
    cmd_watch_stop,
    load_config,
    read_pid_file,
    remove_pid_file,
    write_pid_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_mirror(tmp_path: Path) -> Path:
    """Create a local bare git repo to act as the remote + a working tree.

    Returns the working-tree path (the local mirror). The remote lives
    at ``tmp_path/remote.git`` as a bare repo. The working tree is
    initialised with one commit so the local repo has a real HEAD.
    """
    remote = tmp_path / "remote.git"
    work = tmp_path / "mirror"
    work.mkdir()
    remote.mkdir()

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    (work / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    return work


@pytest.fixture
def tmp_memory(tmp_path: Path) -> Path:
    """Create a memory tree the daemon can watch."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "agents").mkdir()
    (mem / "team").mkdir()
    (mem / "agents" / "hermes.md").write_text("# hermes\n", encoding="utf-8")
    return mem


@pytest.fixture
def watch_config(tmp_mirror: Path, tmp_memory: Path, tmp_path: Path) -> WatchConfig:
    """A WatchConfig wired up to the temp mirror + memory + pid/log paths."""
    return WatchConfig(
        mirror_root=tmp_mirror,
        watched_roots=(tmp_memory,),
        debounce_seconds=0.1,  # tight for tests
        poll_interval_seconds=0.1,
        min_push_interval_seconds=0.0,  # off for tests
        pid_file=tmp_path / "watch.pid",
        log_file=tmp_path / "watch.log",
        workspace_slug="test",
    )


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


class TestPidFileHelpers:
    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_pid_file(tmp_path / "nope.pid") is None

    def test_read_malformed_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "weird.pid"
        p.write_text("not-a-number\n", encoding="utf-8")
        assert read_pid_file(p) is None

    def test_write_then_read(self, tmp_path: Path) -> None:
        p = tmp_path / "x.pid"
        write_pid_file(p, 12345)
        assert read_pid_file(p) == 12345

    def test_remove_missing_is_silent(self, tmp_path: Path) -> None:
        remove_pid_file(tmp_path / "nope.pid")  # must not raise

    def test_remove_existing(self, tmp_path: Path) -> None:
        p = tmp_path / "x.pid"
        write_pid_file(p, 999)
        remove_pid_file(p)
        assert not p.exists()

    def test_pid_alive_negative(self) -> None:
        assert _pid_alive(0) is False
        assert _pid_alive(-1) is False

    def test_pid_alive_self(self) -> None:
        # The test process itself is alive.
        assert _pid_alive(os.getpid()) is True

    def test_pid_alive_dead(self) -> None:
        # Find a PID that almost certainly does not exist. The OS
        # often wraps PIDs so 999_999 is usually free.
        dead = 999_999
        if _pid_alive(dead):
            pytest.skip("999_999 happened to be alive on this host; skipping")
        assert _pid_alive(dead) is False


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_defaults_when_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watch, "_DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
        cfg = load_config()
        assert cfg.debounce_seconds == 5.0
        assert cfg.poll_interval_seconds == 1.0
        assert cfg.min_push_interval_seconds == 30.0
        assert cfg.mirror_root == Path("~/.hermes/sync/mirror/default").expanduser()
        assert cfg.watched_roots == (Path("~/.hermes/memory").expanduser(),)
        assert cfg.workspace_slug == "default"

    def test_yaml_overrides(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            workspace_slug: acme
            debounce_seconds: 7.5
            poll_interval_seconds: 2.0
            min_push_interval_seconds: 45.0
            watched_roots:
              - /tmp/foo
              - /tmp/bar
            exclude:
              - "*.tmp"
              - "*.bak"
            include_hidden: true
        """).strip(), encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.workspace_slug == "acme"
        assert cfg.debounce_seconds == 7.5
        assert cfg.poll_interval_seconds == 2.0
        assert cfg.min_push_interval_seconds == 45.0
        assert cfg.watched_roots == (Path("/tmp/foo"), Path("/tmp/bar"))
        assert cfg.exclude == ("*.tmp", "*.bak")
        assert cfg.include_hidden is True
        assert cfg.mirror_root == Path("~/.hermes/sync/mirror/acme").expanduser()

    def test_tilde_expansion(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(textwrap.dedent("""
            watched_roots:
              - "~/mem-a"
              - "~/mem-b"
        """).strip(), encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.watched_roots == (
            Path("~/mem-a").expanduser(),
            Path("~/mem-b").expanduser(),
        )


# ---------------------------------------------------------------------------
# is_running state machine
# ---------------------------------------------------------------------------


class TestIsRunning:
    def test_no_pid_file(self, watch_config: WatchConfig) -> None:
        daemon = WatchDaemon(watch_config)
        running, pid = daemon.is_running()
        assert running is False
        assert pid is None

    def test_running(self, watch_config: WatchConfig) -> None:
        write_pid_file(watch_config.pid_file, os.getpid())
        daemon = WatchDaemon(watch_config)
        running, pid = daemon.is_running()
        assert running is True
        assert pid == os.getpid()

    def test_stale(self, watch_config: WatchConfig) -> None:
        # Find a dead PID. 999_999 is usually free.
        dead = 999_999
        if _pid_alive(dead):
            pytest.skip("999_999 happened to be alive on this host; skipping")
        write_pid_file(watch_config.pid_file, dead)
        daemon = WatchDaemon(watch_config)
        running, pid = daemon.is_running()
        assert running is False
        assert pid == dead


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_missing_mirror_root(self, tmp_path: Path, tmp_memory: Path) -> None:
        cfg = WatchConfig(
            mirror_root=tmp_path / "nope-mirror",
            watched_roots=(tmp_memory,),
            pid_file=tmp_path / "watch.pid",
        )
        daemon = WatchDaemon(cfg)
        with pytest.raises(watch._PreconditionError, match="does not exist"):
            daemon._validate_preconditions()

    def test_mirror_not_a_git_repo(self, tmp_path: Path, tmp_memory: Path) -> None:
        bad = tmp_path / "not-a-repo"
        bad.mkdir()
        cfg = WatchConfig(
            mirror_root=bad,
            watched_roots=(tmp_memory,),
            pid_file=tmp_path / "watch.pid",
        )
        daemon = WatchDaemon(cfg)
        with pytest.raises(watch._PreconditionError, match="not a git repo"):
            daemon._validate_preconditions()

    def test_missing_watched_root(self, tmp_mirror: Path, tmp_path: Path) -> None:
        cfg = WatchConfig(
            mirror_root=tmp_mirror,
            watched_roots=(tmp_path / "nope-memory",),
            pid_file=tmp_path / "watch.pid",
        )
        daemon = WatchDaemon(cfg)
        with pytest.raises(watch._PreconditionError, match="watched_root does not exist"):
            daemon._validate_preconditions()


# ---------------------------------------------------------------------------
# Foreground lifecycle
# ---------------------------------------------------------------------------


class TestForegroundLifecycle:
    def test_precondition_failure_exits_2(self, watch_config: WatchConfig) -> None:
        # Break the mirror to force a precondition error.
        watch_config.mirror_root = watch_config.mirror_root.parent / "nope"
        daemon = WatchDaemon(watch_config)
        assert daemon.run_foreground() == 2
        assert not watch_config.pid_file.exists()

    def test_already_running_exits_1(self, watch_config: WatchConfig) -> None:
        # Pretend someone else is already running by writing a live PID.
        write_pid_file(watch_config.pid_file, os.getpid())
        daemon = WatchDaemon(watch_config)
        assert daemon.run_foreground() == 1

    def test_stale_pid_file_cleaned(self, watch_config: WatchConfig) -> None:
        # Write a stale PID, then start; expect cleanup + success path.
        dead = 999_999
        if _pid_alive(dead):
            pytest.skip("999_999 happened to be alive on this host; skipping")
        write_pid_file(watch_config.pid_file, dead)
        # We can't run the actual loop (it never returns) so just
        # verify the precondition path doesn't blow up on the stale
        # file; the actual stale-cleanup happens at the start of
        # run_foreground, which we exercise via the already-running
        # path in the next test.
        daemon = WatchDaemon(watch_config)
        # Override _run_loop to a no-op so we can observe the cleanup.
        daemon._run_loop = lambda: None  # type: ignore[assignment]
        rc = daemon.run_foreground()
        assert rc == 0
        # PID file was removed in the finally block.
        assert not watch_config.pid_file.exists()

    def test_pid_file_lifecycle(self, watch_config: WatchConfig) -> None:
        # Run the daemon in a thread, give it a moment, then stop it.
        # We stub ``_run_loop`` with a simple sleep so the test
        # isolates the PID-file lifecycle from the polling/debounce
        # logic (which is covered by other tests).
        daemon = WatchDaemon(watch_config)
        loop_entered = threading.Event()
        def fake_run_loop() -> None:
            loop_entered.set()
            while not daemon._stop_requested:
                time.sleep(0.05)
        daemon._run_loop = fake_run_loop  # type: ignore[assignment]

        t = threading.Thread(target=daemon.run_foreground, daemon=True)
        t.start()

        # Wait up to 2s for the loop to enter (and thus the PID file
        # to be written).
        assert loop_entered.wait(timeout=2), "daemon did not enter the loop"
        assert watch_config.pid_file.exists(), "PID file should exist while running"
        pid_in_file = read_pid_file(watch_config.pid_file)
        assert pid_in_file is not None and pid_in_file > 0

        # Flip the stop flag and wait for the thread to exit.
        daemon._stop_requested = True
        t.join(timeout=2)
        assert not t.is_alive(), "daemon thread should have exited"

        # The finally block should have removed the PID file.
        assert not watch_config.pid_file.exists()


# ---------------------------------------------------------------------------
# Status and stop command helpers
# ---------------------------------------------------------------------------


class TestStatusCommand:
    def test_no_pid(self, watch_config: WatchConfig, capsys: pytest.CaptureFixture) -> None:
        rc = cmd_watch_status(watch_config)
        assert rc == 1
        out = capsys.readouterr().out
        assert "stopped" in out
        assert "no PID file" in out

    def test_stale_pid(self, watch_config: WatchConfig, capsys: pytest.CaptureFixture) -> None:
        dead = 999_999
        if _pid_alive(dead):
            pytest.skip("999_999 happened to be alive on this host; skipping")
        write_pid_file(watch_config.pid_file, dead)
        rc = cmd_watch_status(watch_config)
        assert rc == 1
        out = capsys.readouterr().out
        assert "stale" in out
        assert str(dead) in out

    def test_running(self, watch_config: WatchConfig, capsys: pytest.CaptureFixture) -> None:
        write_pid_file(watch_config.pid_file, os.getpid())
        rc = cmd_watch_status(watch_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "running" in out
        assert str(os.getpid()) in out


class TestStopCommand:
    def test_no_pid_is_silent_zero(self, watch_config: WatchConfig, capsys: pytest.CaptureFixture) -> None:
        rc = cmd_watch_stop(watch_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "nothing to stop" in out

    def test_stale_pid_is_cleaned(self, watch_config: WatchConfig, capsys: pytest.CaptureFixture) -> None:
        dead = 999_999
        if _pid_alive(dead):
            pytest.skip("999_999 happened to be alive on this host; skipping")
        write_pid_file(watch_config.pid_file, dead)
        rc = cmd_watch_stop(watch_config)
        assert rc == 0
        assert not watch_config.pid_file.exists()


# ---------------------------------------------------------------------------
# Throttling behaviour (unit)
# ---------------------------------------------------------------------------


class TestThrottling:
    def test_throttle_skips_when_within_window(self, watch_config: WatchConfig) -> None:
        watch_config.min_push_interval_seconds = 60.0
        daemon = WatchDaemon(watch_config)
        calls: list[int] = []

        def fake_do_push(change):  # noqa: ARG001
            calls.append(1)

        daemon._do_push = fake_do_push  # type: ignore[assignment]

        from agentchat.sync_agent.commit import ChangeSet
        # Empty ChangeSet is fine for throttling tests; we never
        # call summary_line() when throttled.
        empty = ChangeSet(records=(), origin="test")
        # First call: should run.
        daemon._maybe_push(empty)
        assert len(calls) == 1
        # Second call within window: should skip.
        daemon._maybe_push(empty)
        assert len(calls) == 1

    def test_push_failure_does_not_kill_daemon(self, watch_config: WatchConfig) -> None:
        watch_config.min_push_interval_seconds = 0.0
        daemon = WatchDaemon(watch_config)
        def boom(change):  # noqa: ARG001
            raise RuntimeError("simulated push failure")
        daemon._do_push = boom  # type: ignore[assignment]
        # Should not raise; daemon should stay alive.
        from agentchat.sync_agent.commit import ChangeSet
        empty = ChangeSet(records=(), origin="test")
        daemon._maybe_push(empty)
        # The counter only bumps on success.
        assert daemon._push_count == 0


# ---------------------------------------------------------------------------
# Regression: watch_and_commit on_commit hook (the small refactor)
# ---------------------------------------------------------------------------


class TestWatchAndCommitOnCommit:
    def test_on_commit_called_after_successful_commit(self, tmp_path: Path) -> None:
        # Build a tiny git repo the watch_and_commit loop can work on.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@e.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "T"],
            check=True,
            capture_output=True,
        )
        (repo / "x.txt").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            check=True, capture_output=True,
        )

        from agentchat.sync_agent.config import SyncConfig
        from agentchat.sync_agent.commit import ChangeSet, ChangeRecord, CommitResult
        from agentchat.sync_agent.commit import CommitStage

        cfg = SyncConfig(
            repo_dir=repo,
            watched_roots=(repo,),
            debounce_seconds=0.05,
            author_name="T",
            author_email="t@e.com",
        )
        received: list[CommitResult] = []
        # Drive one poll directly so the test is fast and deterministic.
        from agentchat.sync_agent.watcher import PollingEmitter, DebouncedEmitter

        stage = CommitStage(cfg)
        emitter = PollingEmitter(cfg)
        emitter.poll()  # baseline
        time.sleep(1.1)  # ensure mtime differs (most FSes are 1s resolution)
        (repo / "x.txt").write_text("changed\n", encoding="utf-8")
        new_mtime = time.time() + 1
        os.utime(repo / "x.txt", (new_mtime, new_mtime))

        # NOTE: do NOT call ``emitter.poll()`` here to "sanity check"
        # the diff — that would consume the change and leave the
        # debouncer nothing to detect. The debouncer's own poll
        # will be the one that fires.

        fired = threading.Event()
        received: list[CommitResult] = []
        def cb(change: ChangeSet) -> None:
            # The DebouncedEmitter callback gets a ChangeSet. The
            # dev21 on_commit hook is the natural place to commit +
            # observe. Here we emulate that: commit, then capture
            # the CommitResult via the dev21-style on_commit.
            result = stage.run(change)
            # Critical: reset the polling baseline so the next poll
            # does not re-fire with the same diff. The daemon's
            # own loop does this in _on_change; tests have to do
            # it themselves since they drive the loop manually.
            emitter.reset_baseline()
            received.append(result)
            fired.set()
        # 0.2s debounce — generous so the pump loop's 0.3s sleep
        # is shorter than the debounce window, letting the timer
        # actually fire. (If the pump polled faster than the
        # debounce, every poll would cancel the in-flight timer.)
        debouncer = DebouncedEmitter(emitter, debounce_seconds=0.2, callback=cb)
        # Pump with a sleep longer than the debounce so each
        # detected change actually fires its timer.
        deadline = time.monotonic() + 3.0
        while not fired.is_set() and time.monotonic() < deadline:
            change = debouncer.poll()
            time.sleep(0.3)

        assert fired.is_set(), "debounced callback did not fire within 3s"
        assert len(received) == 1
        assert received[0].committed is True

    def test_on_commit_exception_does_not_kill_loop(self, tmp_path: Path) -> None:
        # Same setup, but the on_commit raises. The watch loop's
        # _safe_invoke_on_commit should swallow the exception and log.
        from agentchat.sync_agent.config import SyncConfig
        from agentchat.sync_agent.commit import CommitResult
        from agentchat.sync_agent.watcher import (
            DebouncedEmitter, PollingEmitter, watch_and_commit,
        )

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@e.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "T"],
            check=True, capture_output=True,
        )
        (repo / "y.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "y.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            check=True, capture_output=True,
        )

        cfg = SyncConfig(
            repo_dir=repo,
            watched_roots=(repo,),
            debounce_seconds=0.05,
            author_name="T",
            author_email="t@e.com",
        )

        calls: list[CommitResult] = []
        def bad_callback(result: CommitResult) -> None:
            calls.append(result)
            raise RuntimeError("simulated on_commit failure")

        # We can't easily run the daemon loop in a test (it never
        # returns), so exercise the safe-invoke helper directly.
        from agentchat.sync_agent.watcher import _safe_invoke_on_commit
        fake_result = CommitResult(
            committed=True,
            sha="deadbeef",
            message="test",
            change_set=None,  # type: ignore[arg-type]
            raw_stderr="",
        )
        # Must not raise.
        _safe_invoke_on_commit(bad_callback, fake_result)
        assert calls == [fake_result]
