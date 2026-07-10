# Changelog

All notable changes to `tg-export` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The JSON output contract is versioned separately by an integer `schema_version`
(currently `1`); a bump is a coordinated change shipped on both `tg-export` and
[msgbrowse](https://github.com/joestump/msgbrowse) (ADR-0004).

## [0.2.0] - 2026-07-10

**Architecture pivot ([ADR-0011](docs/adrs/ADR-0011-tdl-raw-transform-pivot.md)):
tg-export is now a `tdl --raw` → contract transformer, not a live Telethon exporter.**
The v0.1.0 live surface was built under a misread of the paired msgbrowse decision
(ADR-0022), which delegates Telegram to `tdl` (one-click Telegram Desktop session
import). tg-export becomes the fidelity tier that transforms tdl's `--raw` dump.

### Changed

- **One command, `transform --input <tdl-export> --output <dir>`** — offline,
  deterministic, no login and no network. The fidelity core (`mapping.py`) and the
  `schema_version: 1` contract are unchanged; only the input source changed.
- Structurally malformed tdl input (missing `account.id`, an out-of-contract chat
  `type`) is rejected up front with the dedicated malformed-input exit code `5`
  instead of surfacing as a generic runtime failure mid-transform.

### Added

- `adapter.py` — the single seam reshaping tdl's gotd-flavoured `--raw` JSON into the
  Telethon-shaped objects `mapping` consumes (a **skeleton** pending a real dump; open
  sites marked `TODO(tdl-shape)`, see ADR-0011 verification gates).
- `transform.py` (pipeline) and `archive.py` (contract writer/manifest, salvaged from
  the retired live module).

### Removed

- The live Telethon surface: `auth.py` (login/session/credentials), `export.py`'s
  dialog walk, `reliability.py` (flood-wait), and the Telethon media download. The
  `telethon` and `platformdirs` runtime dependencies are gone; only `jsonschema`
  remains.
- ADR-0002 (engine), ADR-0006 (credentials), and ADR-0009 (session security) are
  superseded by ADR-0011.

## [0.1.0] - 2026-07-09

First release. Feature-complete against SPEC-0001 (milestones M1–M7).

### Added

- **Output contract (`schema_version: 1`)** — a directory tree (`manifest.json` +
  `chats/<chat_id>.ndjson` + `media/<chat_id>/…`) with JSON Schema files shipped
  inside the package (`manifest.schema.json`, `message.schema.json`). Output is
  byte-identical on re-export (determinism, so msgbrowse's content-hash dedupe stays
  correct).
- **Authentication & session** — one-time interactive `login` (phone → code → 2FA)
  writing a `0600`, caller-owned session; all other commands run headless. Credentials
  via `--api-id`/`--api-hash` or `TG_EXPORT_API_ID`/`TG_EXPORT_API_HASH`; none embedded.
  A takeout session is used for the bulk pull.
- **Core export** — dialog walk → full-fidelity per-message mapping (resolved senders,
  service events, reactions, replies, forwards, flattened text, and resolved link URLs
  with no UTF-16 offsets) → schema-validated NDJSON appended as it is produced, with a
  complete manifest written last. Channels are excluded by default; `--chats` opts in.
- **Media** — download to relative `media/<chat_id>/<message_id>[_<n>].<ext>` paths with
  `kind`/`mime`/`size` metadata; `--no-media` for metadata-only; `--max-media-mb N`
  emits honest skip-stubs (`path: null`, `skipped: true`); idempotent re-download.
- **Incremental refresh** — `--since <dir>` reads prior per-chat `max_message_id` anchors
  and fetches only newer messages via `iter_messages(min_id=…)`, appending in place; new
  chats export in full; `--full` ignores anchors; boundary re-emit is byte-stable.
- **Reliability** — `FloodWaitError` is slept and resumed (never fatal); best-effort
  tolerance (one bad message never aborts a chat, one bad chat never aborts the run);
  killed runs resume cleanly from the partial tree; `--json-logs` for machine-ingestible
  structured logs. Logs carry only counts, ids, and paths — never message bodies.
- **Packaging** — pure-Python, `pyproject.toml` + `src/` layout, `tg-export` console
  entry, exact-pinned deps (`telethon==1.36.0`, `platformdirs==4.3.6`,
  `jsonschema==4.23.0`), and CI (ruff + pytest + sdist/wheel build, tag-gated release).

### Security

- No API credential, session blob, auth key, 2FA password, or full phone number is ever
  printed, logged, or committed. The only network egress is to Telegram's data centers;
  no telemetry.

### msgbrowse handoff

- `tool_version`: `0.1.0` · `schema_version`: `1`
- Shipped schema: `tg_export/schema/manifest.schema.json`, `tg_export/schema/message.schema.json`
- For msgbrowse SPEC-0015 / story #209 — the parser pins this version and validates against these files.

[0.1.0]: https://github.com/joestump-agent/tg-export/releases/tag/v0.1.0
