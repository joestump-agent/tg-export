"""CLI wiring for ``export`` and ``chats`` — offline, mocked client seam.

The auth/open-client seam itself is covered by test_auth; here we assert the CLI
surface: flags flow into an ``ExportConfig``, a run writes the tree and exits 0
non-interactively, the M5-only flags fail fast, and the sentinel exit-code model
(and the ``tg-export: not authorized`` token) still holds for ``export``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from synthetic import FakeTelegramClient
from tg_export import cli, export, schemas
from tg_export.errors import (
    EXIT_MALFORMED_ARG,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    NOT_AUTHORIZED_TOKEN,
    NotAuthorizedError,
)

FAKE_API_ID = "1234567"
FAKE_API_HASH = "0123456789abcdef0123456789abcdef"


def _export_argv(session: Path, output: Path, *extra: str) -> list[str]:
    return [
        "export",
        "--session",
        str(session),
        "--output",
        str(output),
        "--api-id",
        FAKE_API_ID,
        "--api-hash",
        FAKE_API_HASH,
        *extra,
    ]


def _patch_run_export(monkeypatch, *, capture=None):
    def _fake(config, *, session, credential):
        if capture is not None:
            capture.append(config)
        client = FakeTelegramClient()
        return asyncio.run(export.export_with_client(client, config))

    monkeypatch.setattr(export, "run_export", _fake)


def test_export_cli_writes_tree_and_exits_zero(tmp_path, monkeypatch, capsys):
    captured: list[export.ExportConfig] = []
    _patch_run_export(monkeypatch, capture=captured)
    output = tmp_path / "out"
    code = cli.main(_export_argv(tmp_path / "s.session", output))
    assert code == EXIT_OK

    # The written tree validates.
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    schemas.validate("manifest", manifest)

    err = capsys.readouterr().err
    assert "exported 2 chats" in err  # summary carries counts, not bodies
    assert "Anyone up for the ridge loop" not in err
    # Default scope flowed through (no --chats).
    assert captured[0].chats is None


def test_export_chats_flag_parsed_into_filter(tmp_path, monkeypatch, capsys):
    captured: list[export.ExportConfig] = []
    _patch_run_export(monkeypatch, capture=captured)
    code = cli.main(_export_argv(tmp_path / "s.session", tmp_path / "out", "--chats", "2002, 1001"))
    assert code == EXIT_OK
    assert captured[0].chats == frozenset({2002, 1001})


def test_export_no_media_flag_flows(tmp_path, monkeypatch):
    captured: list[export.ExportConfig] = []
    _patch_run_export(monkeypatch, capture=captured)
    cli.main(
        _export_argv(
            tmp_path / "s.session", tmp_path / "out", "--no-media", "--max-media-mb", "5"
        )
    )
    assert captured[0].no_media is True
    assert captured[0].max_media_mb == 5


def test_export_requires_output(tmp_path, capsys):
    code = cli.main(
        [
            "export",
            "--session",
            str(tmp_path / "s.session"),
            "--api-id",
            FAKE_API_ID,
            "--api-hash",
            FAKE_API_HASH,
        ]
    )
    assert code == EXIT_MALFORMED_ARG
    assert "tg-export:" in capsys.readouterr().err


def test_export_full_flag_flows_into_config(tmp_path, monkeypatch):
    # M5: --full is now real and flows through as an ignore-anchors run.
    captured: list[export.ExportConfig] = []
    _patch_run_export(monkeypatch, capture=captured)
    code = cli.main(_export_argv(tmp_path / "s.session", tmp_path / "out", "--full"))
    assert code == EXIT_OK
    assert captured[0].full is True
    assert captured[0].since is None


def test_export_since_flag_flows_into_config(tmp_path, monkeypatch):
    # M5: --since reads a real prior manifest; run once to produce it, then resume.
    captured: list[export.ExportConfig] = []
    _patch_run_export(monkeypatch, capture=captured)
    tree = tmp_path / "tree"
    assert cli.main(_export_argv(tmp_path / "s.session", tree)) == EXIT_OK
    code = cli.main(_export_argv(tmp_path / "s.session", tree, "--since", str(tree)))
    assert code == EXIT_OK
    assert captured[1].since == tree
    assert captured[1].full is False


def test_export_since_defaults_output_to_since_tree(tmp_path, monkeypatch):
    # ADR-0008 one-evolving-tree: omitting --output on a --since run appends into
    # the since tree rather than requiring the caller to repeat the path.
    captured: list[export.ExportConfig] = []
    _patch_run_export(monkeypatch, capture=captured)
    tree = tmp_path / "tree"
    assert cli.main(_export_argv(tmp_path / "s.session", tree)) == EXIT_OK
    code = cli.main(
        [
            "export",
            "--session",
            str(tmp_path / "s.session"),
            "--since",
            str(tree),
            "--api-id",
            FAKE_API_ID,
            "--api-hash",
            FAKE_API_HASH,
        ]
    )
    assert code == EXIT_OK
    assert captured[1].output == tree
    assert captured[1].since == tree


def test_export_since_with_different_output_is_malformed(tmp_path, capsys):
    # Appending into a tree other than the --since tree would drop prior lines and
    # silently produce an incomplete archive — refuse it with the arg exit code.
    code = cli.main(
        _export_argv(
            tmp_path / "s.session", tmp_path / "other", "--since", str(tmp_path / "prev")
        )
    )
    assert code == EXIT_MALFORMED_ARG
    err = capsys.readouterr().err
    assert "--since" in err
    assert "same tree" in err


def test_export_bad_chats_filter_is_malformed(tmp_path, capsys):
    code = cli.main(_export_argv(tmp_path / "s.session", tmp_path / "out", "--chats", "not-an-id"))
    assert code == EXIT_MALFORMED_ARG
    assert "tg-export:" in capsys.readouterr().err


def test_export_not_authorized_emits_token_and_code(tmp_path, monkeypatch, capsys):
    def _raise(config, *, session, credential):
        raise NotAuthorizedError("tg-export: session is not authorized")

    monkeypatch.setattr(export, "run_export", _raise)
    code = cli.main(_export_argv(tmp_path / "s.session", tmp_path / "out"))
    assert code == EXIT_NOT_AUTHORIZED
    assert NOT_AUTHORIZED_TOKEN in capsys.readouterr().err.splitlines()


def test_chats_json_lists_all_dialogs(tmp_path, monkeypatch, capsys):
    def _fake_list(*, session, credential):
        client = FakeTelegramClient()
        return asyncio.run(export.list_chats_with_client(client))

    monkeypatch.setattr(export, "list_chats", _fake_list)
    code = cli.main(
        [
            "chats",
            "--session",
            str(tmp_path / "s.session"),
            "--json",
            "--api-id",
            FAKE_API_ID,
            "--api-hash",
            FAKE_API_HASH,
        ]
    )
    assert code == EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    assert {c["id"] for c in listing} == {1001, 2002, 5005}


def test_chats_table_output(tmp_path, monkeypatch, capsys):
    def _fake_list(*, session, credential):
        client = FakeTelegramClient()
        return asyncio.run(export.list_chats_with_client(client))

    monkeypatch.setattr(export, "list_chats", _fake_list)
    code = cli.main(
        [
            "chats",
            "--session",
            str(tmp_path / "s.session"),
            "--api-id",
            FAKE_API_ID,
            "--api-hash",
            FAKE_API_HASH,
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "2002\tchannel\tTrail Alerts" in out
