"""Unit tests for :class:`unlimited_mcp.safety.redactor.Redactor`."""

from __future__ import annotations

from unlimited_mcp.safety.redactor import REDACTED, Redactor


def test_auto_detects_key_suffix() -> None:
    env = {"GROQ_API_KEY": "gsk_secret_value"}
    r = Redactor(environ=env)
    assert r.secret_count == 1
    assert r.redact("call with gsk_secret_value here") == f"call with {REDACTED} here"


def test_auto_detects_token_suffix() -> None:
    env = {"GH_TOKEN": "ghp_abcdefghijkl"}
    r = Redactor(environ=env)
    assert r.redact("auth: ghp_abcdefghijkl") == f"auth: {REDACTED}"


def test_auto_detects_password_suffix() -> None:
    env = {"DB_PASSWORD": "supersecret"}
    r = Redactor(environ=env)
    assert r.redact("connecting with supersecret/...") == f"connecting with {REDACTED}/..."


def test_short_values_are_not_redacted() -> None:
    """Placeholders like DEBUG_KEY=1 must not turn every '1' into ***REDACTED***."""
    env = {"DEBUG_KEY": "1", "API_KEY": "real_secret_value"}
    r = Redactor(environ=env)
    out = r.redact("count is 1, key is real_secret_value")
    assert "1" in out
    assert "real_secret_value" not in out


def test_manual_env_var_names_override() -> None:
    env = {"WEIRD_NAME": "wxyz_my_secret"}
    r = Redactor(env_var_names=["WEIRD_NAME"], environ=env)
    assert r.redact("value=wxyz_my_secret") == f"value={REDACTED}"


def test_empty_env_no_secrets() -> None:
    r = Redactor(environ={})
    assert r.secret_count == 0
    assert r.redact("nothing to scrub") == "nothing to scrub"


def test_multiple_secrets_in_one_text() -> None:
    env = {"A_KEY": "alpha_secret", "B_TOKEN": "bravo_secret"}
    r = Redactor(environ=env)
    out = r.redact("a=alpha_secret b=bravo_secret")
    assert "alpha_secret" not in out
    assert "bravo_secret" not in out
    assert out.count(REDACTED) == 2


def test_longer_secret_wins_over_shorter_substring() -> None:
    """Avoid partial replacements when one secret contains another."""
    env = {"OUTER_KEY": "abc_xyz_123_long", "INNER_KEY": "abc_xyz_"}
    r = Redactor(environ=env)
    # The text contains the OUTER value; the INNER value is a prefix.
    out = r.redact("seen abc_xyz_123_long elsewhere")
    # Either both replaced or only the longer; what we forbid is leaving
    # half of the longer secret behind.
    assert "abc_xyz_123_long" not in out
    assert "abc_xyz_" not in out


def test_redact_argv() -> None:
    env = {"GROQ_API_KEY": "gsk_full_secret_value"}
    r = Redactor(environ=env)
    out = r.redact_argv(["aider", "--api-key", "gsk_full_secret_value", "foo.py"])
    assert out == ["aider", "--api-key", REDACTED, "foo.py"]


def test_redact_bytes_utf8() -> None:
    env = {"GROQ_API_KEY": "gsk_full_secret_value"}
    r = Redactor(environ=env)
    assert r.redact_bytes(b"call=gsk_full_secret_value") == f"call={REDACTED}".encode()


def test_redact_bytes_non_utf8_passthrough() -> None:
    """Binary garbage is returned unchanged. Better to leak than corrupt."""
    env = {"GROQ_API_KEY": "gsk_full_secret_value"}
    r = Redactor(environ=env)
    binary = b"\xff\xfe\x00\x01\x02 raw bytes"
    assert r.redact_bytes(binary) == binary


def test_uses_real_environment_when_environ_omitted(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_TEST_KEY", "live_environ_secret")
    r = Redactor()
    assert "live_environ_secret" not in r.redact("see live_environ_secret here")
