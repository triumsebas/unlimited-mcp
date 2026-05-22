"""Unit tests for SshHost task-spooler helpers (ts_* methods)."""

from __future__ import annotations

from unittest.mock import MagicMock

from unlimited_mcp.config.schema import SshHostConfig
from unlimited_mcp.hosts.base import RunOutput
from unlimited_mcp.hosts.ssh import SshHost

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**kwargs: object) -> SshHostConfig:
    defaults = dict(type="ssh", user="ubuntu", host="10.0.0.1", port=22)
    return SshHostConfig(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _run_output(stdout: bytes = b"", exit_code: int = 0) -> RunOutput:
    return RunOutput(
        stdout=stdout,
        stderr=b"",
        exit_code=exit_code,
        duration_ms=1,
        output_truncated=False,
        output_bytes=len(stdout),
    )


def _patched_host(**run_outputs: RunOutput) -> tuple[SshHost, MagicMock]:
    """Return SshHost with run() mocked to return a sequence of RunOutputs."""
    host = SshHost(_config())
    mock_run = MagicMock(side_effect=list(run_outputs.values()) if run_outputs else None)
    host.run = mock_run
    return host, mock_run


# ---------------------------------------------------------------------------
# _find_remote_ts_bin
# ---------------------------------------------------------------------------


def test_find_remote_ts_bin_tsp(tmp_path: object) -> None:
    host = SshHost(_config())
    host.run = MagicMock(return_value=_run_output(b"/usr/bin/tsp\n"))
    assert host._find_remote_ts_bin() == "/usr/bin/tsp"


def test_find_remote_ts_bin_ts(tmp_path: object) -> None:
    host = SshHost(_config())
    host.run = MagicMock(return_value=_run_output(b"ts\n"))
    assert host._find_remote_ts_bin() == "ts"


def test_find_remote_ts_bin_cached(tmp_path: object) -> None:
    host = SshHost(_config())
    host.run = MagicMock(return_value=_run_output(b"tsp\n"))
    host._find_remote_ts_bin()
    host._find_remote_ts_bin()
    # run() should only be called once — result is cached.
    assert host.run.call_count == 1


def test_find_remote_ts_bin_cache_reset_on_reconnect() -> None:
    host = SshHost(_config())
    host.run = MagicMock(return_value=_run_output(b"tsp\n"))
    host._find_remote_ts_bin()
    assert host._ts_bin_cache == "tsp"
    host._ts_bin_cache = None  # simulate reconnect reset
    assert host._ts_bin_cache is None


# ---------------------------------------------------------------------------
# ts_submit
# ---------------------------------------------------------------------------


def test_ts_submit_returns_slot_id() -> None:
    host = SshHost(_config())
    # _find_remote_ts_bin call + ts submit call
    host.run = MagicMock(
        side_effect=[
            _run_output(b"tsp\n"),  # _find_remote_ts_bin
            _run_output(b"5\n"),  # ts -L label sh -c ...
        ]
    )
    slot = host.ts_submit(["echo", "hi"], label="myjob")
    assert slot == 5


def test_ts_submit_uses_cwd_and_env() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "tsp"
    host.run = MagicMock(return_value=_run_output(b"3\n"))
    host.ts_submit(
        ["myapp"],
        label="job1",
        cwd="/opt/work",
        env_extra={"FOO": "bar"},
    )
    cmd_arg = host.run.call_args[0][0]
    # argv passed to run() is: ["tsp", "-L", label, "sh", "-c", inner_cmd]
    inner = cmd_arg[-1]
    assert "cd /opt/work" in inner
    assert "export FOO=bar" in inner
    assert "myapp" in inner


def test_ts_submit_exit_code_path_in_inner_cmd() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"1\n"))
    host.ts_submit(["cmd"], label="job", exit_code_path="/tmp/ec.txt")
    inner = host.run.call_args[0][0][-1]
    assert "/tmp/ec.txt" in inner
    assert "_ec=$?" in inner


def test_ts_submit_passes_ts_socket() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"2\n"))
    host.ts_submit(["cmd"], label="job", ts_socket="/tmp/my.sock")
    env_kwarg = host.run.call_args[1].get("env_extra", {})
    assert env_kwarg.get("TS_SOCKET") == "/tmp/my.sock"


# ---------------------------------------------------------------------------
# ts_status
# ---------------------------------------------------------------------------


def test_ts_status_finished() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"finished\n"))
    assert host.ts_status(3) == "finished"


def test_ts_status_running() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"running\n"))
    assert host.ts_status(3) == "running"


def test_ts_status_skipped_normalized_to_failed() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"skipped\n"))
    assert host.ts_status(3) == "failed"


def test_ts_status_passes_socket() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"queued\n"))
    host.ts_status(1, ts_socket="/tmp/s.sock")
    env = host.run.call_args[1].get("env_extra", {})
    assert env.get("TS_SOCKET") == "/tmp/s.sock"


# ---------------------------------------------------------------------------
# ts_output
# ---------------------------------------------------------------------------


def test_ts_output_cats_file() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(
        side_effect=[
            _run_output(b"/tmp/ts-out-abc\n"),  # ts -o slot
            _run_output(b"hello output\n"),  # cat /tmp/ts-out-abc
        ]
    )
    out = host.ts_output(5)
    assert out == b"hello output\n"
    # Second call should be cat of the path returned by ts -o
    cat_args = host.run.call_args_list[1][0][0]
    assert cat_args[0] == "cat"
    assert "/tmp/ts-out-abc" in cat_args[1]


def test_ts_output_empty_path_returns_empty() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b"\n"))
    out = host.ts_output(5)
    assert out == b""
    # Only one run() call — no cat if path is empty
    assert host.run.call_count == 1


# ---------------------------------------------------------------------------
# ts_cancel
# ---------------------------------------------------------------------------


def test_ts_cancel_calls_kill_and_remove() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(return_value=_run_output(b""))
    host.ts_cancel(7)
    calls = [c[0][0] for c in host.run.call_args_list]
    flags = {c[1] for c in calls}  # second element is the flag (-k or -r)
    assert "-k" in flags
    assert "-r" in flags


def test_ts_cancel_ignores_errors() -> None:
    host = SshHost(_config())
    host._ts_bin_cache = "ts"
    host.run = MagicMock(side_effect=RuntimeError("SSH error"))
    # Should not raise even when run() fails
    host.ts_cancel(7)
