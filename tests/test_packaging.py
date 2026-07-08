"""Packaging invariants: version single-source, schema importable at runtime,
console entry point present (SPEC-0001 REQ "Packaging and Distribution")."""

from __future__ import annotations

import tg_export
from tg_export import cli, schemas


def test_version_is_single_source_semver():
    assert tg_export.__version__ == "0.1.0"


def test_schema_loads_from_installed_package():
    # Exercises the importlib.resources path msgbrowse relies on from a wheel.
    manifest = schemas.load_schema("manifest")
    message = schemas.load_schema("message")
    assert manifest["$id"].endswith("manifest.schema.json")
    assert message["$id"].endswith("message.schema.json")


def test_unknown_schema_name_raises():
    import pytest

    with pytest.raises(ValueError):
        schemas.load_schema("nope")


def test_version_flag_prints_semver(capsys):
    import pytest

    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == tg_export.__version__


def test_unimplemented_command_exits_nonzero():
    assert cli.main(["export"]) != 0
