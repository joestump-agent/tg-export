---
status: draft
date: 2026-07-08
implements: [ADR-0001, ADR-0002, ADR-0003, ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0009, ADR-0010]
---

# SPEC-0001: Telegram JSON Export

> **Partially superseded by [ADR-0011](../../adrs/ADR-0011-tdl-raw-transform-pivot.md).**
> The **JSON output contract** (manifest + NDJSON + schema, `schema_version: 1`) and the
> **message-mapping fidelity** requirements below remain authoritative and unchanged.
> The **acquisition half is retired**: tg-export no longer logs in, holds a session, or
> talks to Telegram — it transforms a [`tdl`](https://github.com/iyear/tdl) `--raw` dump
> offline. So the requirements covering authentication/session, the interactive
> `login`/`doctor`/`chats`/`export` CLI surface, incremental `--since`/`--full`, media
> download, and flood-wait reliability are superseded (they now live upstream in tdl and
> in msgbrowse's import). The CLI surface is a single `transform --input --output`. A full
> spec rewrite to the transform model is tracked as follow-up.

## Overview

`tg-export` extracts a Telegram account's history into a clean, ingestion-ready JSON archive that [msgbrowse](https://github.com/joestump/msgbrowse) parses. It is a standalone delegate exporter (see ADR-0001) built on Telethon/MTProto (ADR-0002). Output is a directory tree of NDJSON-per-chat plus a manifest (ADR-0003), versioned by an integer `schema_version` with JSON Schema shipped in-package (ADR-0004). This spec is the authoritative contract; the msgbrowse-side parser (msgbrowse SPEC-0015, story #209) is built to the same `schema_version` and the two MUST stay in lockstep.

The canonical, machine-checkable form of the contract is the JSON Schema files shipped in the package (`schema/manifest.schema.json`, `schema/message.schema.json`). Where this prose and the shipped schema disagree, the shipped schema is authoritative and the discrepancy MUST be fixed.

## Requirements

### Requirement: JSON Output Contract

The tool MUST write output as a directory tree, not a single file. The tree MUST contain a `manifest.json` at the root, one `chats/<chat_id>.ndjson` per exported chat (one JSON message object per line), and downloaded media under `media/<chat_id>/`. Every `manifest.json` MUST carry `schema_version` as an integer (currently `1`) and `tool` as the literal string `"tg-export"`.

The `manifest.json` MUST include: `schema_version` (int), `tool` (string), `tool_version` (semver string), `generated_at` (int, Unix seconds UTC), `account.id` (int, self user id), and `chats[]` (array; MAY be empty). `account.username` and `account.phone_last4` are OPTIONAL and MAY be null; `phone_last4` MUST contain at most the last 4 digits and MUST NOT contain the full number. Each `chats[]` entry MUST include `id` (int), `type` (enum: `private` · `group` · `supergroup` · `channel` · `self`), `title` (string), `message_count` (int), `max_message_id` (int, the incremental anchor), and `file` (string, relative path e.g. `"chats/777000.ndjson"`); `username`, `min_date`, and `max_date` are OPTIONAL.

Each NDJSON message object MUST include: `id` (int), `chat_id` (int), `date` (int, Unix seconds UTC — the canonical timestamp), `kind` (enum: `message` · `service`), `from.name` (string; `"Unknown"` if unresolved), `from.is_self` (bool), and `text` (string; `""` when none — media captions go here). OPTIONAL fields: `edit_date` (int|null), `from.id` (int|null — null for anonymous admins/channel posts), `from.username` (string|null), `entities[]` (array of `{type, url}`), `reply_to_message_id` (int|null), `forward` (object|null: `{from_name, from_id?, date?}`), `reactions[]` (array of `{emoji, count}`), `media` (object|null), and `action` (object|null; service messages only).

Every emitted `manifest.json` and every NDJSON message object MUST validate against the shipped JSON Schema. A bump to `schema_version` MUST be a coordinated change shipped on both tg-export and msgbrowse; it MUST NOT be changed silently.

Re-exporting the same message MUST produce byte-identical field values (determinism, per ADR-0004): the tool MUST NOT emit run-varying fields (e.g. `downloaded_at`), absolute paths, or ordering nondeterminism.

#### Scenario: Manifest and message validate against the shipped schema

- **WHEN** an export completes for an account with at least one chat
- **THEN** the written `manifest.json` and every line of every `chats/<chat_id>.ndjson` validate against `schema/manifest.schema.json` and `schema/message.schema.json` respectively

#### Scenario: Empty account still produces a valid manifest

- **WHEN** an export runs for an account with no exportable chats
- **THEN** a valid `manifest.json` is written with `chats: []` and no `chats/` NDJSON files are required

#### Scenario: Re-export is byte-identical

- **WHEN** the same synthetic message is exported twice into fresh output directories
- **THEN** the two NDJSON lines for that message are byte-for-byte identical

#### Scenario: phone_last4 never carries the full number

- **WHEN** the manifest is written for an account whose phone number is known
- **THEN** `account.phone_last4` contains at most 4 characters and the full phone number appears nowhere in the manifest

### Requirement: CLI Surface

The tool MUST expose the commands `login`, `export`, `chats`, `doctor`, and `--version`. All commands except `login` MUST be non-interactive. The caller MUST be able to pass a `--session <path>` it controls. `export` MUST accept `--session`, `--output <dir>`, `--since <dir>`, `--full`, `--chats ID,ID,...`, `--no-media`, and `--max-media-mb N`. `chats` MUST accept `--session` and `--json`. `--version` MUST print the tool's semver (used by `msgbrowse doctor`).

On failure the tool MUST exit non-zero and print a stable, greppable message on stderr. The tool MUST distinguish failure classes with distinct exit codes: an unauthorized/expired session MUST print a stable token (`tg-export: not authorized`) and exit with a dedicated code that is distinct from the exit codes for a network error and for a malformed-argument error.

#### Scenario: Non-interactive export

- **WHEN** `tg-export export --session <path> --output <dir>` is invoked with a valid authorized session and no controlling TTY
- **THEN** the export runs to completion without prompting for any input

#### Scenario: Unauthorized session emits the sentinel

- **WHEN** any non-`login` command runs against an expired or unauthorized session
- **THEN** stderr contains the exact token `tg-export: not authorized` and the process exits with the dedicated not-authorized exit code, distinct from the network-error and argument-error codes

#### Scenario: Version prints the tool semver

- **WHEN** `tg-export --version` is invoked
- **THEN** the tool's own semver (matching `manifest.tool_version`) is printed to stdout and the process exits 0

### Requirement: Authentication and Session Model

`login` MUST perform a one-time interactive MTProto authentication (prompt for phone → Telegram login code → 2FA password if the account has one), write the Telethon session to the `--session <path>`, and exit 0. All later commands MUST run headless from that session file. The tool MUST accept `api_id`/`api_hash` from `--api-id`/`--api-hash` flags or the `TG_EXPORT_API_ID`/`TG_EXPORT_API_HASH` environment variables (per-user credential is the default; see ADR-0006), and MUST NOT hard-code any API credential in the source.

The session file MUST be created with `0600` permissions. The bulk export path SHOULD use a Telethon takeout session for more forgiving flood limits on large historical pulls.

#### Scenario: Login writes a reusable session

- **WHEN** `tg-export login --session <path>` completes successfully against a real account
- **THEN** a session file exists at `<path>` with `0600` permissions and subsequent `export`/`chats`/`doctor` commands run headless against it

#### Scenario: Missing credential fails clearly

- **WHEN** `login` or `export` runs with neither the `--api-id`/`--api-hash` flags nor the `TG_EXPORT_API_*` environment variables set
- **THEN** the tool exits non-zero with a stable, greppable stderr message directing the user to supply the credential, and no secret value is printed

#### Scenario: No API credential in source

- **WHEN** the source tree is scanned for a literal `api_id`/`api_hash` value
- **THEN** none is present

### Requirement: Message Mapping Fidelity

Mapping a Telethon message to the contract object MUST preserve the fidelity that tdl's standard export lacked: resolved sender (`from.name`, `from.id`, `from.username`, `from.is_self`), service events (`kind: "service"` with an `action` object), reactions (`reactions[]` of `{emoji, count}`), replies (`reply_to_message_id`), and forwards (`forward` object). Visible text MUST be flattened into `text` (media captions included), with `""` when there is none.

For every link-type entity (`url`, `text_link`), the tool MUST resolve and emit the full URL as `entities[].url` and MUST NOT emit UTF-16 offsets across the boundary (per ADR-0005). Emitting other entity types is OPTIONAL. When a sender cannot be resolved, `from.name` MUST be `"Unknown"` and `from.id` MAY be null.

#### Scenario: Link entity is resolved to a URL

- **WHEN** a message containing a `url` or `text_link` entity is mapped
- **THEN** the emitted object contains `entities: [{ "type": "url", "url": "<resolved absolute URL>" }]` and contains no offset fields

#### Scenario: Service message maps to an action

- **WHEN** a service message (e.g. a pin, join, call, or group-photo change) is mapped
- **THEN** the emitted object has `kind: "service"` and an `action` object describing the event

#### Scenario: Unresolved sender degrades to Unknown

- **WHEN** a message's sender cannot be resolved
- **THEN** `from.name` is `"Unknown"` and the message is still emitted

### Requirement: Media Handling

When media download is enabled, the tool MUST download each message's media via the client to `media/<chat_id>/<message_id>[_<n>].<ext>` and set the message's `media.path` to that same relative path. The `media` object MUST carry `kind` (enum: `photo` · `video` · `voice` · `video_note` · `audio` · `sticker` · `animation` · `document`), `mime`, and `size`, and SHOULD carry `width`/`height`/`duration`/`filename` where cheaply available. `--no-media` MUST export metadata only. `--max-media-mb N` MUST skip files larger than N MB while still emitting the `media` object with `path: null` and `skipped: true`. Downloads MUST be idempotent: if the target file already exists with the expected size, the tool MUST skip re-downloading it.

#### Scenario: Media downloads to a relative path

- **WHEN** a message with a photo is exported with media enabled
- **THEN** the file is written under `media/<chat_id>/` and the message's `media.path` is the matching relative path

#### Scenario: Oversize media degrades to a skip stub

- **WHEN** a message's media exceeds `--max-media-mb N`
- **THEN** the `media` object is still emitted with `path: null` and `skipped: true`, and no file is downloaded

#### Scenario: Existing file is not re-downloaded

- **WHEN** an export runs and the target media file already exists with the expected size
- **THEN** the tool does not re-download the file

### Requirement: Incremental Export

`export --since <prev-dir>` MUST read that directory's `manifest.json` and, for each chat, pass its recorded `max_message_id` as the Telethon `iter_messages(min_id=...)` lower bound so that only newer messages are fetched and appended to that chat's existing NDJSON. Chats not present in the prior manifest MUST be exported in full. Each run MUST write a new complete `manifest.json`. `--full` MUST ignore all anchors and re-export everything. Re-emitting a boundary message MUST be safe (byte-stable output per ADR-0004, since msgbrowse re-import is idempotent). `edit_date` changes on already-exported messages are out of scope for v1.

#### Scenario: Since-run fetches only newer messages

- **WHEN** `export --since <prev-dir>` runs and a chat's prior `max_message_id` is M
- **THEN** only messages with id greater than M are fetched for that chat and appended to its existing NDJSON

#### Scenario: New chat is exported in full on a since-run

- **WHEN** `export --since <prev-dir>` runs and a chat is absent from the prior manifest
- **THEN** that chat is exported in full

#### Scenario: Full override ignores anchors

- **WHEN** `export --full` runs against a directory that has prior anchors
- **THEN** all messages are re-exported regardless of `max_message_id`

### Requirement: Reliability and Rate Limits

The tool MUST catch Telethon's `FloodWaitError`, sleep the requested duration, log it at info (seconds only), and resume — it MUST NOT fail the job on a flood-wait. Writes MUST be append-as-you-go so a killed run leaves a valid partial tree, and a re-run with `--since` against that partial output MUST continue where it stopped. The tool MUST be tolerant of best-effort failures: an unresolvable sender, a deleted-account peer, or an undownloadable media file MUST be logged and skipped with best-effort fields — one bad message MUST NOT abort a chat, and one bad chat MUST NOT abort the run.

#### Scenario: Flood-wait is survived, not fatal

- **WHEN** a `FloodWaitError` is raised mid-export
- **THEN** the tool sleeps the requested duration, logs the wait in seconds, and resumes the export without failing

#### Scenario: Killed run resumes cleanly

- **WHEN** an export is killed mid-run and then re-invoked with `--since` against the partial output
- **THEN** the export continues from where it stopped and the resulting tree is valid

#### Scenario: One bad message does not abort the chat

- **WHEN** a single message raises an error during mapping or media download
- **THEN** the message is logged and skipped with best-effort fields and the rest of the chat continues to export

### Requirement: Error Handling Standards

All error-producing operations MUST follow structured error handling:

- Errors MUST be wrapped with contextual information at each layer boundary (e.g., "chat 777000: download media for message 48120 failed: connection reset").
- Sentinel errors MUST be defined for domain-specific failure modes that callers need to distinguish programmatically (at minimum: not-authorized, network failure, malformed argument — surfaced as the distinct exit codes in the CLI Surface requirement).
- Silent error swallowing MUST NOT occur — every error MUST be either returned to the caller, logged with sufficient context, or explicitly handled with a documented reason for suppression (the best-effort tolerance path).
- Structured logging MUST be used for error and progress reporting (key-value pairs, not string interpolation). Progress MUST be machine-greppable (per-chat message counts, flood-waits, skips) and MUST NOT include message bodies.

#### Scenario: Errors are wrapped with context

- **WHEN** a media download fails deep in the export
- **THEN** the logged error identifies the chat id, message id, and underlying cause

#### Scenario: Logs never contain message bodies

- **WHEN** any command runs at its default log level
- **THEN** no message text appears in the logs — only counts, ids, and paths

### Requirement: Security and Secret Hygiene

The tool MUST NOT print, log, or copy session contents, auth keys, the 2FA password, or the full phone number; logs MUST carry only counts, ids, and paths. The session file MUST live where the caller points `--session` and MUST be created `0600`; the tool MUST NOT place it inside the output archive directory on its own initiative. The only network egress MUST be to Telegram's data centers during `login` and `export`; the tool MUST NOT emit telemetry or make third-party network calls.

#### Scenario: No secret material in logs

- **WHEN** `login` and `export` run and their output is captured
- **THEN** no auth key, 2FA password, session blob, or full phone number appears in stdout, stderr, or any log

#### Scenario: Only Telegram egress

- **WHEN** the tool runs
- **THEN** the only outbound network connections are to Telegram's data centers, and no telemetry or third-party endpoint is contacted

### Requirement: Packaging and Distribution

The package MUST be pure Python (no compiled extensions), require Python ≥ 3.11, use a `pyproject.toml` + `src/` layout with package `tg_export` and console entry point `tg-export`, and pin dependencies to exact versions (`telethon==<pin>`, `platformdirs`, and nothing heavy). The JSON Schema files MUST ship inside the package so msgbrowse validates against the same contract. Releases MUST be tagged (`v0.1.0`, …) and MUST publish a wheel so msgbrowse can pin a version and install it hash-compatibly. The package MUST NOT import from msgbrowse or hard-code any msgbrowse path.

#### Scenario: Console script is installed

- **WHEN** the built wheel is `pip install`ed into a clean environment
- **THEN** a `tg-export` console command is available and `--version` prints the tool semver

#### Scenario: Schema ships in the wheel

- **WHEN** the built wheel is inspected
- **THEN** it contains `schema/manifest.schema.json` and `schema/message.schema.json`

#### Scenario: No msgbrowse coupling

- **WHEN** the package's imports and source are scanned
- **THEN** nothing imports from msgbrowse and no msgbrowse path is hard-coded

### Requirement: Testing

The test suite MUST run fully offline with the Telethon client mocked; no test may make a network call. Fixtures MUST be 100% synthetic (invented names, numbers, and text) — a real account's content MUST NEVER appear in the repository. Fixtures MUST cover plain text, links, media, service events, reactions, replies, forwards, an unresolved sender, and a malformed entry. The suite MUST assert generated `manifest.json` and NDJSON against committed golden files and MUST validate every emitted object against the shipped JSON Schema. There MUST be an incremental test proving `min_id` anchoring exports only-new messages and re-emits a boundary safely, and a flood-wait test asserting sleep-and-resume without failure. CI MUST run the linter (ruff), the test suite, build an sdist + wheel, and on a tag run a release job.

#### Scenario: Tests are fully offline and synthetic

- **WHEN** the test suite runs in CI with no network access
- **THEN** every test passes and no test attempts a network connection

#### Scenario: Golden files and schema validation

- **WHEN** the mapping and export tests run against the synthetic fixtures
- **THEN** the generated manifest and NDJSON match the committed golden outputs and validate against the shipped JSON Schema

#### Scenario: No real account content in the repo

- **WHEN** the repository fixtures are reviewed
- **THEN** all names, numbers, and text are synthetic and no real account's content is present
