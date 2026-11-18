"""HEIC backends: pillow-heif fallback (forced, works on Linux) and Apple sips (macOS only)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from recast_engine.converters import images
from recast_engine.core import Options

HAVE_SIPS = os.path.exists("/usr/bin/sips")


def _noop(_p: float) -> None:
    pass


def _heic_src(fixture_dir: Path) -> Path:
    return fixture_dir / "sample.heic"


def test_force_env_disables_sips(monkeypatch):
    monkeypatch.setenv("RECAST_FORCE_PILLOW_HEIF", "1")
    assert images._have_sips() is False


def test_fallback_heic_to_png_and_png_to_heic(fixture_dir, tmp_path, monkeypatch):
    pytest.importorskip("pillow_heif")
    monkeypatch.setenv("RECAST_FORCE_PILLOW_HEIF", "1")
    opts = Options()

    png = tmp_path / "out.png"
    images.convert_image(_heic_src(fixture_dir), png, opts, _noop)
    with Image.open(png) as im:
        assert im.format == "PNG" and im.size[0] > 0

    heic = tmp_path / "back.heic"
    images.convert_image(png, heic, opts, _noop)
    assert heic.stat().st_size > 0
    with Image.open(heic) as im:   # opener registered by the fallback
        assert im.size[0] > 0


@pytest.mark.skipif(not HAVE_SIPS, reason="needs macOS /usr/bin/sips")
def test_sips_heic_to_jpg(fixture_dir, tmp_path):
    out = tmp_path / "out.jpg"
    images.convert_image(_heic_src(fixture_dir), out, Options(), _noop)
    with Image.open(out) as im:
        assert im.format == "JPEG"


@pytest.mark.skipif(not HAVE_SIPS, reason="needs macOS /usr/bin/sips")
def test_sips_jpg_to_heic(fixture_dir, tmp_path):
    out = tmp_path / "out.heic"
    images.convert_image(fixture_dir / "sample.jpg", out, Options(quality="best"), _noop)
    assert out.stat().st_size > 0
    r = subprocess.run(["/usr/bin/sips", "-g", "format", str(out)], capture_output=True, text=True)
    assert "heic" in r.stdout.lower()


@pytest.mark.skipif(not HAVE_SIPS, reason="needs macOS /usr/bin/sips")
def test_sips_heic_to_pdf(fixture_dir, tmp_path):
    out = tmp_path / "out.pdf"
    images.convert_image(_heic_src(fixture_dir), out, Options(), _noop)
    assert out.stat().st_size > 0
    pypdf = pytest.importorskip("pypdf")
    assert len(pypdf.PdfReader(str(out)).pages) == 1
    # combine path also reads HEIC through sips
    combined = tmp_path / "combined.pdf"
    images.combine_images_to_pdf([_heic_src(fixture_dir), fixture_dir / "sample.png"],
                                 combined, Options(), _noop)
    assert len(pypdf.PdfReader(str(combined)).pages) == 2
