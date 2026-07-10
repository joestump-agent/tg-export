# tg-export

**A small, standalone CLI that transforms a [`tdl`](https://github.com/iyear/tdl) Telegram export into a clean, ingestion-ready JSON archive.**

`tg-export` is the Telegram **fidelity tier** for the [msgbrowse](https://github.com/joestump/msgbrowse) ecosystem. msgbrowse has one hard rule: it does not write exporters. For Telegram the extraction is done by `tdl` — a single static Go binary that logs in by **importing an installed Telegram Desktop session** (no phone/code/2FA) and dumps messages, including the raw MTProto struct via `--raw`. tg-export then reshapes that `--raw` dump into the curated msgbrowse contract: resolved senders, service events, reactions, replies, forwards, resolved link URLs, and media metadata. It runs **no login and touches no network** — auth is tdl's job, exactly as msgbrowse defers Signal/iMessage/WhatsApp auth to those source apps (see [ADR-0011](docs/adrs/ADR-0011-tdl-raw-transform-pivot.md)).

> **Status:** `v0.2.0` — pivoted from a live Telethon exporter to a tdl-raw transformer (ADR-0011). The offline transform pipeline (`adapter → mapping → schema → archive`), the shipped JSON Schema contract (`schema_version: 1`), and the 100%-synthetic test suite are in place. **The `adapter.py` reshape is a verified skeleton** pending a real `tdl chat export --raw` dump — see the *Verification gates* below. See [`CHANGELOG.md`](CHANGELOG.md).

- **Language:** Python ≥ 3.11 · **Input:** `tdl --raw` export · **License:** MIT
- **Version:** `0.2.0` · **Contract:** `schema_version: 1`
- **Consumed by:** msgbrowse SPEC-0015 (parser built against the same `schema_version`)

## Install

Pure-Python, Python ≥ 3.11. The only runtime dependency is `jsonschema` (the Telethon/MTProto client and its session-path helper are gone — the transform needs neither):

```
pip install tg-export        # from the release
tg-export --version          # -> 0.2.0
```

The two JSON Schema files ship inside the wheel (`tg_export/schema/manifest.schema.json`, `message.schema.json`) so msgbrowse validates against the exact same contract; load them at runtime with `tg_export.schemas.load_schema("manifest"|"message")`.

## Quickstart

```
# 1. tdl imports your installed Telegram Desktop session (no phone/code) and dumps --raw.
#    (tdl is a separate tool; msgbrowse bundles and drives it. See https://github.com/iyear/tdl)
tdl login -T desktop
tdl chat export -c <chat> --all --raw -o tdl-export.json

# 2. tg-export transforms that dump into the msgbrowse archive. No login, no network.
tg-export transform --input tdl-export.json --output ~/tg-archive
```

## What it does (and doesn't)

**In scope**
- Offline, deterministic transform of a tdl `--raw` export into the msgbrowse contract.
- Curated per-message JSON: sender, timestamp, flattened text + resolved link URLs, service events, reactions, replies, forwards, media metadata.
- Byte-stable output so msgbrowse's content-hash import is idempotent.
- A single seam (`adapter.py`) holding the only coupling to tdl's output shape.

**Out of scope**
- Talking to Telegram at all — no login, no session, no network, no credential. That is tdl's job (against an installed Telegram Desktop client).
- Incrementality: tdl's time-window refresh produces the input; msgbrowse dedupes on import. The transform is stateless.
- Any coupling to msgbrowse internals — this tool knows nothing about SQLite or msgbrowse's schema. It writes files.
- A GUI, a daemon, or scheduling — msgbrowse drives invocation.
- Message-content transformation beyond flattening (no summarization, no scrubbing).

## Verification gates (open)

The `adapter.py` reshape from tdl's gotd-flavoured `--raw` JSON to the Telethon-shaped objects the mapper consumes is a **skeleton** until confirmed against a real dump (ADR-0011). Each open site is marked `TODO(tdl-shape)`:

1. **Completeness** — confirm `--raw` carries the full MTProto message (reply/forward/reactions/action/entities/media) and wire each field.
2. **Entity maps** — confirm whether the dump preserves the `users[]`/`chats[]` arrays needed to resolve `from_id → display name` offline. If not, senders degrade to stable id-only contacts (msgbrowse ADR-0003) until reconciled.

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

## CLI surface

```
tg-export transform --input <tdl-export> --output <dir>   # the only command; fully non-interactive
                    [--json-logs]   # emit one JSON log object per line (machine-ingestible)
tg-export --version                                       # prints tool_version (0.2.0)
```

`--input` is a tdl export (a file, or a directory containing `tdl-export.json`); `--output` is the archive directory to write. msgbrowse invokes with explicit argv (never a shell). Exit codes are a small stable contract: `0` OK, `2` malformed argument, `5` missing/malformed tdl input, `1` generic runtime failure.

## Security invariants

- The transform holds **no session, no credential, and opens no socket** — there is no sensitive Telegram artifact to guard here (that surface moved to tdl). The offline test guard fails any test that attempts a network connection.
- Never print or log a message body. Logs get counts, ids, and types only.
- No telemetry, no third-party calls.

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

The JSON Schema contract lives in `schema/` and ships inside the package (`tg_export/schema/`); load it at runtime with `tg_export.schemas.load_schema("manifest"|"message")`. The test suite maps synthetic Telethon-shaped fixtures and an in-memory tdl-raw document through the pipeline, validates every emitted message + manifest against that schema, and asserts byte-identical determinism — no network access, ever.

## License

MIT © Joe Stump
