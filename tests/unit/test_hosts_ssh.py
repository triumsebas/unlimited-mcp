"""Unit tests for hosts/ssh.py — all SSH I/O is mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unlimited_mcp.config.schema import SshHostConfig
from unlimited_mcp.hosts import Host, SshHost
from unlimited_mcp.safety.redactor import Redactor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**kwargs: object) -> SshHostConfig:
    defaults = dict(type="ssh", user="ubuntu", host="192.168.1.1", port=22)
    return SshHostConfig(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _mock_channel(stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0) -> MagicMock:
    """Return a mock paramiko exec_command triple (stdin, stdout, stderr)."""
    channel = MagicMock()
    channel.recv_exit_status.return_value = exit_code

    stdout_file = MagicMock()
    stdout_file.read.return_value = stdout
    stdout_file.channel = channel

    stderr_file = MagicMock()
    stderr_file.read.return_value = stderr

    stdin_file = MagicMock()

    return stdin_file, stdout_file, stderr_file


def _patched_host(
    config: SshHostConfig | None = None,
    redactor: Redactor | None = None,
    *,
    stdout: bytes = b"hello\n",
    stderr: bytes = b"",
    exit_code: int = 0,
) -> tuple[SshHost, MagicMock]:
    """Build an SshHost with a mocked paramiko client injected."""
    cfg = config or _config()
    host = SshHost(cfg, redactor)

    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_transport.is_active.return_value = True
    mock_client.get_transport.return_value = mock_transport
    mock_client.exec_command.return_value = _mock_channel(stdout, stderr, exit_code)

    host._client = mock_client
    return host, mock_client


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_ssh_host_satisfies_protocol() -> None:
    host = SshHost(_config())
    assert isinstance(host, Host)


def test_ssh_host_name() -> None:
    host = SshHost(_config(host="10.0.0.1", port=2222, user="admin"))
    assert host.name == "ssh:admin@10.0.0.1:2222"


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


def test_run_returns_stdout() -> None:
    host, _ = _patched_host(stdout=b"world\n")
    out = host.run(["echo", "world"])
    assert out.stdout == b"world\n"
    assert out.exit_code == 0


def test_run_returns_stderr() -> None:
    host, _ = _patched_host(stderr=b"oops\n")
    out = host.run(["ls", "/nope"])
    assert out.stderr == b"oops\n"


def test_run_nonzero_exit() -> None:
    host, _ = _patched_host(exit_code=1)
    out = host.run(["false"])
    assert out.exit_code == 1


def test_run_duration_ms_positive() -> None:
    host, _ = _patched_host()
    out = host.run(["true"])
    assert out.duration_ms >= 0


def test_run_output_bytes_reflects_pre_truncation() -> None:
    big = b"A" * 300
    host, _ = _patched_host(stdout=big)
    out = host.run(["cat", "file"], output_limit_bytes=50)
    assert out.output_bytes == 300
    assert out.output_truncated is True


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_run_builds_cwd_prefix() -> None:
    host, mock_client = _patched_host()
    host.run(["ls"], cwd="/tmp/work")
    cmd = mock_client.exec_command.call_args[0][0]
    assert cmd.startswith("cd /tmp/work")


def test_run_builds_env_prefix() -> None:
    host, mock_client = _patched_host()
    host.run(["printenv", "FOO"], env_extra={"FOO": "bar"})
    cmd = mock_client.exec_command.call_args[0][0]
    assert "export FOO=bar" in cmd


def test_run_quotes_special_chars_in_argv() -> None:
    host, mock_client = _patched_host()
    host.run(["echo", "hello world"])
    cmd = mock_client.exec_command.call_args[0][0]
    assert "'hello world'" in cmd


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_run_redacts_stdout() -> None:
    secret = "topsecret99"
    redactor = Redactor(environ={"MY_TOKEN": secret})
    host, _ = _patched_host(redactor=redactor, stdout=secret.encode() + b"\n")
    out = host.run(["cat"])
    assert secret.encode() not in out.stdout
    assert b"***REDACTED***" in out.stdout


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_run_truncates_when_over_limit() -> None:
    host, _ = _patched_host(stdout=b"X" * 200, stderr=b"Y" * 200)
    out = host.run(["cat"], output_limit_bytes=100)
    assert out.output_truncated is True
    assert len(out.stdout) + len(out.stderr) <= 100


def test_run_no_truncation_within_limit() -> None:
    host, _ = _patched_host(stdout=b"hi\n")
    out = host.run(["echo", "hi"], output_limit_bytes=1_000_000)
    assert out.output_truncated is False


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def test_run_writes_stdout_path(tmp_path: Path) -> None:
    p = tmp_path / "out.log"
    host, _ = _patched_host(stdout=b"written\n")
    host.run(["echo"], stdout_path=p)
    assert p.read_bytes() == b"written\n"


def test_run_writes_stderr_path(tmp_path: Path) -> None:
    p = tmp_path / "err.log"
    host, _ = _patched_host(stderr=b"err\n")
    host.run(["cmd"], stderr_path=p)
    assert p.read_bytes() == b"err\n"


def test_run_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "out.log"
    host, _ = _patched_host(stdout=b"x")
    host.run(["cmd"], stdout_path=p)
    assert p.exists()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_run_timeout_raises(tmp_path: Path) -> None:
    host = SshHost(_config())
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_transport.is_active.return_value = True
    mock_client.get_transport.return_value = mock_transport

    # stdout.read() blocks forever — simulate with a slow mock via side_effect
    def _slow_read() -> bytes:
        import time

        time.sleep(10)
        return b""

    stdin_m = MagicMock()
    stdout_m = MagicMock()
    stdout_m.read.side_effect = _slow_read
    stdout_m.channel = MagicMock()
    stderr_m = MagicMock()
    stderr_m.read.return_value = b""
    mock_client.exec_command.return_value = (stdin_m, stdout_m, stderr_m)

    host._client = mock_client

    with pytest.raises(subprocess.TimeoutExpired):
        host.run(["sleep", "10"], timeout_seconds=1)


# ---------------------------------------------------------------------------
# Connection pooling
# ---------------------------------------------------------------------------


def test_reconnects_when_transport_inactive() -> None:
    cfg = _config()
    host = SshHost(cfg)

    dead_transport = MagicMock()
    dead_transport.is_active.return_value = False

    dead_client = MagicMock()
    dead_client.get_transport.return_value = dead_transport
    host._client = dead_client

    stdin_m, stdout_m, stderr_m = _mock_channel(b"ok")
    new_client = MagicMock()
    new_transport = MagicMock()
    new_transport.is_active.return_value = True
    new_client.get_transport.return_value = new_transport
    new_client.exec_command.return_value = (stdin_m, stdout_m, stderr_m)

    with patch.object(host, "_connect", return_value=new_client) as mock_connect:
        host.run(["true"])
        mock_connect.assert_called_once()
        dead_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Auth: passphrase from env var
# ---------------------------------------------------------------------------


def test_passphrase_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PASS", "s3cr3t")
    cfg = _config(key_passphrase_env="MY_PASS")
    host = SshHost(cfg)
    assert host._resolve_passphrase() == "s3cr3t"


def test_passphrase_env_missing_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing env var is non-fatal: returns None and records the reason
    so the connection can still fall back to the ssh-agent."""
    monkeypatch.delenv("NO_SUCH_VAR", raising=False)
    cfg = _config(key_passphrase_env="NO_SUCH_VAR")
    host = SshHost(cfg)
    assert host._resolve_passphrase() is None
    assert host._passphrase_missing_reason is not None
    assert "NO_SUCH_VAR" in host._passphrase_missing_reason


