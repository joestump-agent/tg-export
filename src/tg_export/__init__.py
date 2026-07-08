"""tg-export: a delegate Telegram exporter for the msgbrowse ecosystem.

This package extracts a Telegram account's history into an ingestion-ready JSON
archive (a directory tree of NDJSON-per-chat plus a manifest). The JSON output
contract is versioned by an integer ``schema_version`` and shipped as JSON Schema
files inside this package (ADR-0004, SPEC-0001).

``__version__`` is the single source of truth for the tool's semver; the packaging
metadata reads it dynamically and the manifest emits it as ``tool_version``.
"""

# Governing: ADR-0010 (distribution); SPEC-0001 REQ "Packaging and Distribution"
__version__ = "0.1.0"

__all__ = ["__version__"]
