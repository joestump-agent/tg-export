"""M2 auth & session tests — fully offline, mocked Telethon, 100% synthetic.

Covers the sentinel exit-code contract, ``doctor``'s valid/invalid behavior, the
missing-credential path, secret hygiene (no secret-shaped value in captured
output/logs; session file ``0600``), and the no-hardcoded-credential invariant.
Every test runs under the autouse ``no_network`` guard, and ``telethon_stub``
replaces ``telethon.TelegramClient`` so nothing connects.

# Governing: SPEC-0001 REQ "Authentication and Session Model", REQ "Security and
#            Secret Hygiene", REQ "CLI Surface"; ADR-0009
"""

from __future__ import annotations

import asyncio
import logging
import re
import stat
from pathlib import Path

import pytest

from tg_export import auth, cli
from tg_export.errors import (
    EXIT_MALFORMED_ARG,
    EXIT_NETWORK,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    NOT_AUTHORIZED_TOKEN,
    MalformedArgumentError,
    NetworkError,
    NotAuthorizedError,
)

# Synthetic secrets — never a real account's material (SPEC-0001 REQ "Testing").
FAKE_API_ID = "1234567"
FAKE_API_HASH = "0123456789abcdef0123456789abcdef"
FAKE_PHONE = "+15551234567"
FAKE_CODE = "54321"
FAKE_2FA = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guarantee env credentials never leak in from the host and perturb tests.
    monkeypatch.delenv(auth.ENV_API_ID, raising=False)
    monkeypatch.delenv(auth.ENV_API_HASH, raising=False)


# --- credential resolution ---------------------------------------------------
def test_resolve_credential_from_flags():
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env={})
    assert cred.api_id == 1234567
    assert cred.api_hash == FAKE_API_HASH


def test_resolve_credential_from_env_when_no_flags():
    env = {auth.ENV_API_ID: FAKE_API_ID, auth.ENV_API_HASH: FAKE_API_HASH}
    cred = auth.resolve_credential(None, None, env=env)
    assert cred.api_id == 1234567


def test_flags_override_env():
    env = {auth.ENV_API_ID: "999", auth.ENV_API_HASH: "envhash"}
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env=env)
    assert cred.api_id == 1234567
    assert cred.api_hash == FAKE_API_HASH


def test_missing_credential_raises_greppable_and_leaks_no_value():
    with pytest.raises(MalformedArgumentError) as exc:
        auth.resolve_credential(None, None, env={})
    msg = str(exc.value)
    assert "tg-export:" in msg
    assert "my.telegram.org" in msg
    # No secret value is echoed.
    assert FAKE_API_HASH not in msg


def test_non_integer_api_id_is_malformed_without_echoing_value():
    with pytest.raises(MalformedArgumentError) as exc:
        auth.resolve_credential("not-a-number", FAKE_API_HASH, env={})
    assert "not-a-number" not in str(exc.value)


# --- doctor: valid vs invalid ------------------------------------------------
def _doctor_argv(session: Path) -> list[str]:
    return [
        "doctor",
        "--session",
        str(session),
        "--api-id",
        FAKE_API_ID,
        "--api-hash",
        FAKE_API_HASH,
    ]


def test_doctor_returns_zero_for_valid_session(tmp_path, telethon_stub, capsys):
    telethon_stub(authorized=True)
    code = cli.main(_doctor_argv(tmp_path / "s.session"))
    assert code == EXIT_OK
    # Token must NOT appear on the happy path.
    assert NOT_AUTHORIZED_TOKEN not in capsys.readouterr().err


def test_doctor_unauthorized_emits_token_and_dedicated_code(tmp_path, telethon_stub, capsys):
    telethon_stub(authorized=False)
    code = cli.main(_doctor_argv(tmp_path / "s.session"))
    assert code == EXIT_NOT_AUTHORIZED
    err = capsys.readouterr().err
    # Exact, stable token on stderr (msgbrowse greps for it).
    assert NOT_AUTHORIZED_TOKEN in err.splitlines()


def test_doctor_network_failure_maps_to_network_code(tmp_path, telethon_stub, capsys):
    telethon_stub(connect_error=ConnectionError("dc unreachable"))
    code = cli.main(_doctor_argv(tmp_path / "s.session"))
    assert code == EXIT_NETWORK
    # A network failure must NOT masquerade as "not authorized".
    assert NOT_AUTHORIZED_TOKEN not in capsys.readouterr().err


def test_missing_credential_cli_exits_malformed(tmp_path, capsys):
    # No --api-* flags and no env → malformed-argument exit, greppable message.
    code = cli.main(["doctor", "--session", str(tmp_path / "s.session")])
    assert code == EXIT_MALFORMED_ARG
    assert "tg-export:" in capsys.readouterr().err


# --- distinct exit codes -----------------------------------------------------
def test_sentinel_exit_codes_are_all_distinct():
    codes = {
        NotAuthorizedError("x").exit_code,
        NetworkError("x").exit_code,
        MalformedArgumentError("x").exit_code,
    }
    assert len(codes) == 3
    assert codes == {EXIT_NOT_AUTHORIZED, EXIT_NETWORK, EXIT_MALFORMED_ARG}


def test_three_failure_classes_yield_three_codes_behaviorally(tmp_path, telethon_stub):
    session = tmp_path / "s.session"
    telethon_stub(authorized=False)
    not_auth = cli.main(_doctor_argv(session))

    telethon_stub(connect_error=ConnectionError("boom"))
    network = cli.main(_doctor_argv(session))

    malformed = cli.main(["doctor", "--session", str(session)])  # no creds

    assert len({not_auth, network, malformed}) == 3


