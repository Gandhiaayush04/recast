"""Shared pytest fixtures.

* `fixture_dir` (session): sample files for every format, built once into a temp dir.
* `isolated_output` (autouse): RECAST_OUTPUT_DIR points at a temp dir, so no test
  ever writes to the real ~/Downloads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ENGINE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import make_fixtures  # noqa: E402


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> Path:
    """Directory holding sample.<ext> for every format in formats.GROUPS."""
    out = tmp_path_factory.mktemp("fixtures")
    make_fixtures.build(out, verbose=False)
    return out


@pytest.fixture(autouse=True)
def isolated_output(tmp_path, monkeypatch) -> Path:
    out = tmp_path / "recast-output"
    out.mkdir()
    monkeypatch.setenv("RECAST_OUTPUT_DIR", str(out))
    return out
