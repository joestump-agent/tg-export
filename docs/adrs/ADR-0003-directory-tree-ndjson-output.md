---
status: accepted
date: 2026-07-08
decision-makers: Joe Stump
extends: [ADR-0001]
related: [ADR-0004, ADR-0008]
---

# ADR-0003: Emit a directory tree with NDJSON-per-chat plus a manifest

## Context and Problem Statement

A Telegram account can hold gigabytes of history across hundreds of chats. tg-export must write that history in a shape both sides can process without loading it all into memory, that survives a killed run, and that supports cheap incremental appends. What on-disk structure should the export take?

## Decision Drivers

* Bounded memory on both the write side (exporter) and the read side (msgbrowse parser).
* Crash-resumability: a killed run must leave a valid, partial, resumable artifact.
* Cheap incremental refresh (append new messages without rewriting existing data).
* Robustness to a single malformed record — one bad line must not abort a chat or a run.
* Device-sync friendliness (the tree is synced by Syncthing; see ADR-0008).

## Considered Options

* **A — Directory tree: `manifest.json` + `chats/<chat_id>.ndjson` + `media/<chat_id>/...`.**
* **B — A single large JSON file** (one array/object for the whole account).
* **C — A SQLite database** written by the exporter.

## Decision Outcome

Chosen option: **A — directory tree with NDJSON per chat**. One JSON object per line means the exporter appends as it goes (crash-resumable) and msgbrowse reads with a line scanner in bounded memory — no whole-file parse of a multi-GB dump. A malformed line is logged and skipped on both sides; it never aborts the run. The `manifest.json` carries the account record plus a per-chat index and the incremental anchors (ADR-0008). Media lands in `media/<chat_id>/<message_id>[_<n>].<ext>`, referenced by relative path from each message's `media.path`.

### Consequences

* Good — streaming on both sides; gigabyte accounts never need a full in-memory parse.
* Good — append-as-you-go gives free crash-resumability and cheap incrementals.
* Good — a corrupt line degrades to one skipped message, not a failed run.
* Good — plain files sync cleanly over Syncthing and are trivially inspectable.
* Bad — many small files rather than one artifact (acceptable; matches the reader's staging model).
* Neutral — requires a documented layout convention rather than a self-describing single file.

### Confirmation

Golden-file tests assert the generated `manifest.json` and each `chats/*.ndjson` byte-for-byte against committed fixtures, and every emitted object validates against the shipped JSON Schema (ADR-0004). An incremental test proves append-only growth of a chat's ndjson.

## Pros and Cons of the Options

### A — Directory tree + NDJSON per chat

* Good — bounded memory, crash-resumable, append-friendly, corruption-tolerant.
* Good — natural fit for msgbrowse's staging→merge model.
* Bad — layout must be documented and kept stable (that is what the contract/spec is for).

### B — Single JSON file

* Good — one self-contained artifact.
* Bad — requires whole-file parse; blows memory on large accounts.
* Bad — not appendable; every incremental refresh rewrites the whole file.
* Bad — one malformed byte can invalidate the entire document.

### C — SQLite database

* Good — queryable, compact.
* Bad — reintroduces a schema/engine coupling msgbrowse deliberately avoids on the exporter side; msgbrowse owns the SQLite, not the delegate (ADR-0001).
* Bad — binary diffs sync poorly and are opaque to inspect.

## Architecture Diagram

```mermaid
flowchart TD
    OUT[output-dir/] --> MAN[manifest.json\naccount + per-chat index + anchors]
    OUT --> CH[chats/]
    OUT --> MED[media/]
    CH --> C1[777000.ndjson\none message/line]
    MED --> M1[777000/]
    M1 --> F1[48120.jpg]
    M1 --> F2[48121_1.mp4]
    C1 -. media.path .-> F1
```

## More Information

Field-level schema and the `schema_version` lockstep are in ADR-0004 and SPEC-0001. Incremental anchoring via `max_message_id` is ADR-0008. Builds on ADR-0001.
