"""Unit tests for jobs/runner_remote_ts.py — SshHost ts_* methods are mocked."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unlimited_mcp.jobs.runner_remote_ts import RemoteTsRunner
from unlimited_mcp.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_host(
    *,
    name: str = "ssh:ubuntu@10.0.0.1:22",
    slot_id: int = 42,
    statuses: list[str] | None = None,
    output: bytes = b"remote output\n",
    exit_code: int = 0,
    submit_raises: Exception | None = None,
) -> MagicMock:
    """Build a mock SshHost with ts_* methods pre-configured."""
    host = MagicMock()
    host.name = name

    if submit_raises is not None:
        host.ts_submit.side_effect = submit_raises
    else:
        host.ts_submit.return_value = slot_id

    # ts_status cycles through statuses; last value is repeated.
    _statuses = statuses or ["running", "finished"]
    _iter = iter(_statuses)
    _last = _statuses[-1]

    def _status(*_a: object, **_kw: object) -> str:
        return next(_iter, _last)

    host.ts_status.side_effect = _status
    host.ts_output.return_value = output

    # run() is called to read the exit-code file.
    ec_result = MagicMock()
    ec_result.stdout = str(exit_code).encode()
    host.run.return_value = ec_result

    return host


def _runner(tmp_path: Path, host: MagicMock | None = None) -> tuple[RemoteTsRunner, MagicMock]:
    h = host or _mock_host()
    store = JobStore(tmp_path / "jobs")
    return RemoteTsRunner(h, store, poll_interval=0.05), h


def _wait(runner: RemoteTsRunner, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = runner.get_result(job_id)
        if r and r.status not in ("running", "queued"):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# Basic submission
# ---------------------------------------------------------------------------


def test_submit_returns_running(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.submit(["echo", "hi"])
    assert result.status == "running"
    assert result.job_id


def test_submit_calls_ts_submit(tmp_path: Path) -> None:
    r, host = _runner(tmp_path)
    r.submit(["my_cmd", "--flag"], label="test-label")
    host.ts_submit.assert_called_once()
    call_kwargs = host.ts_submit.call_args[1]
    assert call_kwargs["label"]  # label is set to job_id internally
    assert "exit_code_path" in call_kwargs


def test_slot_id_stored_in_slot_map(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(slot_id=7))
    result = r.submit(["cmd"])
    assert r._slot_map[result.job_id] == 7


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def test_completed_job_ok(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.submit(["cmd"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "completed"
    assert final.ok is True


def test_failed_job_nonzero_exit(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(exit_code=1, statuses=["finished"]))
    result = r.submit(["false"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert final.ok is False


def test_ts_failed_status_marks_failed(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(statuses=["failed"]))
    result = r.submit(["cmd"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"


def test_output_written_to_disk(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(output=b"hello from remote\n", statuses=["finished"]))
    result = r.submit(["cat"])
    _wait(r, result.job_id)
    final = r.get_result(result.job_id)
    assert final is not None and final.raw_output_ref
    assert b"hello from remote" in Path(final.raw_output_ref).read_bytes()


def test_polls_until_finished(tmp_path: Path) -> None:
    """Runner correctly polls through multiple 'running' states before 'finished'."""
    r, host = _runner(tmp_path, _mock_host(statuses=["running", "running", "running", "finished"]))
    result = r.submit(["cmd"])
    _wait(r, result.job_id)
    # ts_status should have been called at least 4 times
    assert host.ts_status.call_count >= 4
    final = r.get_result(result.job_id)
    assert final is not None and final.status == "completed"


# ---------------------------------------------------------------------------
# Submit failure (ts not installed, connection error, etc.)
# ---------------------------------------------------------------------------


def test_submit_raises_marks_failed_immediately(tmp_path: Path) -> None:
    host = _mock_host(submit_raises=RuntimeError("tsp not found"))
    r, _ = _runner(tmp_path, host)
    result = r.submit(["cmd"])
    # Should be failed immediately (no background thread needed)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "REMOTE_TS_SUBMIT_ERROR"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_marks_cancelled(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path, _mock_host(statuses=["running", "running", "finished"]))
    result = r.submit(["cmd"])
    cancelled = r.cancel(result.job_id)
    assert cancelled.status == "cancelled"


def test_cancel_calls_ts_cancel(tmp_path: Path) -> None:
    r, host = _runner(tmp_path, _mock_host(statuses=["running", "running", "finished"]))
    result = r.submit(["cmd"])
    r.cancel(result.job_id)
    host.ts_cancel.assert_called_once()


def test_cancel_unknown_job(tmp_path: Path) -> None:
    r, _ = _runner(tmp_path)
    result = r.cancel("nonexistent-job-id")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "JOB_NOT_FOUND"


def test_cancel_prevents_result_overwrite(tmp_path: Path) -> None:
    """Poller should not overwrite a cancelled status."""
    import threading

    barrier = threading.Barrier(2)

    def _slow_status(*_a: object, **_kw: object) -> str:
        barrier.wait(timeout=3)
        time.sleep(0.3)
        return "finished"

    host = _mock_host(statuses=["running"])
    host.ts_status.side_effect = _slow_status

    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.0)
    result = r.submit(["cmd"])

    barrier.wait(timeout=3)
    r.cancel(result.job_id)

    r.join_all(timeout=5)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "cancelled"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_marks_failed(tmp_path: Path) -> None:
    # ts_status always returns "running" — poller will hit deadline
    host = _mock_host(statuses=["running"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.05)
    result = r.submit(["sleep", "9999"], timeout_seconds=1)
    _wait(r, result.job_id, timeout=10)
    final = r.get_result(result.job_id)
    assert final is not None
    assert final.status == "failed"
    assert "timeout" in final.summary.lower()


def test_timeout_calls_ts_cancel(tmp_path: Path) -> None:
    host = _mock_host(statuses=["running"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.05)
    result = r.submit(["sleep", "9999"], timeout_seconds=1)
    _wait(r, result.job_id, timeout=10)
    host.ts_cancel.assert_called()


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
    ids = [jr.job_id for jr in r.list_results()]
    assert result.job_id in ids


# ---------------------------------------------------------------------------
# ts_socket passthrough
# ---------------------------------------------------------------------------


def test_ts_socket_passed_to_host(tmp_path: Path) -> None:
    host = _mock_host(statuses=["finished"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, ts_socket="/tmp/my.sock", poll_interval=0.05)
    result = r.submit(["cmd"])
    _wait(r, result.job_id)
    submit_kwargs = host.ts_submit.call_args[1]
    assert submit_kwargs.get("ts_socket") == "/tmp/my.sock"
    status_kwargs = host.ts_status.call_args[1]
    assert status_kwargs.get("ts_socket") == "/tmp/my.sock"


# ---------------------------------------------------------------------------
# Prompt file and stdin content
# ---------------------------------------------------------------------------


def test_prompt_file_uploaded_and_substituted(tmp_path: Path) -> None:
    """prompt_file_content is uploaded via SFTP and {prompt_file} replaced in argv."""
    host = _mock_host(statuses=["finished"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.05)
    result = r.submit(["{prompt_file}", "--run"], prompt_file_content="do the task")
    _wait(r, result.job_id)

    host.sftp_put.assert_called()
    upload_path, upload_content = host.sftp_put.call_args_list[0][0]
    assert "prompt.txt" in upload_path
    assert upload_content == b"do the task"

    # ts_submit argv should have {prompt_file} replaced with the remote path.
    submitted_argv = host.ts_submit.call_args[0][0]
    assert submitted_argv[0] == upload_path
    assert "{prompt_file}" not in submitted_argv


def test_stdin_content_uploaded_and_piped(tmp_path: Path) -> None:
    """stdin_content is uploaded via SFTP and piped via ts_submit stdin_file."""
    host = _mock_host(statuses=["finished"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.05)
    result = r.submit(["myagent"], stdin_content="task instructions here")
    _wait(r, result.job_id)

    put_calls = host.sftp_put.call_args_list
    stdin_uploads = [(p, c) for p, c in (c[0] for c in put_calls) if "stdin" in p]
    assert stdin_uploads, "Expected an sftp_put for stdin content"
    assert stdin_uploads[0][1] == b"task instructions here"

    ts_kwargs = host.ts_submit.call_args[1]
    assert ts_kwargs.get("stdin_file") is not None
    assert "stdin" in ts_kwargs["stdin_file"]


# ---------------------------------------------------------------------------
# max_slots — remote ts -S configured on first submit
# ---------------------------------------------------------------------------


def test_max_slots_configures_remote_on_first_submit(tmp_path: Path) -> None:
    host = _mock_host(statuses=["finished"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, max_slots=3, poll_interval=0.05)
    r.submit(["cmd"])
    # _find_remote_ts_bin is called; then ts -S 3; then ts_submit
    bin_call = host._find_remote_ts_bin.call_args_list
    assert len(bin_call) >= 1
    run_calls = host.run.call_args_list
    slot_calls = [c for c in run_calls if "-S" in (c[0][0] if c[0] else [])]
    assert slot_calls, "Expected a ts -S <n> call for slot configuration"
    assert "3" in slot_calls[0][0][0]


# ---------------------------------------------------------------------------
# clarify_rounds Q&A sync
# ---------------------------------------------------------------------------


def test_clarify_sync_downloads_question_file(tmp_path: Path) -> None:
    """Poller downloads question file from remote to local questions_dir."""
    import json as _json
    from unlimited_mcp.jobs.store import JobStore

    questions_payload = _json.dumps([{"id": 1, "question": "Which approach?"}]).encode()

    host = _mock_host(statuses=["running", "finished"])
    # host.ts_submit / ts_status / ts_output are mocked by _mock_host.
    # host.run is called only by _sync_clarify (ls) and _poll (cat/rm ec_path).
    # _sync_clarify runs once per poll iteration (running + finished = 2 ls calls).
    host.run.side_effect = [
        MagicMock(stdout=b"/tmp/q/round_001_questions.json\n"),  # ls (running iteration)
        MagicMock(stdout=b"/tmp/q/round_001_questions.json\n"),  # ls (finished iteration)
        MagicMock(stdout=b"0"),                                   # cat ec_path
        MagicMock(stdout=b""),                                    # rm ec_path
    ]
    host.sftp_get.return_value = questions_payload
    host.sftp_exists.return_value = False  # no answers yet

    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.05)
    result = r.submit(["agent", "run"], remote_questions_dir="/tmp/q")
    _wait(r, result.job_id)

    local_q = store.questions_dir(result.job_id) / "round_001_questions.json"
    assert local_q.exists()
    assert _json.loads(local_q.read_bytes())[0]["id"] == 1


def test_clarify_sync_uploads_answer_file(tmp_path: Path) -> None:
    """Poller uploads answers written locally to the remote questions dir."""
    import json as _json
    from unlimited_mcp.jobs.store import JobStore

    host = _mock_host(statuses=["running", "finished"])
    host.run.side_effect = [
        MagicMock(stdout=b"ts\n"),   # _find_remote_ts_bin
        MagicMock(stdout=b"2\n"),    # ts_submit
        MagicMock(stdout=b""),       # ls — no question files yet
        MagicMock(stdout=b"0"),      # cat ec_path
        MagicMock(stdout=b""),       # rm ec_path
    ]
    host.sftp_exists.return_value = False  # answer not yet uploaded

    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, poll_interval=0.05)
    result = r.submit(["cmd"], remote_questions_dir="/remote/q")

    # Write a local answer before the poll fires.
    q_dir = store.questions_dir(result.job_id)
    q_dir.mkdir(parents=True, exist_ok=True)
    answer_file = q_dir / "round_001_answers.json"
    answer_file.write_text(_json.dumps({"answers": [{"id": 1, "answer": "A"}]}))

    _wait(r, result.job_id)

    # sftp_put should have been called with the answer content.
    put_calls = host.sftp_put.call_args_list
    assert any("round_001_answers.json" in str(c) for c in put_calls)


def test_max_slots_configured_only_once(tmp_path: Path) -> None:
    host = _mock_host(statuses=["finished"])
    store = JobStore(tmp_path / "jobs")
    r = RemoteTsRunner(host, store, max_slots=2, poll_interval=0.05)
    result1 = r.submit(["cmd1"])
    result2 = r.submit(["cmd2"])
    _wait(r, result1.job_id)
    _wait(r, result2.job_id)
    run_calls = host.run.call_args_list
    slot_calls = [c for c in run_calls if "-S" in (c[0][0] if c[0] else [])]
    assert len(slot_calls) == 1, "ts -S should only be called once across multiple submits"
