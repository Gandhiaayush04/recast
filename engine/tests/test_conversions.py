"""Every (source, target) pair in formats.targets_for, run through the real CLI.

Needs the fixtures from make_fixtures.py (built once by the session fixture) and the
real converter modules. A pair is skipped when its engine is not installed, and fails
when no converter is registered for it. Video sources are marked `slow`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from recast_engine import cli, core, engines
from recast_engine.formats import GROUP_OF, GROUPS, VIDEO, targets_for

# Conversions that must produce a folder of several files (one per page / sheet).
MULTI_PAIRS = {("pdf", "png"), ("pdf", "jpg"), ("xlsx", "csv"), ("ods", "csv")}


def _pairs():
    params = []
    for exts in GROUPS.values():
        for src in exts:
            for tgt in targets_for(src):
                marks = [pytest.mark.slow] if GROUP_OF[src] == VIDEO else []
                params.append(pytest.param(src, tgt, id=f"{src}->{tgt}", marks=marks))
    return params


def _missing_engine(src: str, tgt: str, engine: str) -> str | None:
    """Reason to skip, or None when the engine for this pair is available."""
    if engine == "ffmpeg" and not (engines.find("ffmpeg") and engines.find("ffprobe")):
        return "ffmpeg/ffprobe not installed"
    if engine == "libreoffice" and not engines.find("libreoffice"):
        return "LibreOffice not installed"
    if engine == "pdfium":
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            return "pypdfium2 not installed"
    if engine == "pillow":
        try:
            import PIL  # noqa: F401
        except ImportError:
            return "Pillow not installed"
    if "heic" in (src, tgt):
        try:
            import pillow_heif  # noqa: F401
        except ImportError:
            return "pillow-heif not installed"
    return None


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("src, tgt", _pairs())
def test_conversion(src, tgt, fixture_dir, tmp_path, capsys):
    src_file = fixture_dir / f"sample.{src}"
    if not src_file.is_file():
        pytest.skip(f"no fixture for .{src} (its engine was missing when it was built)")

    conv = core.find_converter(src, tgt)
    if conv is None:
        pytest.fail(f"no converter registered for {src} -> {tgt}")

    reason = _missing_engine(src, tgt, conv.engine)
    if reason:
        pytest.skip(reason)

    out = tmp_path / "out"
    code = cli.main(["convert", "--to", tgt, "--out", str(out), str(src_file)])
    events = _events(capsys.readouterr().out)

    failed = [e for e in events if e["event"] == "failed"]
    assert not failed, f"{failed[0]['message']} {failed[0].get('detail', '')}" if failed else ""
    done = [e for e in events if e["event"] == "done"]
    assert len(done) == 1, events
    assert code == 0

    outputs = [Path(p) for p in done[0]["outputs"]]
    assert outputs, "no output files reported"
    if (src, tgt) in MULTI_PAIRS:
        assert len(outputs) > 1, f"expected several files for {src}->{tgt}"
        assert all(p.parent == out for p in outputs)  # default: separate files
        # with --folder they land in one folder named after the source
        out2 = tmp_path / "out2"
        cli.main(["convert", "--to", tgt, "--folder", "--out", str(out2), str(src_file)])
        ev2 = _events(capsys.readouterr().out)
        outs2 = [Path(p) for e in ev2 if e["event"] == "done" for p in e["outputs"]]
        assert len(outs2) == len(outputs)
        assert all(p.parent == out2 / src_file.stem for p in outs2)
    else:
        assert len(outputs) == 1, f"expected one file for {src}->{tgt}, got {len(outputs)}"

    for p in outputs:
        assert p.is_file(), p
        assert p.stat().st_size > 0, f"empty output {p}"
        assert p.suffix == f".{tgt}", p
