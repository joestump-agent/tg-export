---
status: accepted
date: 2026-07-10
decision-makers: Joe Stump
amends: [ADR-0001]
supersedes: [ADR-0002, ADR-0006, ADR-0009]
related: [msgbrowse ADR-0022]
---

# ADR-0011: tg-export is a tdl-raw transformer, not a live Telethon exporter

## Context and Problem Statement

ADR-0001 established tg-export as the delegate exporter for Telegram, and ADR-0002
chose Telethon (MTProto) as its extraction engine — a full live client with its own
login, session file, credentials, takeout mode, and flood-wait handling (ADR-0006,
ADR-0009). That whole live surface was built (v0.1.0) under a misreading of the
paired decisions on the msgbrowse side.

msgbrowse's **ADR-0022** independently chose to delegate Telegram to
[`tdl`](https://github.com/iyear/tdl): a single static Go binary that (a) logs in by
**importing an installed Telegram Desktop session** (`tdl login -T desktop`) — no
phone/code/2FA, the same "auth is the source app's job" pattern msgbrowse already
uses for Signal, iMessage, and WhatsApp — and (b) dumps messages, including the raw
MTProto message struct via `--raw`. tg-export's ADR-0001/ADR-0002 rejected tdl on
fidelity grounds (its *curated* JSON drops resolved senders; only `--raw` carries the
full message). Both observations are true, and together they resolve — not into a
rivalry, but into a two-tier split:

* **tdl** is the load-bearing, one-click **session + dump** tier.
* **tg-export** is the **fidelity** tier: it transforms tdl's `--raw` MTProto output
  into the curated msgbrowse contract (resolved senders, service events, reactions,
  replies, forwards, link-entity resolution, media metadata).

The key realization: tg-export's fidelity brain — `mapping.map_message` — is already
a *pure, offline* transform over Telethon-shaped objects. It never needed a live
client; it needs correctly-shaped input. tdl's `--raw` is that input (the same
underlying MTProto messages, serialized by gotd). So the live half of tg-export was
not just redundant — it was the wrong tool built on a misread contract.

## Decision Drivers

* Honor msgbrowse ADR-0022's one-click onboarding (Telegram Desktop session import)
  while keeping the fidelity tdl's curated export lacks.
* Auth belongs to the source app, not to tg-export — consistent with every other
  msgbrowse source. tg-export should hold no session, no credential, no network.
* Keep the coupling surface small and one-directional: the input is tdl's `--raw`
  output; the output is the unchanged `schema_version: 1` contract.
* Preserve the proven, tested fidelity core (`mapping.py`) untouched.

## Decision Outcome

**tg-export is re-scoped to a single offline transform: `tdl --raw` → msgbrowse JSON
contract.** It exposes one command, `transform --input <tdl-export> --output <dir>`,
touches no network, and depends only on `jsonschema`.

Concretely this ADR:

* **Deletes** the live surface — `auth.py` (login/session/credentials), `export.py`'s
  dialog walk, `reliability.py` (flood-wait), and the Telethon `download_media` path —
  and drops the `telethon` and `platformdirs` dependencies.
* **Keeps** the fidelity core: `mapping.py` (the jewel), `jsonio.py`, `schemas.py`,
  and the contract-writer/manifest helpers (salvaged into `archive.py`).
* **Adds** `adapter.py` — the single seam that reshapes tdl's gotd-flavoured `--raw`
  JSON into the Telethon-shaped objects `mapping` consumes — and `transform.py`, the
  pipeline that runs `adapter → mapping → schema → archive`.
* **Supersedes** ADR-0002 (engine), ADR-0006 (credentials), ADR-0009 (session
  security + sentinel exits): with no session, credential, or Telegram connection,
  those decisions are moot. The exit-code contract shrinks to OK / malformed-arg /
  malformed-input / runtime.
* **Amends** ADR-0001: tg-export remains the Telegram *delegate*, but the delegation
  is now "transform tdl's dump," and the input coupling is tdl's `--raw` shape rather
  than Telegram's protocol.

### Consequences

* Good — one-click onboarding via tdl's Telegram Desktop session import; no
  my.telegram.org credential, no phone/code prompt in tg-export.
* Good — tg-export holds no secrets and opens no sockets; the sensitive-artifact
  surface (ADR-0009) disappears rather than needing containment.
* Good — the tested fidelity core is reused verbatim; only the input source changes.
* Neutral — Telegram support now **requires an installed Telegram Desktop client**
  (tdl's session source). Accepted: this matches every other msgbrowse source, which
  reads a locally-installed app's state. Telegram-mobile-only users have no path.
* Bad — tg-export gains a version-coupling to tdl's `--raw` serialization, quarantined
  entirely in `adapter.py`.

### Confirmation

The package imports no `telethon`/`platformdirs` and opens no network (grep- and
test-verified: the offline `no_network` guard stays autouse). The transform maps an
in-memory tdl-raw document end-to-end and every emitted message + the manifest
validate against the shipped schema; output is byte-stable across runs.

## VERIFICATION GATES (open until a real tdl dump is inspected)

The `adapter.py` reshape is a **skeleton** pending confirmation against a real
`tdl chat export -c <chat> --all --raw` dump:

1. **Completeness** — confirm `--raw` emits the full MTProto `Message`/`MessageService`
   (so `reply_to`, `fwd_from`, `reactions`, `action`, `entities`, `media` are present),
   and wire each into the Telethon-shaped shim.
2. **Entity maps** — a raw message's `from_id` is a bare peer id; display names live in
   a separate `users[]`/`chats[]` array. Confirm whether tdl's dump preserves that map.
   If yes, senders resolve to names offline; if no, senders degrade to id-only — still a
   stably-keyed contact for msgbrowse (ADR-0003), just nameless until reconciled.

Each open site is marked `TODO(tdl-shape)` in `adapter.py`/`transform.py`, so closing a
gate is a local edit there.

## Alternatives considered

* **Keep the live Telethon exporter as the primary path.** Rejected: duplicates tdl,
  reintroduces the my.telegram.org + phone/code friction msgbrowse ADR-0022 already
  solved, and holds a session/credential surface no other source needs.
* **Layered merge** (tdl fast import + tg-export full backfill, deduped in msgbrowse).
  Rejected: two logins, a dedup problem, and cross-source reconciliation msgbrowse's
  content-hash import doesn't need.
* **tdl for auth only, tg-export does a live export with the imported session.**
  Rejected: still a live client + credential surface; the `--raw` dump already carries
  the data, so a second live pull is wasted.

## More Information

Consumes tdl's `--raw` output; emits SPEC-0001's `schema_version: 1` contract
unchanged. Builds on ADR-0001; supersedes ADR-0002/0006/0009. Counterpart on the
consuming side is msgbrowse ADR-0022 (tdl as the one-click session+dump tier).
