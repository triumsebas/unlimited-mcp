"""Unit tests for jobs/runner_remote.py — Host.run() is mocked."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unlimited_mcp.hosts.base import RunOutput
from unlimited_mcp.jobs.runner_remote import RemoteRunner
from unlimited_mcp.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_host(
    stdout: bytes = b"ok\n",
    stderr: bytes = b"",
    exit_code: int = 0,
    name: str = "ssh:ubuntu@10.0.0.1:22",
    side_effect: Exception | None = None,
) -> MagicMock:
    host = MagicMock()
    host.name = name
    if side_effect is not None:
        host.run.side_effect = side_effect
    else:
        host.run.return_value = RunOutput(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=10,
            output_truncated=False,
            output_bytes=len(stdout) + len(stderr),
        )
    return host


def _runner(tmp_path: Path, host: MagicMock | None = None) -> tuple[RemoteRunner, MagicMock]:
    h = host or _mock_host()
    store = JobStore(tmp_path / "jobs")
    return RemoteRunner(h, store), h


def _wait(runner: RemoteRunner, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = runner.get_result(job_id)
        if r and r.status not in ("running", "queued"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


def test_submit_returns_running(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.submit(["echo", "hi"])
    assert result.status == "running"
    assert result.job_id


def test_completed_job_ok(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.submit(["echo", "hi"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "completed"
    assert final.ok is True


def test_failed_job_nonzero_exit(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(exit_code=1, stderr=b"oops\n"))
    result = r.submit(["false"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert final.ok is False


def test_timeout_marks_failed(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(side_effect=subprocess.TimeoutExpired(["cmd"], 1)))
    result = r.submit(["sleep", "10"], timeout_seconds=1)
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert "timeout" in final.summary.lower()


def test_exception_marks_failed(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(side_effect=ConnectionError("refused")))
    result = r.submit(["cmd"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert final.error is not None
    assert final.error.code == "REMOTE_EXEC_ERROR"


def test_host_name_in_summary(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(name="ssh:admin@gpu:22"))
    result = r.submit(["cmd"], label="train")
    assert "ssh:admin@gpu:22" in result.summary or "train" in result.summary


# ---------------------------------------------------------------------------
# stdout/stderr written to disk
# ---------------------------------------------------------------------------


def test_stdout_written_to_disk(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(stdout=b"hello remote\n"))
    result = r.submit(["cat"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None and final.raw_output_ref
    assert b"hello remote" in Path(final.raw_output_ref).read_bytes()


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_marks_cancelled(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.submit(["cmd"])
    cancelled = r.cancel(result.job_id)
    assert cancelled.status == "cancelled"


def test_cancel_unknown_job(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.cancel("nonexistent-job-id")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "JOB_NOT_FOUND"


def test_cancel_prevents_result_overwrite(tmp_path: Path) -> None:
    """If cancel fires before the thread writes its result, status stays cancelled."""
    import threading

    barrier = threading.Barrier(2)

    def _slow_run(*_a: object, **_kw: object) -> RunOutput:
        barrier.wait(timeout=3)
        time.sleep(0.2)
        return RunOutput(stdout=b"", stderr=b"", exit_code=0,
                         duration_ms=200, output_truncated=False, output_bytes=0)

    host = MagicMock()
    host.name = "ssh:test"
    host.run.side_effect = _slow_run

    store = JobStore(tmp_path / "jobs")
    r = RemoteRunner(host, store)
    result = r.submit(["cmd"])

    barrier.wait(timeout=3)
    r.cancel(result.job_id)

    r.join_all(timeout=5)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "cancelled"


# ---------------------------------------------------------------------------
# Unsupported features
# ---------------------------------------------------------------------------


def test_stdin_content_passed_to_host(tmp_path: Path) -> None:
    r, host = _runner(tmp_path)
    r.submit(["cmd"], stdin_content="hello from orchestrator")
    r.join_all(timeout=5)
    call_kwargs = host.run.call_args[1]
    assert call_kwargs.get("stdin_content") == b"hello from orchestrator"


def test_prompt_file_uploaded_and_substituted(tmp_path: Path) -> None:
    from unlimited_mcp.hosts.ssh import SshHost
    from unittest.mock import MagicMock, patch

    host = _mock_host()
    # Make it look like an SshHost so the isinstance check passes.
    host.__class__ = SshHost

    store = JobStore(tmp_path / "jobs")
    from unlimited_mcp.jobs.runner_remote import RemoteRunner
    r = RemoteRunner(host, store)
    r.submit(["{prompt_file}", "--flag"], prompt_file_content="my task here")
    r.join_all(timeout=5)

    host.sftp_put.assert_called_once()
    upload_path, upload_content = host.sftp_put.call_args[0]
    assert "prompt.txt" in upload_path
    assert upload_content == b"my task here"

    # The first host.run call should have {prompt_file} replaced in argv.
    first_run_argv = host.run.call_args_list[0][0][0]
    assert first_run_argv[0] == upload_path
    assert "{prompt_file}" not in first_run_argv


# ---------------------------------------------------------------------------
# list_results / get_result
# ---------------------------------------------------------------------------


def test_get_result_unknown_returns_none(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    assert r.get_result("no-such-job") is None


def test_list_results_includes_submitted(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.submit(["cmd"])
    _wait(r, result.job_id)
    all_results = r.list_results()
    ids = [jr.job_id for jr in all_results]
    assert result.job_id in ids


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_key_returns_same_job(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    r1 = r.submit(["cmd"], idempotency_key="my-key")
    r2 = r.submit(["cmd"], idempotency_key="my-key")
    assert r1.job_id == r2.job_id


def test_idempotency_key_resubmits_after_failure(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(exit_code=1))
    r1 = r.submit(["cmd"], idempotency_key="retry-key")
    _wait(r, r1.job_id)
    # After failure, same key should produce a new job.
    r2 = r.submit(["cmd"], idempotency_key="retry-key")
    assert r2.job_id != r1.job_id
