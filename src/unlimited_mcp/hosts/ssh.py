"""SSH execution backend for the ``Host`` protocol.

Connects to a remote host via paramiko and runs commands over an SSH channel.
The connection is kept alive across calls (pooled) and automatically
re-established if it drops.

Authentication cascade (tried in order, stops at first success):
  1. Running ssh-agent (``SSH_AUTH_SOCK``)
  2. Default key files (``~/.ssh/id_ed25519``, ``~/.ssh/id_rsa``, …)
  3. ``key_file`` from config (explicit path, optional)
  4. Passphrase from env var (``key_passphrase_env``) or keyring
     (``key_passphrase_keyring``)

Credentials are never written to disk.  See ``SSH.md`` for setup guide.

Requires the ``ssh`` optional dependency group::

    pip install 'unlimited-mcp[ssh]'
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unlimited_mcp.safety.redactor import Redactor

from .base import RunOutput

if TYPE_CHECKING:
    import paramiko

    from unlimited_mcp.config.schema import SshHostConfig


class SshHost:
    """Execute commands on a remote host over SSH.

    Parameters
    ----------
    config:
        Validated ``SshHostConfig`` from ``config.yaml``.
    redactor:
        When provided, applied to stdout and stderr before bytes are returned
        or written to disk.
    """

    def __init__(
        self,
        config: SshHostConfig,
        redactor: Redactor | None = None,
    ) -> None:
        self._config = config
        self._redactor = redactor
        self._client: paramiko.SSHClient | None = None
        self._ts_bin_cache: str | None = None

    @property
    def name(self) -> str:
        c = self._config
        return f"ssh:{c.user}@{c.host}:{c.port}"

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _resolve_passphrase(self) -> str | None:
        cfg = self._config
        if cfg.key_passphrase_env:
            val = os.environ.get(cfg.key_passphrase_env)
            if val is None:
                raise RuntimeError(
                    f"SSH passphrase env var {cfg.key_passphrase_env!r} is not set"
                )
            return val
        if cfg.key_passphrase_keyring:
            try:
                import keyring as _keyring  # type: ignore[import-untyped]
            except ImportError as exc:
                raise RuntimeError(
                    "keyring package not installed; run: pip install 'unlimited-mcp[ssh]'"
                ) from exc
            val = _keyring.get_password(cfg.key_passphrase_keyring, cfg.user)
            if val is None:
                raise RuntimeError(
                    f"No passphrase found in keyring for service "
                    f"{cfg.key_passphrase_keyring!r}, account {cfg.user!r}"
                )
            return val
        return None

    def _connect(self) -> paramiko.SSHClient:
        # Resolve and validate config before importing the optional dependency.
        passphrase = self._resolve_passphrase()

        # Passphrase without key_file is ambiguous — we wouldn't know which
        # file to unlock.  key_file is the source of truth in config.yaml.
        if passphrase is not None and not self._config.key_file:
            raise RuntimeError(
                f"Host {self._config.host!r}: key_passphrase_env / "
                "key_passphrase_keyring requires key_file to be set in config.yaml.\n"
                "Example:\n"
                "  key_file: ~/.ssh/my_key\n"
                "  key_passphrase_env: MY_SSH_PASSPHRASE"
            )

        try:
            import paramiko as _paramiko
        except ImportError as exc:
            raise RuntimeError(
                "paramiko not installed; run: pip install 'unlimited-mcp[ssh]'"
            ) from exc

        client = _paramiko.SSHClient()
        client.load_system_host_keys()
        # RejectPolicy: refuse connections to unknown hosts.
        # Users must accept the host fingerprint once with: ssh user@host
        client.set_missing_host_key_policy(_paramiko.RejectPolicy())

        has_explicit_key = bool(self._config.key_file)

        kwargs: dict[str, Any] = dict(
            hostname=self._config.host,
            port=self._config.port,
            username=self._config.user,
            # ssh-agent is always welcome — it is runtime state, not config.
            allow_agent=True,
            # When key_file is explicit, use only that key.
            # When not set, fall back to standard key names (~/.ssh/id_*).
            look_for_keys=not has_explicit_key,
        )
        if has_explicit_key:
            kwargs["key_filename"] = str(Path(self._config.key_file).expanduser())
        if passphrase is not None:
            kwargs["passphrase"] = passphrase

        client.connect(**kwargs)
        return client

    def _get_client(self) -> paramiko.SSHClient:
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._ts_bin_cache = None
        self._client = self._connect()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # SFTP helpers
    # ------------------------------------------------------------------

    def sftp_get(self, remote_path: str) -> bytes:
        """Download *remote_path* from the remote host and return its contents."""
        sftp = self._get_client().open_sftp()
        try:
            with sftp.open(remote_path, "rb") as f:
                return f.read()
        finally:
            sftp.close()

    def sftp_put(self, remote_path: str, content: bytes) -> None:
        """Upload *content* to *remote_path* on the remote host (creates parents if needed)."""
        sftp = self._get_client().open_sftp()
        try:
            # Ensure parent directory exists.
            parent = remote_path.rsplit("/", 1)[0]
            if parent:
                try:
                    sftp.mkdir(parent)
                except OSError:
                    pass  # already exists
            with sftp.open(remote_path, "wb") as f:
                f.write(content)
        finally:
            sftp.close()

    def sftp_exists(self, remote_path: str) -> bool:
        """Return True if *remote_path* exists on the remote host."""
        sftp = self._get_client().open_sftp()
        try:
            sftp.stat(remote_path)
            return True
        except OSError:
            return False
        finally:
            sftp.close()

    # ------------------------------------------------------------------
    # Host protocol
    # ------------------------------------------------------------------

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: int = 60,
        output_limit_bytes: int = 1_000_000,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        stdin_content: bytes | None = None,
    ) -> RunOutput:
        # Build shell command: cd → export env → argv
        parts: list[str] = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        if env_extra:
            parts.extend(f"export {k}={shlex.quote(v)}" for k, v in env_extra.items())
        parts.append(shlex.join(argv))
        cmd = " && ".join(parts)

        client = self._get_client()
        t0 = time.monotonic()

        _, stdout_f, stderr_f = client.exec_command(cmd, timeout=float(timeout_seconds))
        if stdin_content is not None:
            _.write(stdin_content)
        _.close()  # signals EOF to the remote command

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_out = pool.submit(stdout_f.read)
            f_err = pool.submit(stderr_f.read)
            try:
                stdout_raw: bytes = f_out.result(timeout=timeout_seconds)
                stderr_raw: bytes = f_err.result(timeout=timeout_seconds)
            except FutureTimeout:
                stdout_f.channel.close()
                raise subprocess.TimeoutExpired(argv, timeout_seconds)

        exit_code: int = stdout_f.channel.recv_exit_status()
        duration_ms = int((time.monotonic() - t0) * 1000)

        if self._redactor is not None:
            stdout_raw = self._redactor.redact_bytes(stdout_raw)
            stderr_raw = self._redactor.redact_bytes(stderr_raw)

        output_bytes = len(stdout_raw) + len(stderr_raw)
        truncated = output_bytes > output_limit_bytes
        if truncated:
            half = output_limit_bytes // 2
            stdout_raw = stdout_raw[-half:] if stdout_raw else b""
            stderr_raw = stderr_raw[-half:] if stderr_raw else b""

        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_bytes(stdout_raw)
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.write_bytes(stderr_raw)

        return RunOutput(
            stdout=stdout_raw,
            stderr=stderr_raw,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output_truncated=truncated,
            output_bytes=output_bytes,
        )

    # ------------------------------------------------------------------
    # Remote task-spooler helpers (used by RemoteTsRunner)
    # ------------------------------------------------------------------

    def _find_remote_ts_bin(self) -> str:
        """Probe the remote for task-spooler binary name (tsp/ts). Cached per connection."""
        if self._ts_bin_cache is not None:
            return self._ts_bin_cache
        out = self.run(
            ["sh", "-c", "command -v tsp || command -v ts 2>/dev/null || echo ts"]
        )
        name = out.stdout.decode().strip().split("\n")[-1].strip() or "ts"
        self._ts_bin_cache = name
        return self._ts_bin_cache

    def ts_submit(
        self,
        argv: list[str],
        *,
        label: str,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
        ts_socket: str | None = None,
        exit_code_path: str | None = None,
        stdin_file: str | None = None,
    ) -> int:
        """Enqueue *argv* on the remote task-spooler. Returns the slot ID.

        Parameters
        ----------
        exit_code_path:
            If set, the remote command will write the exit code of *argv* to
            this path on the remote machine so callers can retrieve it later.
        stdin_file:
            If set, the command is prefixed with ``cat <stdin_file> |`` so the
            file is fed to the command's stdin.  Used when ``stdin_content``
            is provided (ts does not support stdin natively).
        """
        parts: list[str] = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        if env_extra:
            parts.extend(f"export {k}={shlex.quote(v)}" for k, v in env_extra.items())
        cmd = shlex.join(argv)
        if stdin_file:
            cmd = f"cat {shlex.quote(stdin_file)} | {cmd}"
        parts.append(cmd)
        inner = " && ".join(parts) if parts else shlex.join(argv)

        if exit_code_path:
            # Wrap so exit code is saved even when the inner command fails.
            inner = (
                f"{{ {inner}; }}; "
                f"_ec=$?; echo $_ec > {shlex.quote(exit_code_path)}; exit $_ec"
            )

        ts_env: dict[str, str] | None = {"TS_SOCKET": ts_socket} if ts_socket else None
        ts_bin = self._find_remote_ts_bin()
        result = self.run(
            [ts_bin, "-L", label, "sh", "-c", inner],
            env_extra=ts_env,
        )
        return int(result.stdout.decode().strip())

    def ts_status(self, slot_id: int, *, ts_socket: str | None = None) -> str:
        """Return the remote task-spooler state for *slot_id*.

        Returns one of: ``"queued"``, ``"running"``, ``"finished"``, ``"failed"``.
        """
        ts_env: dict[str, str] | None = {"TS_SOCKET": ts_socket} if ts_socket else None
        result = self.run(
            [self._find_remote_ts_bin(), "-s", str(slot_id)],
            env_extra=ts_env,
        )
        raw = result.stdout.decode().strip().lower()
        # ts uses "skipped" when the command failed or its predecessor failed.
        return "failed" if raw == "skipped" else (raw or "unknown")

    def ts_output(self, slot_id: int, *, ts_socket: str | None = None) -> bytes:
        """Return captured stdout+stderr of a finished task-spooler job."""
        ts_env: dict[str, str] | None = {"TS_SOCKET": ts_socket} if ts_socket else None
        path_out = self.run(
            [self._find_remote_ts_bin(), "-o", str(slot_id)],
            env_extra=ts_env,
        )
        output_path = path_out.stdout.decode().strip()
        if not output_path:
            return b""
        return self.run(["cat", output_path]).stdout

    def ts_cancel(self, slot_id: int, *, ts_socket: str | None = None) -> None:
        """Kill a running remote ts job or remove it from the queue."""
        ts_env: dict[str, str] | None = {"TS_SOCKET": ts_socket} if ts_socket else None
        ts_bin = self._find_remote_ts_bin()
        # -k sends SIGTERM to the running job; -r removes a queued job.
        # Ignore errors — the job may have already finished.
        for flag in ("-k", "-r"):
            try:
                self.run([ts_bin, flag, str(slot_id)], env_extra=ts_env)
            except Exception:
                pass
