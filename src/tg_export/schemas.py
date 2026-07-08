"""Runtime access to the shipped JSON Schema contract.

The JSON Schema files (``manifest.schema.json``, ``message.schema.json``) ship
inside this package as package data so that both tg-export and msgbrowse validate
against the exact same contract (ADR-0004, SPEC-0001 REQ "JSON Output Contract").
They are loaded via ``importlib.resources`` so this works from an installed wheel,
not just from a source checkout.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

# Governing: ADR-0004 (shipped JSON Schema, lockstep contract);
#            SPEC-0001 REQ "JSON Output Contract"

#: Logical schema name -> shipped filename.
SCHEMA_FILES: dict[str, str] = {
    "manifest": "manifest.schema.json",
    "message": "message.schema.json",
}


def _filename(name: str) -> str:
    try:
        return SCHEMA_FILES[name]
    except KeyError:
        raise ValueError(
            f"unknown schema {name!r}; expected one of {sorted(SCHEMA_FILES)}"
        ) from None


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Return the parsed JSON Schema for ``name`` ("manifest" or "message")."""
    resource = resources.files("tg_export").joinpath("schema", _filename(name))
    with resource.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@cache
def get_validator(name: str) -> Draft202012Validator:
    """Return a cached draft 2020-12 validator for the named schema."""
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(name: str, instance: Any) -> None:
    """Validate ``instance`` against the named schema, raising on the first error."""
    get_validator(name).validate(instance)


def is_valid(name: str, instance: Any) -> bool:
    """Return ``True`` iff ``instance`` validates against the named schema."""
    return get_validator(name).is_valid(instance)
