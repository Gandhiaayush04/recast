#!/usr/bin/env python3
"""Generate small sample files for every format in formats.GROUPS.

Usage:
    python3 tests/make_fixtures.py [OUTPUT_DIR]     (default: tests/fixtures/)

Every file is named sample.<ext>. Idempotent: a file that already exists and is
non-empty is left alone. A format whose engine is missing is skipped with a
warning (the tests that need it will then skip too).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

TESTS_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = TESTS_DIR.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from recast_engine.engines import find  # noqa: E402
from recast_engine.formats import GROUPS  # noqa: E402

DEFAULT_DIR = TESTS_DIR / "fixtures"
SAMPLE_TEXT = "café – naïve – 日本"
W, H = 320, 240
SECONDS = 2


class Missing(Exception):
    """A required engine or Python package is not installed."""


# ---- helpers ----------------------------------------------------------------

def _tmp_for(out: Path) -> Path:
    return out.with_name("tmp_" + out.name)


def _finish(tmp: Path, out: Path) -> None:
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise RuntimeError(f"nothing was written for {out.name}")
    os.replace(tmp, out)


def _run(cmd: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {proc.stderr.strip()[-400:]}")


def _ffmpeg(args: list[str], out: Path) -> None:
    ff = find("ffmpeg")
    if not ff:
        raise Missing("ffmpeg")
    tmp = _tmp_for(out)
    _run([ff, "-y", "-hide_banner", "-loglevel", "error", *args, str(tmp)])
    _finish(tmp, out)


def _soffice(src: Path, out: Path, to_ext: str, infilter: str | None = None) -> None:
    """Convert `src` with LibreOffice into `out` (extension `to_ext`).
    Each call gets its own profile directory so calls never collide."""
    so = find("libreoffice")
    if not so:
        raise Missing("libreoffice")
    profile = Path(tempfile.mkdtemp(prefix="lo-profile-"))
    outdir = Path(tempfile.mkdtemp(prefix="lo-out-"))
    try:
        cmd = [so, f"-env:UserInstallation=file://{profile}", "--headless",
               "--norestore", "--nolockcheck"]
        if infilter:
            cmd.append(f"--infilter={infilter}")
        cmd += ["--convert-to", to_ext, "--outdir", str(outdir), str(src)]
        _run(cmd, timeout=240)
        produced = outdir / f"{src.stem}.{to_ext}"
        if not produced.is_file() or produced.stat().st_size == 0:
            raise RuntimeError(f"LibreOffice produced no {to_ext} file")
        tmp = _tmp_for(out)
        shutil.copyfile(produced, tmp)
        _finish(tmp, out)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


# ---- images -----------------------------------------------------------------

def _base_image() -> Image.Image:
    img = Image.new("RGB", (W, H), (30, 120, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 160, 140], fill=(250, 200, 40))
    d.ellipse([180, 60, 300, 200], fill=(220, 50, 90))
    d.text((24, 200), "Recast sample", fill=(255, 255, 255))
    return img


def _img_jpg(out: Path, scratch: Path) -> None:
    tmp = _tmp_for(out)
    _base_image().save(tmp, "JPEG", quality=90)
    _finish(tmp, out)


def _img_png(out: Path, scratch: Path) -> None:
    rgba = _base_image().convert("RGBA")
    alpha = Image.new("L", (W, H), 255)
    ImageDraw.Draw(alpha).rectangle([0, 0, W // 2, H // 2], fill=0)  # transparent corner
    rgba.putalpha(alpha)
    tmp = _tmp_for(out)
    rgba.save(tmp, "PNG")
    _finish(tmp, out)


def _img_heic(out: Path, scratch: Path) -> None:
    try:
        import pillow_heif
    except ImportError as e:
        raise Missing("pillow-heif") from e
    tmp = _tmp_for(out)
    pillow_heif.from_pillow(_base_image()).save(str(tmp), quality=90)
    _finish(tmp, out)


def _img_webp(out: Path, scratch: Path) -> None:
    tmp = _tmp_for(out)
    _base_image().save(tmp, "WEBP", quality=90)
    _finish(tmp, out)


def _img_tiff(out: Path, scratch: Path) -> None:
    tmp = _tmp_for(out)
    _base_image().save(tmp, "TIFF")
    _finish(tmp, out)


def _img_bmp(out: Path, scratch: Path) -> None:
    tmp = _tmp_for(out)
    _base_image().save(tmp, "BMP")
    _finish(tmp, out)


def _img_gif(out: Path, scratch: Path) -> None:
    """Three-frame animated GIF."""
    frames = []
    for i in range(3):
        f = _base_image()
        ImageDraw.Draw(f).rectangle([i * 40, 0, i * 40 + 30, 30], fill=(255, 0, 0))
        frames.append(f)
    tmp = _tmp_for(out)
    frames[0].save(tmp, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    _finish(tmp, out)


def _pdf(out: Path, scratch: Path) -> None:
    """Three-page PDF, one coloured page each."""
    pages = []
    for i, colour in enumerate([(255, 255, 255), (235, 245, 255), (255, 245, 235)], start=1):
        p = Image.new("RGB", (612, 792), colour)
        ImageDraw.Draw(p).text((72, 72), f"Recast sample page {i}", fill=(0, 0, 0))
        pages.append(p)
    tmp = _tmp_for(out)
    pages[0].save(tmp, "PDF", save_all=True, append_images=pages[1:], resolution=100.0)
    _finish(tmp, out)


# ---- video and audio --------------------------------------------------------

VIDEO_IN = ["-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate=25:duration={SECONDS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={SECONDS}"]
AUDIO_IN = ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={SECONDS}"]
H264_AAC = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]


def _vid_mp4(out, scratch): _ffmpeg([*VIDEO_IN, *H264_AAC], out)
def _vid_mov(out, scratch): _ffmpeg([*VIDEO_IN, *H264_AAC], out)
def _vid_mkv(out, scratch): _ffmpeg([*VIDEO_IN, *H264_AAC], out)
def _vid_webm(out, scratch):
    _ffmpeg([*VIDEO_IN, "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "45", "-deadline", "realtime",
             "-cpu-used", "8", "-c:a", "libopus"], out)
def _vid_avi(out, scratch):
    _ffmpeg([*VIDEO_IN, "-c:v", "mpeg4", "-q:v", "5", "-c:a", "libmp3lame", "-q:a", "4"], out)


def _aud(codec_args: list[str]) -> Callable[[Path, Path], None]:
    def make(out: Path, scratch: Path) -> None:
        _ffmpeg([*AUDIO_IN, "-vn", *codec_args], out)
    return make


_aud_mp3 = _aud(["-c:a", "libmp3lame", "-q:a", "4"])
_aud_wav = _aud(["-c:a", "pcm_s16le"])
_aud_m4a = _aud(["-c:a", "aac", "-b:a", "128k"])
_aud_aac = _aud(["-c:a", "aac", "-b:a", "128k", "-f", "adts"])
_aud_flac = _aud(["-c:a", "flac"])
def _aud_ogg(out: Path, scratch: Path) -> None:
    # Homebrew's ffmpeg may lack libvorbis; fall back to Opus inside an Ogg container.
    try:
        _ffmpeg([*AUDIO_IN, "-vn", "-c:a", "libvorbis", "-q:a", "4"], out)
    except Exception:
        _ffmpeg([*AUDIO_IN, "-vn", "-c:a", "libopus", "-b:a", "96k", "-f", "ogg"], out)


# ---- documents --------------------------------------------------------------

_TXT_BODY = f"{SAMPLE_TEXT}\nSecond line of sample text.\n"
_CSV_BODY = f'item,note\nalpha,"{SAMPLE_TEXT}"\nbeta,plain text\n'

_FODS_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'office:version="1.2" '
    'office:mimetype="application/vnd.oasis.opendocument.spreadsheet">'
    '<office:body><office:spreadsheet>'
)
_FODS_TAIL = '</office:spreadsheet></office:body></office:document>\n'

_FODP_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'office:version="1.2" '
    'office:mimetype="application/vnd.oasis.opendocument.presentation">'
    '<office:body><office:presentation>'
)
_FODP_TAIL = '</office:presentation></office:body></office:document>\n'


def _fods_text(sheets: list[tuple[str, list[list[str]]]]) -> str:
    def cell(v: str) -> str:
        return ('<table:table-cell office:value-type="string">'
                f'<text:p>{escape(v)}</text:p></table:table-cell>')

    def sheet(name: str, rows: list[list[str]]) -> str:
        body = "".join("<table:table-row>" + "".join(cell(c) for c in r) + "</table:table-row>"
                       for r in rows)
        return f'<table:table table:name="{escape(name)}">{body}</table:table>'

    return _FODS_HEAD + "".join(sheet(n, r) for n, r in sheets) + _FODS_TAIL


def _fodp_text(slides: list[str]) -> str:
    pages = "".join(
        f'<draw:page draw:name="Slide {i}">'
        '<draw:frame draw:name="title" svg:x="2cm" svg:y="2cm" svg:width="24cm" svg:height="5cm">'
        f'<draw:text-box><text:p>{escape(s)}</text:p></draw:text-box></draw:frame>'
        '</draw:page>'
        for i, s in enumerate(slides, start=1))
    return _FODP_HEAD + pages + _FODP_TAIL


def _write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _doc_txt(out: Path, scratch: Path) -> None:
    tmp = _tmp_for(out)
    tmp.write_text(_TXT_BODY, encoding="utf-8")
    _finish(tmp, out)


def _doc_csv(out: Path, scratch: Path) -> None:
    tmp = _tmp_for(out)
    tmp.write_text(_CSV_BODY, encoding="utf-8")
    _finish(tmp, out)


def _from_txt(to_ext: str) -> Callable[[Path, Path], None]:
    def make(out: Path, scratch: Path) -> None:
        src = _write_text(scratch / "sample.txt", _TXT_BODY)
        _soffice(src, out, to_ext, infilter="Text (encoded):UTF8")
    return make


_doc_docx = _from_txt("docx")
_doc_odt = _from_txt("odt")
_doc_rtf = _from_txt("rtf")


def _slides(to_ext: str) -> Callable[[Path, Path], None]:
    def make(out: Path, scratch: Path) -> None:
        src = _write_text(scratch / "sample.fodp",
                          _fodp_text([SAMPLE_TEXT, "Slide two"]))
        _soffice(src, out, to_ext)
    return make


_slide_pptx = _slides("pptx")
_slide_odp = _slides("odp")


def _sheets(to_ext: str) -> Callable[[Path, Path], None]:
    def make(out: Path, scratch: Path) -> None:
        src = _write_text(scratch / "sample.fods", _fods_text([
            ("Sheet One", [["name", "note"], ["alpha", SAMPLE_TEXT]]),
            ("Sheet Two", [["city", "value"], ["Dallas", "1"]]),
        ]))
        _soffice(src, out, to_ext)
    return make


_sheet_ods = _sheets("ods")
_sheet_xlsx = _sheets("xlsx")


# ---- registry ---------------------------------------------------------------

MAKERS: dict[str, Callable[[Path, Path], None]] = {
    "jpg": _img_jpg, "png": _img_png, "heic": _img_heic, "webp": _img_webp,
    "tiff": _img_tiff, "bmp": _img_bmp, "gif": _img_gif,
    "mp4": _vid_mp4, "mov": _vid_mov, "mkv": _vid_mkv, "webm": _vid_webm, "avi": _vid_avi,
    "mp3": _aud_mp3, "wav": _aud_wav, "m4a": _aud_m4a, "aac": _aud_aac,
    "flac": _aud_flac, "ogg": _aud_ogg,
    "txt": _doc_txt, "docx": _doc_docx, "odt": _doc_odt, "rtf": _doc_rtf,
    "pptx": _slide_pptx, "odp": _slide_odp,
    "csv": _doc_csv, "xlsx": _sheet_xlsx, "ods": _sheet_ods,
    "pdf": _pdf,
}


def build(out_dir: Path | None = None, verbose: bool = True) -> dict[str, Path | None]:
    """Create every sample file in `out_dir`. Returns {ext: path, or None if skipped}."""
    out_dir = Path(out_dir) if out_dir else DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    all_exts = [e for exts in GROUPS.values() for e in exts]
    missing_makers = sorted(set(all_exts) - set(MAKERS))
    if missing_makers:
        raise RuntimeError(f"no fixture maker for: {missing_makers}")

    scratch = Path(tempfile.mkdtemp(prefix="recast-fixtures-"))
    results: dict[str, Path | None] = {}
    try:
        for ext in all_exts:  # GROUPS order; dependencies (txt, fodp, fods) are written in scratch
            out = out_dir / f"sample.{ext}"
            if out.is_file() and out.stat().st_size > 0:
                results[ext] = out
                continue
            try:
                MAKERS[ext](out, scratch)
                results[ext] = out
                if verbose:
                    print(f"created {out.name}")
            except Missing as e:
                results[ext] = None
                print(f"skipped {ext}: {e} is not installed", file=sys.stderr)
            except Exception as e:  # keep going; the affected tests will fail loudly
                results[ext] = None
                print(f"FAILED {ext}: {e}", file=sys.stderr)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    res = build(target)
    ok = sum(1 for v in res.values() if v)
    print(f"{ok}/{len(res)} fixtures in {target}")