def test_no_passphrase_fields_returns_none() -> None:
    host = SshHost(_config())
    assert host._resolve_passphrase() is None


# ---------------------------------------------------------------------------
# Auth: passphrase from keyring
# ---------------------------------------------------------------------------


def test_passphrase_from_keyring_account_falls_back_to_user() -> None:
    """Without key_file, the keyring account defaults to the SSH user."""
    cfg = _config(key_passphrase_keyring="unlimited-mcp-ssh")
    host = SshHost(cfg)

    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = "kr_pass"

    with patch.dict("sys.modules", {"keyring": mock_keyring}):
        result = host._resolve_passphrase()

    mock_keyring.get_password.assert_called_once_with("unlimited-mcp-ssh", "ubuntu")
    assert result == "kr_pass"


def test_passphrase_keyring_account_defaults_to_key_basename() -> None:
    """With key_file set, the keyring account defaults to its basename,
    so several hosts sharing one key reuse a single keychain entry."""
    cfg = _config(
        key_passphrase_keyring="unlimited-mcp-ssh",
        key_file="~/.ssh/id_rsa",
    )
    host = SshHost(cfg)

    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = "kr_pass"

    with patch.dict("sys.modules", {"keyring": mock_keyring}):
        result = host._resolve_passphrase()

    mock_keyring.get_password.assert_called_once_with("unlimited-mcp-ssh", "id_rsa")
    assert result == "kr_pass"


