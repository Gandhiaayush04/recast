"""Keeps the Swift app's copy of the conversion matrix in step with formats.py.

The app ships `formats.json` and computes the "Convert to" menu itself. These tests fail
when formats.py changes without regenerating the two files the app and its tests use:

    RECAST_UPDATE_FIXTURES=1 python -m pytest tests/test_swift_parity.py
"""
import itertools
import json
import os
from pathlib import Path

from recast_engine.cli import analyze_targets, main
from recast_engine.formats import ALIASES, GROUP_OF, export_matrix

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "app" / "Recast" / "Resources" / "formats.json"
CASES = ROOT / "app" / "RecastTests" / "Fixtures" / "matrix_cases.json"
UPDATE = os.environ.get("RECAST_UPDATE_FIXTURES") == "1"


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def build_cases() -> list[dict]:
    singles = sorted(set(GROUP_OF) | set(ALIASES) | {"xyz", ""})
    names = [f"/x/a.{e}" if e else "/x/noext" for e in singles]
    names += ["/x/UPPER.JPG", "/x/Photo.HEIC", "/x/.hidden", "/x/archive.tar.gz"]
    lists = [[n] for n in names]
    rep = ["jpg", "png", "heic", "tif", "mp4", "m4v", "mov", "mp3", "flac", "docx", "xlsx", "csv", "pdf", "xyz"]
    for a, b in itertools.product(rep, rep):
        lists.append([f"/x/1.{a}", f"/x/2.{b}"])
    lists += [
        ["/x/1.jpg", "/x/2.png", "/x/3.heif"],
        ["/x/1.jpg", "/x/2.jpg", "/x/3.jpg"],
        ["/x/1.mp4", "/x/2.mkv", "/x/3.mp3"],
        ["/x/1.pdf", "/x/2.pdf"],
        ["/x/1.docx", "/x/2.odt", "/x/3.txt"],
        ["/x/1.jpg", "/x/2.xyz", "/x/3.png"],
    ]
    return [{"files": f, "expected": analyze_targets(f)} for f in lists]


def _check(path: Path, content: str) -> None:
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert path.read_text() == content, (
        f"{path.relative_to(ROOT)} is out of date. Regenerate with "
        "RECAST_UPDATE_FIXTURES=1 python -m pytest tests/test_swift_parity.py")


def test_formats_json_matches_engine():
    _check(MATRIX, _dump(export_matrix()))


def test_matrix_cases_match_engine():
    _check(CASES, _dump(build_cases()))


def test_formats_command_prints_the_matrix(capsys):
    assert main(["formats"]) == 0
    assert json.loads(capsys.readouterr().out) == export_matrix()
