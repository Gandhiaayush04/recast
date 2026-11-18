"""Malicious-input tests: each builds a tiny hostile sample on the fly and proves the
matching hardening in the engine. A test skips when the engine it needs is missing."""
from __future__ import annotations

import os
import socket
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from recast_engine import cli, core, engines
from recast_engine.core import ConversionError, Options, find_converter

OPTS = Options()
NOOP = lambda p: None  # noqa: E731


def _run(src: Path, target: str, dst: Path) -> None:
    c = find_converter(src.suffix.lstrip("."), target)
    assert c is not None
    c.fn(src, dst, OPTS, NOOP)


def _need_ffmpeg():
    if not (engines.find("ffmpeg") and engines.find("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")


def _need_lo():
    if not engines.find("libreoffice"):
        pytest.skip("LibreOffice not installed")


# ---- 1. ffmpeg content sniffing ------------------------------------------------

class _Listener:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                self.hits += 1
                c.close()
            except (socket.timeout, OSError):
                pass

    def close(self):
        self._stop = True
        self.t.join(2)
        self.sock.close()


def test_dash_manifest_as_mp4_makes_no_request(tmp_path):
    _need_ffmpeg()
    srv = _Listener()
    try:
        mpd = tmp_path / "evil.mp4"
        mpd.write_text(
            '<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
            'mediaPresentationDuration="PT10S" minBufferTime="PT1S" '
            'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">'
            f'<BaseURL>http://127.0.0.1:{srv.port}/</BaseURL><Period><AdaptationSet mimeType="video/mp4">'
            '<Representation id="1" bandwidth="1000" codecs="avc1.42c01e" width="320" height="240">'
            '<BaseURL>seg.mp4</BaseURL><SegmentBase indexRange="0-100"/></Representation>'
            '</AdaptationSet></Period></MPD>')
        for target in ("mp3", "mkv"):
            with pytest.raises(ConversionError):
                _run(mpd, target, tmp_path / f"out.{target}")
        assert srv.hits == 0
    finally:
        srv.close()


def test_concat_playlist_as_mp4_is_refused(tmp_path):
    _need_ffmpeg()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    evil = tmp_path / "evil.mp4"
    evil.write_text(f"ffconcat version 1.0\nfile '{secret}'\n")
    with pytest.raises(ConversionError):
        _run(evil, "mp3", tmp_path / "out.mp3")


def test_normal_media_still_converts(fixture_dir, tmp_path):
    _need_ffmpeg()
    _run(fixture_dir / "sample.mp3", "wav", tmp_path / "a.wav")
    assert (tmp_path / "a.wav").stat().st_size > 0
    _run(fixture_dir / "sample.mp4", "mp3", tmp_path / "b.mp3")
    assert (tmp_path / "b.mp3").stat().st_size > 0


def test_every_ffmpeg_input_gets_whitelists():
    from recast_engine.converters import media
    out = media._with_safe_input(["-i", "x", "-map", "0"])
    assert out[:out.index("-i")][-4:] == media.SAFE_INPUT_ARGS


# ---- 2. LibreOffice external content -------------------------------------------

def _rewrite_zip(src: Path, dst: Path, edit: dict[str, "callable"]) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename in edit:
                data = edit[info.filename](data.decode("utf-8")).encode("utf-8")
            zout.writestr(info, data)


def _add_rel(rel_type: str, external: bool = True):
    tag = (f'<Relationship Id="rIdEvil" Type="{rel_type}" Target="http://127.0.0.1:9/x"'
           + (' TargetMode="External"' if external else "") + "/>")
    return lambda s: s.replace("</Relationships>", tag + "</Relationships>")


R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


@pytest.mark.parametrize("rel", ["image", "oleObject", "attachedTemplate", "frame", "subDocument",
                                 "video", "audio"])
def test_docx_external_rel_refused(rel, fixture_dir, tmp_path):
    _need_lo()
    bad = tmp_path / "bad.docx"
    _rewrite_zip(fixture_dir / "sample.docx", bad, {"word/_rels/document.xml.rels": _add_rel(R + rel)})
    with pytest.raises(ConversionError) as e:
        _run(bad, "pdf", tmp_path / "o.pdf")
    assert "outside itself" in e.value.message


def test_docx_hyperlink_still_converts(fixture_dir, tmp_path):
    _need_lo()
    ok = tmp_path / "ok.docx"
    _rewrite_zip(fixture_dir / "sample.docx", ok, {"word/_rels/document.xml.rels": _add_rel(R + "hyperlink")})
    _run(ok, "pdf", tmp_path / "o.pdf")
    assert (tmp_path / "o.pdf").stat().st_size > 0


def test_pptx_and_xlsx_external_rels_refused(fixture_dir, tmp_path):
    _need_lo()
    p = tmp_path / "bad.pptx"
    _rewrite_zip(fixture_dir / "sample.pptx", p, {"ppt/_rels/presentation.xml.rels": _add_rel(R + "oleObject")})
    with pytest.raises(ConversionError):
        _run(p, "pdf", tmp_path / "o.pdf")
    x = tmp_path / "bad.xlsx"
    _rewrite_zip(fixture_dir / "sample.xlsx", x, {"xl/_rels/workbook.xml.rels": _add_rel(R + "externalLink")})
    with pytest.raises(ConversionError) as e:
        _run(x, "pdf", tmp_path / "o2.pdf")
    assert "outside itself" in e.value.message


@pytest.mark.parametrize("href", ["http://127.0.0.1:9/x.png", "file:///etc/passwd", "/etc/passwd",
                                  "../../secret.png", "smb://host/share/x.png"])
def test_odt_external_image_refused(href, fixture_dir, tmp_path):
    _need_lo()
    bad = tmp_path / "bad.odt"
    ins = f'<draw:frame><draw:image xlink:href="{href}"/></draw:frame>'
    _rewrite_zip(fixture_dir / "sample.odt", bad,
                 {"content.xml": lambda s: s.replace("</office:text>", ins + "</office:text>")})
    with pytest.raises(ConversionError) as e:
        _run(bad, "pdf", tmp_path / "o.pdf")
    assert "outside itself" in e.value.message


def test_odt_internal_image_ref_and_link_are_allowed(fixture_dir, tmp_path):
    _need_lo()
    ok = tmp_path / "ok.odt"
    ins = ('<text:p><text:a xlink:type="simple" xlink:href="http://example.com/">link</text:a></text:p>')
    _rewrite_zip(fixture_dir / "sample.odt", ok,
                 {"content.xml": lambda s: s.replace("</office:text>", ins + "</office:text>")})
    _run(ok, "pdf", tmp_path / "o.pdf")
    assert (tmp_path / "o.pdf").stat().st_size > 0


def test_ods_table_source_refused(fixture_dir, tmp_path):
    _need_lo()
    bad = tmp_path / "bad.ods"
    ins = '<table:table table:name="X"><table:table-source xlink:href="file:///etc/passwd"/></table:table>'
    _rewrite_zip(fixture_dir / "sample.ods", bad,
                 {"content.xml": lambda s: s.replace("</office:spreadsheet>", ins + "</office:spreadsheet>")})
    with pytest.raises(ConversionError):
        _run(bad, "xlsx", tmp_path / "o.xlsx")


def test_profile_disables_link_updates_and_macros():
    from recast_engine.converters import documents as d
    assert "MacroSecurityLevel" in d.REGISTRY_XCU and "<value>3</value>" in d.REGISTRY_XCU
    assert "DisableMacrosExecution" in d.REGISTRY_XCU
    assert d.REGISTRY_XCU.count('oor:name="Link"') == 2


def test_csv_formulas_stay_text(tmp_path):
    _need_lo()
    csv = tmp_path / "evil.csv"
    csv.write_text('a,b\n=1+1,"=HYPERLINK(""http://127.0.0.1:9/"",""click"")"\n3,4\n')
    out = tmp_path / "o.xlsx"
    _run(csv, "xlsx", out)
    with zipfile.ZipFile(out) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        shared = z.read("xl/sharedStrings.xml").decode()
    assert "<f" not in sheet.replace("<fill", "")
    assert "=1+1" in shared and "=HYPERLINK" in shared


# ---- 3. Image bombs --------------------------------------------------------------

def test_animation_over_budget_falls_back_to_first_frame(tmp_path):
    frames = []
    for i in range(90):                      # 90 * 1100 * 1100 = 108M > 100M
        im = Image.new("P", (1100, 1100), 0)
        im.putpixel((i, i), 1)
        frames.append(im)
    gif = tmp_path / "anim.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=20, loop=0)
    assert Image.open(gif).n_frames == 90
    out = tmp_path / "out.webp"
    _run(gif, "webp", out)
    assert getattr(Image.open(out), "n_frames", 1) == 1


def test_small_animation_keeps_frames(tmp_path):
    frames = [Image.new("RGB", (20, 20), (i * 40, 0, 0)) for i in range(4)]
    gif = tmp_path / "a.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=50, loop=0)
    out = tmp_path / "a.webp"
    _run(gif, "webp", out)
    assert Image.open(out).n_frames == 4


def test_huge_one_bit_png_refused(tmp_path):
    big = tmp_path / "big.png"
    Image.new("1", (10000, 10000), 0).save(big)      # 100M pixels, tiny on disk
    with pytest.raises(ConversionError) as e:
        _run(big, "jpg", tmp_path / "o.jpg")
    assert e.value.message == "This image is too large to convert safely."


def test_eps_renamed_png_refused(tmp_path):
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
    with pytest.raises(ConversionError) as e:
        _run(fake, "jpg", tmp_path / "o.jpg")
    assert e.value.message == "This file looks damaged or isn't really a .png file."


def test_normal_image_still_converts(fixture_dir, tmp_path):
    _run(fixture_dir / "sample.png", "jpg", tmp_path / "o.jpg")
    assert Image.open(tmp_path / "o.jpg").size[0] > 0


# ---- 4. PDF huge pages -------------------------------------------------------------

def _blank_pdf(path: Path, w: int, h: int) -> None:
    from pypdf import PdfWriter
    wr = PdfWriter()
    wr.add_blank_page(w, h)
    with open(path, "wb") as f:
        wr.write(f)


def test_pdf_huge_page_is_scaled_down(tmp_path):
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "huge.pdf"
    _blank_pdf(pdf, 14400, 14400)
    out = tmp_path / "pages"
    out.mkdir()
    _run(pdf, "png", out)
    img = Image.open(next(out.iterdir()))
    assert img.size[0] * img.size[1] <= 64_000_000


def test_pdf_absurd_page_refused(tmp_path):
    pytest.importorskip("pypdfium2")
    pdf = tmp_path / "absurd.pdf"
    _blank_pdf(pdf, 200000, 200000)
    out = tmp_path / "pages"
    out.mkdir()
    with pytest.raises(ConversionError) as e:
        _run(pdf, "png", out)
    assert e.value.message == "This PDF page is too large to convert."


# ---- 5. Stale temp cleanup ------------------------------------------------------------

def _dead_pid() -> int:
    pid = 4_000_000
    while True:
        try:
            os.kill(pid, 0)
            pid += 1
        except ProcessLookupError:
            return pid
        except PermissionError:
            pid += 1


def test_sweep_removes_only_dead_session_dirs(tmp_path):
    dead = tmp_path / f"recast-session-{_dead_pid()}-abc123"
    alive = tmp_path / f"recast-session-{os.getpid()}-abc123"
    other = tmp_path / "unrelated-dir"
    odd = tmp_path / "recast-session-notapid-x"
    for d in (dead, alive, other, odd):
        d.mkdir()
        (d / "f").write_text("x")
    removed = cli.sweep_stale_sessions(str(tmp_path))
    assert removed == [str(dead)]
    assert not dead.exists()
    assert alive.exists() and other.exists() and odd.exists()


# ---- 6. Engine overrides ----------------------------------------------------------------

def test_binary_override_needs_dev_flag(monkeypatch, tmp_path):
    fake = tmp_path / "fakeffmpeg"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("RECAST_FFMPEG", str(fake))
    monkeypatch.delenv("RECAST_DEV", raising=False)
    engines.find.cache_clear()
    try:
        assert engines.find("ffmpeg") != str(fake)
        monkeypatch.setenv("RECAST_DEV", "1")
        engines.find.cache_clear()
        assert engines.find("ffmpeg") == str(fake)
    finally:
        monkeypatch.undo()
        engines.find.cache_clear()


import os as _os
import subprocess as _sp
import pytest as _pytest


@_pytest.mark.skipif(not _os.path.exists("/usr/bin/sandbox-exec"), reason="macOS only")
def test_libreoffice_sandbox_profile_is_valid_and_blocks_internet():
    from recast_engine.converters.documents import SANDBOX_PROFILE
    ok = _sp.run(["/usr/bin/sandbox-exec", "-p", SANDBOX_PROFILE, "/usr/bin/true"], capture_output=True)
    assert ok.returncode == 0, ok.stderr
    blocked = _sp.run(["/usr/bin/sandbox-exec", "-p", SANDBOX_PROFILE, "/usr/bin/curl", "-s", "-m", "5",
                       "-o", "/dev/null", "https://example.com"], capture_output=True)
    assert blocked.returncode != 0