def test_passphrase_keyring_account_explicit_override() -> None:
    """key_passphrase_account wins over both key_file basename and user."""
    cfg = _config(
        key_passphrase_keyring="unlimited-mcp-ssh",
        key_file="~/.ssh/id_rsa",
        key_passphrase_account="shared",
    )
    host = SshHost(cfg)

    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = "kr_pass"

    with patch.dict("sys.modules", {"keyring": mock_keyring}):
        host._resolve_passphrase()

    mock_keyring.get_password.assert_called_once_with("unlimited-mcp-ssh", "shared")


def test_passphrase_keyring_not_found_is_non_fatal() -> None:
    """A missing keyring entry is non-fatal: returns None and records the
    reason so the connection can still fall back to the ssh-agent."""
    cfg = _config(
        key_passphrase_keyring="unlimited-mcp-ssh",
        key_file="~/.ssh/id_rsa",
    )
    host = SshHost(cfg)

    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = None

    with patch.dict("sys.modules", {"keyring": mock_keyring}):
        assert host._resolve_passphrase() is None

    assert host._passphrase_missing_reason is not None
    assert "id_rsa" in host._passphrase_missing_reason


# ---------------------------------------------------------------------------
# key_file controls look_for_keys and is required with passphrase
# ---------------------------------------------------------------------------


def _mock_paramiko() -> tuple[MagicMock, MagicMock]:
    """Return (mock_paramiko_module, mock_client) ready for sys.modules injection."""
    mock_client = MagicMock()
    mock_mod = MagicMock()
    mock_mod.SSHClient.return_value = mock_client
    mock_mod.RejectPolicy = MagicMock
    return mock_mod, mock_client


