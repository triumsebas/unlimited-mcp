"""Unit tests for hosts/base.py (RunOutput, Host protocol) and hosts/local.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from unlimited_mcp.hosts import Host, LocalHost, RunOutput
from unlimited_mcp.safety.redactor import Redactor

# ---------------------------------------------------------------------------
# RunOutput
# ---------------------------------------------------------------------------


def test_run_output_is_frozen() -> None:
    out = RunOutput(
        stdout=b"hi",
        stderr=b"",
        exit_code=0,
        duration_ms=5,
        output_truncated=False,
        output_bytes=2,
    )
    with pytest.raises((AttributeError, TypeError)):
        out.exit_code = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_local_host_satisfies_protocol() -> None:
    host = LocalHost()
    assert isinstance(host, Host)


def test_local_host_name() -> None:
    assert LocalHost().name == "local"


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


def test_run_echo(tmp_path: Path) -> None:
    host = LocalHost()
    out = host.run([sys.executable, "-c", "print('hello')"])
    assert out.exit_code == 0
    assert b"hello" in out.stdout
    assert out.output_truncated is False
    assert out.output_bytes == len(out.stdout) + len(out.stderr)


def test_run_nonzero_exit() -> None:
    host = LocalHost()
    out = host.run([sys.executable, "-c", "raise SystemExit(42)"])
    assert out.exit_code == 42


def test_run_stderr_captured() -> None:
    host = LocalHost()
    out = host.run([sys.executable, "-c", "import sys; sys.stderr.write('err\\n')"])
    assert b"err" in out.stderr
    assert out.stdout == b""


# ---------------------------------------------------------------------------
# env_extra
# ---------------------------------------------------------------------------


def test_run_env_extra() -> None:
    host = LocalHost()
    out = host.run(
        [sys.executable, "-c", "import os; print(os.environ['MY_VAR'])"],
        env_extra={"MY_VAR": "sentinel_value"},
    )
    assert b"sentinel_value" in out.stdout


def test_run_env_extra_overrides() -> None:
    import os

    os.environ["_TEST_OVERRIDE"] = "original"
    try:
        host = LocalHost()
        out = host.run(
            [sys.executable, "-c", "import os; print(os.environ['_TEST_OVERRIDE'])"],
            env_extra={"_TEST_OVERRIDE": "overridden"},
        )
        assert b"overridden" in out.stdout
    finally:
        os.environ.pop("_TEST_OVERRIDE", None)


# ---------------------------------------------------------------------------
# cwd
# ---------------------------------------------------------------------------


def test_run_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x")
    host = LocalHost()
    out = host.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(tmp_path),
    )
    assert str(tmp_path).encode() in out.stdout


# ---------------------------------------------------------------------------
# stdout_path / stderr_path (JobStore integration)
# ---------------------------------------------------------------------------


def test_run_writes_stdout_path(tmp_path: Path) -> None:
    stdout_file = tmp_path / "stdout.log"
    host = LocalHost()
    host.run(
        [sys.executable, "-c", "print('written to disk')"],
        stdout_path=stdout_file,
    )
    assert stdout_file.exists()
    assert b"written to disk" in stdout_file.read_bytes()


def test_run_writes_stderr_path(tmp_path: Path) -> None:
    stderr_file = tmp_path / "stderr.log"
    host = LocalHost()
    host.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('err line\\n')"],
        stderr_path=stderr_file,
    )
    assert b"err line" in stderr_file.read_bytes()


def test_run_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "stdout.log"
    host = LocalHost()
    host.run([sys.executable, "-c", "print('x')"], stdout_path=nested)
    assert nested.exists()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_run_redacts_stdout() -> None:
    secret = "supersecrettoken123"
    redactor = Redactor(environ={"MY_TOKEN": secret})
    host = LocalHost(redactor=redactor)
    out = host.run(
        [sys.executable, "-c", f"print('{secret}')"],
    )
    assert secret.encode() not in out.stdout
    assert b"***REDACTED***" in out.stdout


def test_run_redacts_written_file(tmp_path: Path) -> None:
    secret = "supersecrettoken123"
    stdout_file = tmp_path / "stdout.log"
    redactor = Redactor(environ={"MY_TOKEN": secret})
    host = LocalHost(redactor=redactor)
    host.run(
        [sys.executable, "-c", f"print('{secret}')"],
        stdout_path=stdout_file,
    )
    assert secret.encode() not in stdout_file.read_bytes()
    assert b"***REDACTED***" in stdout_file.read_bytes()


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


def test_run_truncates_large_output() -> None:
    host = LocalHost()
    # generate ~500 bytes per stream; limit to 100 bytes combined
    out = host.run(
        [sys.executable, "-c", "print('A' * 300)"],
        output_limit_bytes=100,
    )
    assert out.output_truncated is True
    assert len(out.stdout) + len(out.stderr) <= 100


def test_run_no_truncation_within_limit() -> None:
    host = LocalHost()
    out = host.run(
        [sys.executable, "-c", "print('hi')"],
        output_limit_bytes=1_000_000,
    )
    assert out.output_truncated is False


def test_run_output_bytes_reflects_pre_truncation_size() -> None:
    host = LocalHost()
    out = host.run(
        [sys.executable, "-c", "print('B' * 300)"],
        output_limit_bytes=50,
    )
    # output_bytes is the pre-truncation combined size
    assert out.output_bytes > 50


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_run_timeout_raises() -> None:
    host = LocalHost()
    with pytest.raises(subprocess.TimeoutExpired):
        host.run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=1,
        )


# ---------------------------------------------------------------------------
# duration_ms
# ---------------------------------------------------------------------------


def test_run_duration_ms_is_positive() -> None:
    host = LocalHost()
    out = host.run([sys.executable, "-c", "pass"])
    assert out.duration_ms >= 0
