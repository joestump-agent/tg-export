"""tg-export: the Telegram fidelity transformer for the msgbrowse ecosystem.

tg-export consumes a ``tdl`` Telegram export (``--raw`` MTProto messages, produced
by tdl's one-click, Telegram-Desktop-session-imported dump) and reshapes it into the
msgbrowse JSON output contract: a directory tree of NDJSON-per-chat plus a manifest.
It performs no login and touches no network of its own — auth and extraction are
tdl's job (ADR-0011). The contract is versioned by an integer ``schema_version`` and
shipped as JSON Schema files inside this package (ADR-0004, SPEC-0001).

``__version__`` is the single source of truth for the tool's semver; the packaging
metadata reads it dynamically and the manifest emits it as ``tool_version``.
"""

# Governing: ADR-0010 (distribution), ADR-0011 (transform pivot);
#            SPEC-0001 REQ "Packaging and Distribution"
__version__ = "0.2.0"

__all__ = ["__version__"]
