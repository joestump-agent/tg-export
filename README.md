# tg-export

**A small, standalone Telethon CLI that extracts a Telegram account's history into a clean, ingestion-ready JSON archive.**

`tg-export` is a **delegate exporter** for the [msgbrowse](https://github.com/joestump/msgbrowse) ecosystem. msgbrowse has one hard architectural rule: it does not write exporters — extraction is always delegated to a dedicated, provider-targeted tool whose output msgbrowse ingests. Telegram needed a delegate that didn't exist yet (tdl drops non-`--raw` senders and `--raw` leaks MTProto; Telegram Desktop's export is manual). So this tool emits a JSON shape *designed for ingestion* — senders, service events, reactions, media, and true incrementality.

> **Status:** early implementation. The architecture and contract are captured as ADRs (`docs/adrs/`) and a specification (`docs/openspec/specs/`); the implementation backlog is tracked as GitHub issues, milestones **M1–M7**. **M1** has landed: the package scaffold, the shipped JSON Schema contract (`schema/`), the canonical deterministic serializer, and the offline synthetic test harness. The Telegram-facing commands (`login`/`export`/`chats`/`doctor`) land in M2–M6.

- **Language:** Python ≥ 3.11 · **Engine:** [Telethon](https://docs.telethon.dev/) (MTProto) · **License:** MIT
- **Consumed by:** msgbrowse SPEC-0015 (parser built against the same `schema_version`)

## What it does (and doesn't)

**In scope**
- One-time interactive login; thereafter fully non-interactive exports.
- Full-account export: private chats, groups, supergroups (channels opt-in via `--chats`).
- Curated per-message JSON: sender, timestamp, flattened text + resolved link URLs, service events, reactions, replies, forwards.
- Media download to relative paths (photos, video, voice, video notes, audio, stickers, animations, documents).
- True incremental refresh via per-chat message-id anchors.
- Flood-wait-resilient, resumable, structured-logging, pip-installable, pinned releases.

**Out of scope**
- Secret chats (device-bound E2E, not reachable via MTProto client APIs).
- Any coupling to msgbrowse internals — this tool knows nothing about SQLite or msgbrowse's schema. It writes files.
- Sending, editing, deleting, or any write to Telegram. Read-only.
- A GUI, a daemon, or scheduling — msgbrowse drives invocation.
- Message-content transformation beyond flattening (no summarization, no scrubbing).

## The output contract

Output is a **directory tree**, not a single file, so gigabyte accounts stream on both write and read sides. Everything is `schema_version: 1`; a bump is a coordinated change on both tg-export and msgbrowse.

```
<output-dir>/
  manifest.json            # account + per-chat index and export anchors
  chats/
    <chat_id>.ndjson       # one message object per line (newline-delimited JSON)
  media/
    <chat_id>/
      <message_id>.jpg      # referenced by relative path from a message's media.path
      <message_id>_1.mp4    # _N suffix when one message carries multiple files
```

NDJSON-per-chat means the exporter appends as it goes (crash-resumable) and msgbrowse reads with a line scanner in bounded memory — no whole-file parse of a multi-GB dump. A malformed line is logged and skipped on both sides; it never aborts the run.

The authoritative field-by-field contract lives in **`docs/openspec/specs/`** and is shipped as machine-checkable JSON Schema files (`manifest.schema.json`, `message.schema.json`) inside the package so both repos validate against one source of truth.

## CLI surface (planned)

```
tg-export login   --session <path> [--api-id N] [--api-hash H]   # one-time interactive auth
tg-export export  --session <path> --output <dir>                # the workhorse (non-interactive)
                  [--since <dir>]     # incremental: resume from that dir's manifest anchors
                  [--full]            # ignore anchors, re-export everything
                  [--chats ID,ID,...] # restrict to specific chats (opt-in for channels)
                  [--no-media]        # metadata only
                  [--max-media-mb N]  # skip files larger than N MB, leaving a skip-stub
tg-export chats   --session <path> [--json]                      # list dialogs
tg-export doctor  --session <path>                               # verify session is valid/authorized
tg-export --version                                              # prints tool_version
```

msgbrowse invokes with explicit argv (never a shell) and controls the `--session` path. Every command is non-interactive except `login`. An unauthorized/expired session prints a stable `tg-export: not authorized` token and exits with a dedicated non-zero code, distinct from network or malformed-arg errors.

## Security invariants

- The session file (Telethon auth keys, a small SQLite DB) is the sensitive artifact. It lives where the caller points `--session`, is created `0600`, and never inside the synced archive root.
- Never print, log, or copy session contents, auth keys, the 2FA password, or the full phone number. Logs get counts, ids, and paths only.
- The only network egress is to Telegram's data centers, during `login` and `export`. No telemetry, no third-party calls.

## Development

This repository is governed by the [SDD plugin](https://github.com/joestump/claude-plugin-sdd). See `CLAUDE.md` for the workflow. Design artifacts:

- **ADRs:** `docs/adrs/` — the architectural decisions
- **Spec:** `docs/openspec/specs/` — the requirements and the JSON contract
- **Backlog:** GitHub issues on this repo, milestones M1–M7

### Local setup

Python ≥ 3.11. From a fresh virtualenv:

```
python -m pip install -e '.[dev]'   # runtime + dev deps (ruff, pytest, build)
ruff check .                        # lint
python -m pytest -q                 # fully offline, 100% synthetic fixtures
python -m build                     # sdist + wheel into dist/
```

The JSON Schema contract lives in `schema/` and ships inside the package (`tg_export/schema/`); load it at runtime with `tg_export.schemas.load_schema("manifest"|"message")`. The test suite validates the committed golden export tree (`tests/fixtures/golden/`) against that schema and asserts byte-identical determinism — no network access, ever.

## License

MIT © Joe Stump
