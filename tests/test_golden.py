"""Golden-file guard: the committed export tree MUST match what the current
serializer + fixtures produce, byte-for-byte (SPEC-0001 REQ "Testing")."""

from __future__ import annotations

from pathlib import Path

import synthetic


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_regenerated_tree_matches_committed_golden(golden_dir: Path, tmp_path: Path):
    fresh = tmp_path / "fresh"
    synthetic.write_golden(fresh)

    committed = _tree(golden_dir)
    regenerated = _tree(fresh)

    assert set(regenerated) == set(committed), (
        "golden tree file set drifted; regenerate tests/fixtures/golden"
    )
    for rel, data in committed.items():
        assert regenerated[rel] == data, (
            f"{rel} drifted from committed golden; regenerate tests/fixtures/golden"
        )
