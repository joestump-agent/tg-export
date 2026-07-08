---
status: accepted
date: 2026-07-08
decision-makers: Joe Stump
related: [ADR-0004]
---

# ADR-0005: Resolve link entities to URLs; never emit UTF-16 offsets across the boundary

## Context and Problem Statement

Telegram message formatting (links, bold, mentions) is expressed as *entities* with `offset`/`length` indices. Those indices are **UTF-16 code-unit** offsets — a notorious source of off-by-one corruption when re-indexed in another language's string model. msgbrowse only needs the link URLs. Should tg-export pass through raw entity offsets and make msgbrowse do offset math, or resolve what the consumer needs on the export side?

## Decision Drivers

* msgbrowse must extract links reliably without touching UTF-16 offset arithmetic.
* Correctness over completeness — emitting wrong offsets is worse than omitting them.
* Determinism (ADR-0004): resolved values must be stable across re-exports.
* Keep the message object lean; only carry what the consumer reads.

## Considered Options

* **A — Resolve link entities to their full `url` on the export side; emit `entities: [{type, url}]`.** Other entity types are optional and low-priority.
* **B — Pass through raw entities with UTF-16 `offset`/`length`** and let msgbrowse re-index.
* **C — Emit no entities**; put bare URLs in text only.

## Decision Outcome

Chosen option: **A**. For every link-type entity (`url`, `text_link`), tg-export resolves and emits the full `url` in the entity object. msgbrowse extracts links by reading `entities[].url` directly and never touches offsets. This avoids the UTF-16 trap entirely — the notorious off-by-one class of bug simply cannot occur across the boundary. Emitting other entity types (bold, mention, etc.) is optional and low-priority; the contract only commits to link URLs.

### Consequences

* Good — msgbrowse does zero offset math; link extraction is a field read.
* Good — no UTF-16/UTF-8 re-indexing bugs across the language boundary.
* Good — resolved URLs are deterministic and hash-stable (ADR-0004).
* Bad — rich inline formatting (bold ranges, mention spans) is not faithfully carried in v1 (accepted — msgbrowse doesn't need it; flattened visible text plus link URLs is the contract).
* Neutral — Telethon resolves the URL for `text_link`; `url` entities carry it inline already.

### Confirmation

A mapping test feeds synthetic messages with `url` and `text_link` entities (including multibyte/emoji-adjacent text that would shift UTF-16 offsets) and asserts the emitted `entities[].url` values are correct and offset-independent. No offset field appears in the message schema.

## Pros and Cons of the Options

### A — Resolve to URLs on export

* Good — eliminates an entire bug class; trivial consumer read.
* Good — lean, deterministic message object.
* Bad — drops rich formatting spans in v1 (not needed by the consumer).

### B — Pass through UTF-16 offsets

* Good — full fidelity of entity ranges.
* Bad — forces msgbrowse into UTF-16↔UTF-8 offset math — the exact off-by-one corruption source this ADR avoids.
* Bad — couples the consumer to Telegram's indexing model.

### C — No entities, bare URLs in text

* Good — simplest.
* Bad — loses reliable, structured link extraction; msgbrowse would have to regex URLs out of prose.

## Architecture Diagram

```mermaid
flowchart LR
    MSG[Telethon message\n+ MessageEntityUrl/TextUrl] --> RES[mapping.py\nresolve entity -> url]
    RES --> OUT["message.entities:\n[{type:'url', url:'https://...'}]"]
    OUT --> MB[msgbrowse\nreads entities[].url]
    NOTE[UTF-16 offsets\nnever cross boundary]:::warn -.-> OUT
    classDef warn fill:#3a2a1a,stroke:#f59e0b,color:#fde68a;
```

## More Information

The message object contract is in SPEC-0001; determinism requirement in ADR-0004. This decision is the "avoid the UTF-16 trap" design note from the build brief §3.
