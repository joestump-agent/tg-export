"""Packaging invariants: version single-source, schema importable at runtime,
console entry point present, no msgbrowse coupling
(SPEC-0001 REQ "Packaging and Distribution")."""

from __future__ import annotations

import ast
import pathlib

import tg_export
from tg_export import cli, schemas

_SRC = pathlib.Path(tg_export.__file__).parent
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def test_no_msgbrowse_coupling():
    # SPEC-0001 REQ "Packaging and Distribution": nothing imports from msgbrowse and
    # no msgbrowse path is hard-coded. (Descriptive prose mentioning msgbrowse is fine;
    # an import or a filesystem path is not.)
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not a.name.split(".")[0] == "msgbrowse" for a in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "msgbrowse", path
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # No hard-coded msgbrowse filesystem path (a path-shaped literal).
                assert "/msgbrowse" not in node.value and "msgbrowse/" not in node.value, path


def test_version_is_dynamic_single_source():
    # pyproject must derive the version from tg_export.__version__ (dynamic), so the
    # packaged metadata can never drift from the runtime __version__ / manifest.tool_version.
    import tomllib

    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "version" in project.get("dynamic", []), "version must be dynamic"
    assert "version" not in project, "no static version key may shadow the dynamic one"
    assert data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "tg_export.__version__"