# --- login writes a hardened session -----------------------------------------
def test_login_writes_0600_session(tmp_path, telethon_stub):
    telethon_stub(authorized=False)  # start() flips to authorized
    session = tmp_path / "app" / "tg.session"
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env={})
    resolved = auth.login(
        session=session,
        credential=cred,
        phone_cb=lambda: FAKE_PHONE,
        code_cb=lambda: FAKE_CODE,
        password_cb=lambda: FAKE_2FA,
    )
    assert resolved.exists()
    mode = stat.S_IMODE(resolved.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_login_bare_session_name_normalizes_to_dot_session(tmp_path, telethon_stub):
    # --session /app/tg (no extension) must resolve to /app/tg.session so the file
    # lands exactly where headless reuse will reopen it (review item 2).
    stub = telethon_stub(authorized=False)
    session = tmp_path / "app" / "tg"
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env={})
    resolved = auth.login(
        session=session,
        credential=cred,
        phone_cb=lambda: FAKE_PHONE,
        code_cb=lambda: FAKE_CODE,
        password_cb=lambda: FAKE_2FA,
    )
    expected = session.with_name("tg.session")
    assert resolved == expected
    assert expected.exists()
    assert not session.exists()  # the literal bare path is not the session file
    assert stat.S_IMODE(expected.stat().st_mode) == 0o600
    # The client was constructed against the same normalized path (reuse is stable).
    assert stub.instances[-1].session == str(expected)


def test_open_client_takeout_does_not_finalize_on_consumer_error(tmp_path, telethon_stub):
    # Review item 1: a consumer-side exception must propagate and reach the
    # takeout's __aexit__ with real exc_info — NOT be finalized as success — so
    # M3's killed run can resume cleanly.
    stub = telethon_stub(authorized=True)
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env={})

    async def run() -> None:
        async with auth.open_client(
            session=tmp_path / "s.session", credential=cred, takeout=True
        ) as takeout_client:
            assert takeout_client is not None
            raise ValueError("consumer boom")

    with pytest.raises(ValueError, match="consumer boom"):
        asyncio.run(run())

    client = stub.instances[-1]
    assert client.takeout_exc_type is ValueError  # exc_info forwarded
    assert client.takeout_finalized_success is False  # not finalized as success


def test_open_client_takeout_finalizes_on_clean_exit(tmp_path, telethon_stub):
    stub = telethon_stub(authorized=True)
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env={})

    async def run() -> None:
        async with auth.open_client(
            session=tmp_path / "s.session", credential=cred, takeout=True
        ) as takeout_client:
            assert takeout_client is not None

    asyncio.run(run())
    client = stub.instances[-1]
    assert client.takeout_exc_type is None
    assert client.takeout_finalized_success is True


def test_login_via_cli_exits_zero(tmp_path, telethon_stub, monkeypatch):
    telethon_stub(authorized=False)
    # Feed the interactive prompts without a TTY by patching the default callbacks.
    monkeypatch.setattr(auth, "_prompt_phone", lambda: FAKE_PHONE)
    monkeypatch.setattr(auth, "_prompt_code", lambda: FAKE_CODE)
    monkeypatch.setattr(auth, "_prompt_password", lambda: FAKE_2FA)
    session = tmp_path / "tg.session"
    code = cli.main(
        ["login", "--session", str(session), "--api-id", FAKE_API_ID, "--api-hash", FAKE_API_HASH]
    )
    assert code == EXIT_OK
    assert session.exists()


# --- secret hygiene ----------------------------------------------------------
def test_login_and_doctor_leak_no_secret(tmp_path, telethon_stub, capsys, caplog):
    caplog.set_level(logging.DEBUG, logger="tg_export")
    telethon_stub(authorized=False)
    session = tmp_path / "tg.session"
    cred = auth.resolve_credential(FAKE_API_ID, FAKE_API_HASH, env={})

    auth.login(
        session=session,
        credential=cred,
        phone_cb=lambda: FAKE_PHONE,
        code_cb=lambda: FAKE_CODE,
        password_cb=lambda: FAKE_2FA,
    )
    # Re-run doctor headless against the (now authorized) stub.
    telethon_stub(authorized=True)
    cli.main(_doctor_argv(session))

    captured = capsys.readouterr()
    haystack = "\n".join([captured.out, captured.err, caplog.text])

    for secret in (FAKE_2FA, FAKE_PHONE, FAKE_API_HASH):
        assert secret not in haystack, f"secret leaked into output/logs: {secret!r}"
    # Full phone must not appear; a last-4 hint (if any) is all that is allowed.
    assert FAKE_PHONE not in haystack
    # Session path (a path, not a secret) is fine and expected to be present.
    assert str(session) in haystack


def test_phone_last4_never_carries_full_number():
    from tg_export.logging import phone_last4

    hint = phone_last4(FAKE_PHONE)
    assert hint == "4567"
    assert FAKE_PHONE not in (hint or "")


# --- no hard-coded credential in source --------------------------------------
def test_no_api_credential_literal_in_source():
    src = Path(__file__).resolve().parents[1] / "src" / "tg_export"
    api_hash_literal = re.compile(r"""['"][0-9a-fA-F]{32}['"]""")
    api_id_literal = re.compile(r"""api_id\s*=\s*['"]?\d{5,}""")
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not api_hash_literal.search(text), f"api_hash-shaped literal in {path}"
        assert not api_id_literal.search(text), f"api_id literal in {path}"


# --- only-Telegram egress (no telemetry / third-party http) ------------------
def test_source_has_no_third_party_network_clients():
    src = Path(__file__).resolve().parents[1] / "src" / "tg_export"
    forbidden = re.compile(
        r"\b(?:import\s+requests|from\s+requests|urllib\.request|http\.client|httpx)\b"
    )
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"third-party network client in {path}"
