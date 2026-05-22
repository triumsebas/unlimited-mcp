"""Unit tests for SshHost SFTP helpers (sftp_get, sftp_put, sftp_exists)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from unlimited_mcp.config.schema import SshHostConfig
from unlimited_mcp.hosts.ssh import SshHost

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> SshHostConfig:
    return SshHostConfig(type="ssh", user="ubuntu", host="10.0.0.1", port=22)


def _host_with_sftp() -> tuple[SshHost, MagicMock, MagicMock]:
    """Return (host, mock_ssh_client, mock_sftp) ready for use."""
    host = SshHost(_config())

    mock_sftp = MagicMock()
    mock_client = MagicMock()
    mock_transport = MagicMock()
    mock_transport.is_active.return_value = True
    mock_client.get_transport.return_value = mock_transport
    mock_client.open_sftp.return_value = mock_sftp

    host._client = mock_client
    return host, mock_client, mock_sftp


# ---------------------------------------------------------------------------
# sftp_get
# ---------------------------------------------------------------------------


def test_sftp_get_returns_file_contents() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_file.read.return_value = b"hello content"
    mock_sftp.open.return_value = mock_file

    result = host.sftp_get("/remote/path/file.txt")
    assert result == b"hello content"
    mock_sftp.open.assert_called_once_with("/remote/path/file.txt", "rb")


def test_sftp_get_closes_sftp_session() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_file.read.return_value = b""
    mock_sftp.open.return_value = mock_file

    host.sftp_get("/remote/file.txt")
    mock_sftp.close.assert_called_once()


def test_sftp_get_closes_on_error() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_sftp.open.side_effect = OSError("file not found")

    with pytest.raises(OSError):
        host.sftp_get("/remote/missing.txt")
    mock_sftp.close.assert_called_once()


# ---------------------------------------------------------------------------
# sftp_put
# ---------------------------------------------------------------------------


def test_sftp_put_writes_content() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_sftp.open.return_value = mock_file

    host.sftp_put("/remote/dir/out.txt", b"data to write")
    mock_sftp.open.assert_called_once_with("/remote/dir/out.txt", "wb")
    mock_file.write.assert_called_once_with(b"data to write")


def test_sftp_put_creates_parent_dir() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_sftp.open.return_value = mock_file

    host.sftp_put("/remote/newdir/file.txt", b"x")
    mock_sftp.mkdir.assert_called_once_with("/remote/newdir")


def test_sftp_put_ignores_existing_parent() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_sftp.open.return_value = mock_file
    mock_sftp.mkdir.side_effect = OSError("already exists")

    # Should not raise
    host.sftp_put("/remote/existing/file.txt", b"x")


def test_sftp_put_closes_sftp_session() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_file = MagicMock()
    mock_file.__enter__ = MagicMock(return_value=mock_file)
    mock_file.__exit__ = MagicMock(return_value=False)
    mock_sftp.open.return_value = mock_file

    host.sftp_put("/remote/file.txt", b"")
    mock_sftp.close.assert_called_once()


# ---------------------------------------------------------------------------
# sftp_exists
# ---------------------------------------------------------------------------


def test_sftp_exists_true() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_sftp.stat.return_value = MagicMock()  # stat succeeds

    assert host.sftp_exists("/remote/present.txt") is True
    mock_sftp.stat.assert_called_once_with("/remote/present.txt")


def test_sftp_exists_false() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_sftp.stat.side_effect = OSError("not found")

    assert host.sftp_exists("/remote/missing.txt") is False


def test_sftp_exists_closes_sftp_session() -> None:
    host, _, mock_sftp = _host_with_sftp()
    mock_sftp.stat.return_value = MagicMock()

    host.sftp_exists("/remote/file.txt")
    mock_sftp.close.assert_called_once()
