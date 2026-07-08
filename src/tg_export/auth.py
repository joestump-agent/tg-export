"""Credential resolution, interactive login, and headless client factories.

This module owns the entire MTProto auth surface (ADR-0002):

* :func:`resolve_credential` sources the ``api_id``/``api_hash`` app credential
  from ``--api-id``/``--api-hash`` flags or the ``TG_EXPORT_API_ID`` /
  ``TG_EXPORT_API_HASH`` environment variables — per-user by default (ADR-0006).
  No credential is ever hard-coded here.
* :func:`login` performs the one-time interactive phone -> code -> 2FA-password
  dance and writes the Telethon session to the caller-owned ``--session`` path,
  hardened to ``0600``.
* :func:`verify_session` (used by ``doctor``) and :func:`open_client` (the seam
  M3's export uses) run *headless*: a missing/expired session raises
  :class:`NotAuthorizedError` rather than prompting. ``open_client`` exposes a
  ``takeout`` flag so bulk export can opt into Telegram's takeout session for
  more forgiving flood limits.

Secret hygiene (SPEC-0001 REQ "Security and Secret Hygiene"; ADR-0009): the
session blob, auth keys, the 2FA password, and the full phone number are never
printed, logged, or copied. The 2FA password is read via ``getpass`` (never
echoed) and never retained. The only network egress is Telethon -> Telegram DCs.

# Governing: SPEC-0001 REQ "Authentication and Session Model", REQ "Security and
#            Secret Hygiene"; ADR-0002, ADR-0006, ADR-0009
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import telethon
from telethon.errors import ServerError, UnauthorizedError
from telethon.errors import TimeoutError as TelethonTimeoutError

from .errors import MalformedArgumentError, NetworkError, NotAuthorizedError
from .logging import log_event

# Environment variables that source the per-user credential (ADR-0006).
ENV_API_ID = "TG_EXPORT_API_ID"
ENV_API_HASH = "TG_EXPORT_API_HASH"

#: Stable, greppable message pointing a first-timer at my.telegram.org. Carries no
#: secret value — it only names the flags/env vars to set.
CREDENTIAL_HELP = (
    "tg-export: missing Telegram API credential. Pass --api-id and --api-hash, or "
    f"set {ENV_API_ID} and {ENV_API_HASH}. Register an app at "
    "https://my.telegram.org/apps to obtain them."
)

# Transport failures that mean "network problem", not "bad session". Builtin
# ConnectionError/OSError cover socket-level faults; Telethon's ServerError and
# TimeoutError cover DC-side faults.
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
    ServerError,
    TelethonTimeoutError,
)

# A factory takes (session_path, api_id, api_hash) and returns a Telethon-shaped
# client. The default builds a real ``telethon.TelegramClient``; tests inject a
# fake. Looked up via the ``telethon`` module at call time so a monkeypatch on
# ``telethon.TelegramClient`` is honoured.
ClientFactory = Callable[[str, int, str], Any]

# Interactive prompt callbacks (injectable for tests; defaults prompt the TTY).
PhoneCallback = Callable[[], str]
CodeCallback = Callable[[], str]
PasswordCallback = Callable[[], str]


@dataclass(frozen=True)
class ApiCredential:
    """A resolved Telegram app credential. Never logged or printed."""

    api_id: int
    api_hash: str


def resolve_credential(
    api_id: str | int | None = None,
    api_hash: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> ApiCredential:
    """Resolve the app credential from flags then env, erroring clearly if absent.

    Precedence: explicit ``--api-id``/``--api-hash`` flag values win; otherwise the
    ``TG_EXPORT_API_*`` environment variables are consulted (ADR-0006). Raises
    :class:`MalformedArgumentError` (never printing any value) when neither source
    supplies both halves, or when ``api_id`` is not an integer.
    """
    environ = os.environ if env is None else env
    raw_id: str | int | None = api_id if api_id is not None else environ.get(ENV_API_ID)
    raw_hash: str | None = api_hash if api_hash is not None else environ.get(ENV_API_HASH)

    if raw_id is None or raw_id == "" or not raw_hash:
        raise MalformedArgumentError(CREDENTIAL_HELP)

    try:
        parsed_id = int(raw_id)
    except (TypeError, ValueError):
        # Do not echo the offending value — it is credential-shaped.
        raise MalformedArgumentError(
            "tg-export: --api-id / TG_EXPORT_API_ID must be an integer"
        ) from None

    return ApiCredential(api_id=parsed_id, api_hash=str(raw_hash))


def _default_factory(session: str, api_id: int, api_hash: str) -> Any:
    # Resolved through the module so tests monkeypatching telethon.TelegramClient
    # take effect. Constructed inside the running loop by the callers below.
    return telethon.TelegramClient(session, api_id, api_hash)


def _resolve_session_path(session: str | os.PathLike[str]) -> Path:
    """Return the concrete session-file path, appending ``.session`` when absent.

    Telethon's SQLiteSession appends ``.session`` to a name that lacks the
    extension, so a caller passing ``/app/tg`` would find the real file at
    ``/app/tg.session``. Normalizing here means the path we hand Telethon, the
    path we harden, and the path we report are all identical — and login and every
    headless reuse resolve to the same file. A path that already carries any
    suffix is passed through unchanged.
    """
    path = Path(session)
    if path.suffix == "":
        return path.with_name(path.name + ".session")
    return path


def _construct(
    session: str | os.PathLike[str],
    credential: ApiCredential,
    factory: ClientFactory | None,
) -> Any:
    build = factory or _default_factory
    resolved = _resolve_session_path(session)
    return build(str(resolved), credential.api_id, credential.api_hash)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _connect(client: Any) -> None:
    try:
        await _maybe_await(client.connect())
    except _NETWORK_ERRORS as exc:
        raise NetworkError("tg-export: could not reach Telegram") from exc


async def _disconnect(client: Any) -> None:
    disconnect = getattr(client, "disconnect", None)
    if disconnect is None:
        return
    try:
        await _maybe_await(disconnect())
    except Exception as exc:
        # Best-effort teardown must never mask the original outcome, but a
        # persistent disconnect failure should still be greppable (name only,
        # never a message body or secret).
        log_event("disconnect_failed", level=logging.DEBUG, error=type(exc).__name__)


async def _is_authorized(client: Any) -> bool:
    try:
        return bool(await _maybe_await(client.is_user_authorized()))
    except UnauthorizedError:
        # Unreachable with Telethon 1.36 — is_user_authorized() catches RPCError
        # internally and returns False. Kept as a defensive guard so a future
        # Telethon that lets the 401 escape still degrades to "not authorized".
        return False
    except _NETWORK_ERRORS as exc:
        raise NetworkError("tg-export: could not reach Telegram") from exc


def harden_session_file(session: str | os.PathLike[str]) -> Path:
    """Ensure the session file exists and is ``0600``; return its resolved path.

    The path is normalized the same way it is for the client (see
    :func:`_resolve_session_path`), so the hardened file is exactly the one
    Telethon wrote and exactly the one headless reuse will reopen. When no real DB
    was written (e.g. under a mocked client) the resolved path is created so the
    "a session exists at the resolved --session path" contract holds, then locked
    to ``0600`` explicitly (independent of the process umask). A trailing
    ``.session``-suffixed fallback is still checked to stay robust if a caller
    supplies an unusual non-``.session`` extension that Telethon extends.
    """
    resolved = _resolve_session_path(session)
    candidates = [resolved, resolved.with_name(resolved.name + ".session")]
    for candidate in candidates:
        if candidate.exists():
            os.chmod(candidate, 0o600)
            return candidate
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.touch(mode=0o600)
    os.chmod(resolved, 0o600)
    return resolved


# --- interactive prompts (defaults; injectable) ------------------------------
def _prompt_phone() -> str:
    return input("Phone number (international, e.g. +15551234567): ").strip()


def _prompt_code() -> str:
    return input("Login code (sent by Telegram): ").strip()


def _prompt_password() -> str:
    # getpass never echoes and the value is passed straight to Telethon, never
    # stored, logged, or printed.
    return getpass.getpass("Two-factor password: ")


async def _alogin(
    session: str | os.PathLike[str],
    credential: ApiCredential,
    factory: ClientFactory | None,
    phone_cb: PhoneCallback,
    code_cb: CodeCallback,
    password_cb: PasswordCallback,
) -> None:
    client = _construct(session, credential, factory)
    try:
        # Telethon's start() drives the phone -> code -> 2FA-password dance and
        # handles SessionPasswordNeededError internally when a password callback
        # is supplied. Callbacks (not literal values) keep secrets out of frames.
        await _maybe_await(
            client.start(phone=phone_cb, code_callback=code_cb, password=password_cb)
        )
    except UnauthorizedError as exc:
        raise NotAuthorizedError("tg-export: login was not authorized") from exc
    except _NETWORK_ERRORS as exc:
        raise NetworkError("tg-export: could not reach Telegram") from exc
    finally:
        await _disconnect(client)


def login(
    *,
    session: str | os.PathLike[str],
    credential: ApiCredential,
    client_factory: ClientFactory | None = None,
    phone_cb: PhoneCallback | None = None,
    code_cb: CodeCallback | None = None,
    password_cb: PasswordCallback | None = None,
) -> Path:
    """Run the one-time interactive login and write a hardened ``0600`` session.

    Returns the resolved session-file path. Prints/logs nothing secret: the phone,
    code, and 2FA password stay inside the injected callbacks and Telethon.
    """
    asyncio.run(
        _alogin(
            session,
            credential,
            client_factory,
            phone_cb or _prompt_phone,
            code_cb or _prompt_code,
            password_cb or _prompt_password,
        )
    )
    resolved = harden_session_file(session)
    # Log only the path — never the session blob, phone, or any auth material.
    log_event("login_ok", session=str(resolved))
    return resolved


async def _averify(
    session: str | os.PathLike[str],
    credential: ApiCredential,
    factory: ClientFactory | None,
) -> None:
    client = _construct(session, credential, factory)
    await _connect(client)
    try:
        if not await _is_authorized(client):
            raise NotAuthorizedError("tg-export: session is not authorized")
    finally:
        await _disconnect(client)


def verify_session(
    *,
    session: str | os.PathLike[str],
    credential: ApiCredential,
    client_factory: ClientFactory | None = None,
) -> None:
    """Headless authorization check for ``doctor``.

    Returns ``None`` when the session is valid and authorized; raises
    :class:`NotAuthorizedError` for a missing/expired session or
    :class:`NetworkError` when Telegram is unreachable. Never prompts.
    """
    asyncio.run(_averify(session, credential, client_factory))


@asynccontextmanager
async def open_client(
    *,
    session: str | os.PathLike[str],
    credential: ApiCredential,
    takeout: bool = False,
    client_factory: ClientFactory | None = None,
) -> Any:
    """Headless, authorized client context — the seam M3's export builds on.

    Connects, asserts the session is authorized (raising
    :class:`NotAuthorizedError` otherwise — never prompting), and yields a client
    ready for read-only history iteration. When ``takeout=True`` the yielded client
    is a Telethon **takeout** session (ADR-0002) with more forgiving flood limits
    for large historical pulls; M3 opts in by passing ``takeout=True``.
    """
    client = _construct(session, credential, client_factory)
    await _connect(client)
    try:
        if not await _is_authorized(client):
            raise NotAuthorizedError("tg-export: session is not authorized")
        if takeout:
            # Drive the takeout via ``async with`` so a consumer-side exception is
            # forwarded to __aexit__ with real exc_info — the takeout is NOT
            # finalized as success, letting M3's killed run resume cleanly.
            async with client.takeout(finalize=True) as takeout_client:
                yield takeout_client
        else:
            yield client
    finally:
        await _disconnect(client)


__all__ = [
    "ApiCredential",
    "CREDENTIAL_HELP",
    "ENV_API_HASH",
    "ENV_API_ID",
    "harden_session_file",
    "login",
    "open_client",
    "resolve_credential",
    "verify_session",
]