def test_passphrase_without_key_file_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passphrase configured but key_file missing → clear error at connect time."""
    monkeypatch.setenv("MY_PASS", "secret")
    cfg = _config(key_passphrase_env="MY_PASS")  # no key_file
    host = SshHost(cfg)
    # Validation runs before paramiko import — no need to mock paramiko.
    with pytest.raises(RuntimeError, match="key_file"):
        host._connect()


def test_explicit_key_file_disables_look_for_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """When key_file is set, look_for_keys=False so only that key is tried."""
    monkeypatch.setenv("MY_PASS", "secret")
    cfg = _config(key_file="~/.ssh/my_custom_key", key_passphrase_env="MY_PASS")
    host = SshHost(cfg)

    mock_mod, mock_client = _mock_paramiko()
    with patch.dict("sys.modules", {"paramiko": mock_mod}):
        host._connect()

    call_kwargs = mock_client.connect.call_args[1]
    assert call_kwargs["look_for_keys"] is False
    assert "my_custom_key" in call_kwargs["key_filename"]
    assert call_kwargs["passphrase"] == "secret"


def test_no_key_file_enables_look_for_keys() -> None:
    """Without key_file, look_for_keys=True so standard names are tried."""
    cfg = _config()  # no key_file, no passphrase
    host = SshHost(cfg)

    mock_mod, mock_client = _mock_paramiko()
    with patch.dict("sys.modules", {"paramiko": mock_mod}):
        host._connect()

    call_kwargs = mock_client.connect.call_args[1]
    assert call_kwargs["look_for_keys"] is True
    assert "key_filename" not in call_kwargs


# ---------------------------------------------------------------------------
# Fallback: missing keyring entry → try ssh-agent instead of hard-failing
# ---------------------------------------------------------------------------


def _mock_paramiko_with_exc(agent_keys: int = 0) -> tuple[MagicMock, MagicMock]:
    """Like _mock_paramiko but with a real SSHException class and a
    controllable Agent().get_keys() length."""
    mock_mod, mock_client = _mock_paramiko()

    class _SSHError(Exception):
        pass

    mock_mod.SSHException = _SSHError
    agent = MagicMock()
    agent.get_keys.return_value = [object()] * agent_keys
    mock_mod.Agent.return_value = agent
    return mock_mod, mock_client


def test_missing_keyring_falls_back_to_agent_connect() -> None:
    """Keyring entry absent → _connect still proceeds (no passphrase) so
    paramiko can authenticate via the ssh-agent."""
    cfg = _config(
        key_passphrase_keyring="unlimited-mcp-ssh",
        key_file="~/.ssh/id_rsa",
    )
    host = SshHost(cfg)

    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = None
    mock_mod, mock_client = _mock_paramiko_with_exc(agent_keys=1)

    with patch.dict("sys.modules", {"keyring": mock_keyring, "paramiko": mock_mod}):
        host._connect()

    call_kwargs = mock_client.connect.call_args[1]
    assert call_kwargs["allow_agent"] is True
    assert "passphrase" not in call_kwargs  # none was available
    assert host._passphrase_missing_reason is not None


def test_missing_keyring_and_agent_fail_gives_clear_error() -> None:
    """When the keyring entry is absent AND the agent fallback fails to
    authenticate, surface the missing-passphrase reason (not paramiko's
    misleading key-format error)."""
    cfg = _config(
        key_passphrase_keyring="unlimited-mcp-ssh",
        key_file="~/.ssh/id_rsa",
    )
    host = SshHost(cfg)

    mock_keyring = MagicMock()
    mock_keyring.get_password.return_value = None
    mock_mod, mock_client = _mock_paramiko_with_exc(agent_keys=0)
    mock_client.connect.side_effect = mock_mod.SSHException(
        "encountered RSA key, expected OPENSSH key"
    )

    with (
        patch.dict("sys.modules", {"keyring": mock_keyring, "paramiko": mock_mod}),
        pytest.raises(RuntimeError, match="Configured passphrase unavailable"),
    ):
        host._connect()


# ---------------------------------------------------------------------------
# Missing paramiko
# ---------------------------------------------------------------------------


def test_connect_without_paramiko_raises() -> None:
    host = SshHost(_config())
    with (
        patch.dict("sys.modules", {"paramiko": None}),  # type: ignore[dict-item]
        pytest.raises((RuntimeError, ImportError)),
    ):
        host._connect()


# ---------------------------------------------------------------------------
# Agent forwarding
# ---------------------------------------------------------------------------


def test_forward_agent_uses_channel_path() -> None:
    """When forward_agent=True the channel path is used and AgentRequestHandler fires."""
    cfg = _config(forward_agent=True)
    host = SshHost(cfg)

    mock_channel = MagicMock()
    mock_channel.recv_exit_status.return_value = 0

    stdout_f = MagicMock()
    stdout_f.read.return_value = b"ok\n"
    stdout_f.channel = mock_channel

    stderr_f = MagicMock()
    stderr_f.read.return_value = b""

    mock_channel.makefile.return_value = stdout_f
    mock_channel.makefile_stderr.return_value = stderr_f
    mock_channel.makefile_stdin.return_value = MagicMock()

    mock_transport = MagicMock()
    mock_transport.is_active.return_value = True
    mock_transport.open_session.return_value = mock_channel

    mock_client = MagicMock()
    mock_client.get_transport.return_value = mock_transport
    host._client = mock_client

    mock_agent_handler = MagicMock()
    mock_agent_mod = MagicMock()
    mock_agent_mod.AgentRequestHandler = mock_agent_handler
    mock_paramiko_mod = MagicMock()
    mock_paramiko_mod.agent = mock_agent_mod

    with patch.dict(
        "sys.modules", {"paramiko": mock_paramiko_mod, "paramiko.agent": mock_agent_mod}
    ):
        out = host.run(["git", "push"])

    mock_transport.open_session.assert_called_once()
    mock_agent_handler.assert_called_once_with(mock_channel)
    mock_channel.exec_command.assert_called_once()
    assert out.stdout == b"ok\n"


def test_no_forward_agent_skips_channel_path() -> None:
    """Default config (forward_agent=False) uses exec_command, not open_session."""
    host, mock_client = _patched_host(stdout=b"ok\n")
    host.run(["git", "fetch"])
    mock_client.exec_command.assert_called_once()
    mock_client.get_transport.return_value.open_session.assert_not_called()


# ---------------------------------------------------------------------------
# repos_root
# ---------------------------------------------------------------------------


def test_repos_root_default_is_none() -> None:
    cfg = _config()
    assert cfg.repos_root is None


def test_repos_root_parses_from_config() -> None:
    cfg = _config(repos_root="/root/repos")
    assert cfg.repos_root == "/root/repos"


def test_repos_root_and_forward_agent_together() -> None:
    cfg = _config(forward_agent=True, repos_root="/srv/repos")
    assert cfg.forward_agent is True
    assert cfg.repos_root == "/srv/repos"
