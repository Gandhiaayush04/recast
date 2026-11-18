"""Image converters (engine "pillow"): any image format -> any other image
format, and image -> PDF. Also exposes `combine_images_to_pdf`, which is not
registered but imported by cli.py for the "Combine into one PDF" feature.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageSequence

from ..core import ConversionError, Options, Progress, converter
from ..formats import GROUPS, IMAGE, normalize_ext

log = logging.getLogger(__name__)

# --- Safety limits ---------------------------------------------------------------
MAX_PIXELS = 64_000_000
MAX_ANIMATION_PIXELS = 100_000_000   # frames * width * height
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
# Only decoders for formats Recast supports (HEIF only reaches Pillow via pillow-heif).
OPEN_FORMATS = ["JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF", "MPO", "HEIF", "HEIC"]
TOO_LARGE = "This image is too large to convert safely."
_open_lock = threading.Lock()


def _safe_open(path, formats=None) -> Image.Image:
    """Image.open + full decode with decompression-bomb warnings turned into errors
    and only the allowed decoders. Raises Pillow's own exceptions."""
    with _open_lock, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        Image.init()   # make sure every decoder plugin is registered
        allowed = [f for f in (formats or OPEN_FORMATS) if f in Image.OPEN]
        img = Image.open(path, formats=allowed)
        try:
            img.load()  # force decode now so damaged files fail here
        except BaseException:
            img.close()
            raise
        return img

# --- HEIC backends -------------------------------------------------------------
# On macOS all HEIC reading/writing goes through Apple's built-in /usr/bin/sips, so the
# GPL-licensed pillow-heif wheel doesn't have to ship in the app. pillow-heif is only a
# fallback (e.g. the Linux dev/test box) and is imported lazily, never at module import.
SIPS_PATH = "/usr/bin/sips"
SIPS_TIMEOUT = 120
_pillow_heif_state: bool | None = None   # None = not tried yet


def _have_sips() -> bool:
    if os.environ.get("RECAST_FORCE_PILLOW_HEIF") == "1":
        return False
    return os.path.exists(SIPS_PATH) and os.access(SIPS_PATH, os.X_OK)


def _have_pillow_heif() -> bool:
    """Import pillow-heif on first use and register its opener."""
    global _pillow_heif_state
    if _pillow_heif_state is None:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _pillow_heif_state = True
        except Exception as e:  # pragma: no cover - depends on local install
            log.debug("pillow-heif unavailable: %s", e)
            _pillow_heif_state = False
    return _pillow_heif_state


def _heic_available() -> bool:
    return _have_sips() or _have_pillow_heif()


def _run_sips(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([SIPS_PATH, *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=SIPS_TIMEOUT,
                          env={"HOME": os.environ.get("HOME", "/tmp"),
                               "PATH": "/usr/bin:/bin"})


# --- Quality mapping (single place) ------------------------------------------
LOSSY_QUALITY = {"smaller": 60, "balanced": 82, "best": 95}   # jpg / webp / heic / pdf pages
PNG_COMPRESS = {"smaller": 9, "balanced": 6, "best": 4}       # lossless; higher = smaller file, slower
TIFF_COMPRESSION = {"smaller": "tiff_adobe_deflate", "balanced": "tiff_lzw", "best": "tiff_lzw"}

PDF_DPI = 150
FORMAT_NAME = {"jpg": "JPEG", "png": "PNG", "heic": "HEIF", "webp": "WEBP",
               "tiff": "TIFF", "bmp": "BMP", "gif": "GIF", "pdf": "PDF"}
NO_ALPHA = {"jpg", "bmp", "pdf"}   # targets that can't hold transparency
ANIMATED = {"gif", "webp"}         # targets that can keep animation

_IMAGE_EXTS = list(GROUPS[IMAGE])


# --- Helpers ------------------------------------------------------------------

def _heic_error() -> ConversionError:
    return ConversionError("HEIC support isn't available on this Mac.")


def _open_heic_with_sips(src: Path) -> Image.Image:
    """Decode a HEIC with sips into a temp PNG and return it fully loaded in memory.

    sips applies the orientation itself and writes a PNG without an EXIF orientation
    tag, so the exif_transpose that follows is a no-op (no double rotation)."""
    ext = normalize_ext(src.suffix)
    damaged = ConversionError(f"This file looks damaged or isn't really a .{ext} file.")
    tmpdir = tempfile.mkdtemp(prefix="recast-sips-")
    try:
        out = Path(tmpdir) / "in.png"
        try:
            r = _run_sips(["-s", "format", "png", str(src), "--out", str(out)])
        except subprocess.TimeoutExpired:
            raise ConversionError("This image took too long to convert.")
        except OSError as e:
            log.debug("sips failed to start: %r", e)
            raise _heic_error()
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            log.debug("sips decode failed (%s): %s %s", r.returncode, r.stdout, r.stderr)
            raise damaged
        try:
            return _safe_open(out, ["PNG"])
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ConversionError(TOO_LARGE)
        except Exception as e:
            log.debug("sips png open failed: %r", e)
            raise damaged
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _save_heic_with_sips(img: Image.Image, dst: Path, opts: Options) -> None:
    """Write `img` as PNG (keeps alpha) to a temp dir, then let sips encode the HEIC."""
    tmpdir = tempfile.mkdtemp(prefix="recast-sips-")
    try:
        tmp_png = Path(tmpdir) / "out.png"
        try:
            img.save(tmp_png, format="PNG")
        except Exception as e:
            log.debug("temp png save failed: %r", e)
            raise ConversionError("Couldn't save this image as HEIC.")
        q = str(LOSSY_QUALITY[opts.quality])
        try:
            r = _run_sips(["-s", "format", "heic", "-s", "formatOptions", q,
                           str(tmp_png), "--out", str(dst)])
        except subprocess.TimeoutExpired:
            raise ConversionError("Saving this image as HEIC took too long.")
        except OSError as e:
            log.debug("sips failed to start: %r", e)
            raise _heic_error()
        if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            log.debug("sips encode failed (%s): %s %s", r.returncode, r.stdout, r.stderr)
            raise ConversionError("Couldn't save this image as HEIC.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _open(src: Path) -> Image.Image:
    """Open an image, mapping every expected failure to a plain-English error."""
    ext = normalize_ext(src.suffix)
    if ext == "heic":
        if _have_sips():
            return _open_heic_with_sips(src)
        if not _have_pillow_heif():
            raise _heic_error()
    try:
        return _safe_open(src)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ConversionError(TOO_LARGE)
    except (Image.UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as e:
        log.debug("open failed for %s: %s", src, e)
        raise ConversionError(f"This file looks damaged or isn't really a .{ext} file.")
    except Exception as e:  # e.g. pillow-heif decode errors
        log.debug("open failed for %s: %r", src, e)
        raise ConversionError(f"This file looks damaged or isn't really a .{ext} file.")


def _has_alpha(img: Image.Image) -> bool:
    return (img.mode in ("RGBA", "LA", "PA", "La", "RGBa")
            or (img.mode == "P" and "transparency" in img.info)
            or (img.mode in ("L", "RGB", "1") and "transparency" in img.info))


def _to_8bit(img: Image.Image) -> Image.Image:
    """Bring 16/32-bit and float grayscale modes down to 8-bit."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N"):
        return img.point(lambda v: v * (1 / 256)).convert("L")
    if img.mode == "I":
        return img.point(lambda v: v * (1 / 256)).convert("L")
    if img.mode == "F":
        return img.point(lambda v: v * 255).convert("L")
    return img


def _normalize(img: Image.Image, target: str) -> Image.Image:
    """Return an image in a mode the target format can store."""
    img = _to_8bit(img)
    alpha = _has_alpha(img)
    if target in NO_ALPHA:
        if alpha:
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.getchannel("A"))
            return bg
        return img.convert("RGB") if (target != "jpg" or img.mode not in ("L", "RGB")) else img
    # Targets that keep alpha
    if alpha:
        return img.convert("RGBA")
    if target == "png" and img.mode in ("L", "P", "1", "RGB"):
        return img
    if target == "tiff" and img.mode in ("L", "1"):
        return img
    return img.convert("RGB")


def _save_kwargs(target: str, opts: Options, img: Image.Image) -> dict[str, Any]:
    q = opts.quality
    kw: dict[str, Any] = {}
    if target == "jpg":
        kw.update(quality=LOSSY_QUALITY[q], optimize=True)
        if q == "best":
            kw["subsampling"] = 0
    elif target in ("webp", "heic"):
        kw.update(quality=LOSSY_QUALITY[q])
    elif target == "png":
        kw.update(compress_level=PNG_COMPRESS[q])
    elif target == "tiff":
        kw.update(compression=TIFF_COMPRESSION[q])
    elif target == "pdf":
        kw.update(resolution=PDF_DPI, quality=LOSSY_QUALITY[q])
    # Keep the colour profile when the mode is unchanged and plain.
    icc = img.info.get("icc_profile")
    if icc and target in ("jpg", "png", "webp", "tiff") and img.mode in ("RGB", "RGBA", "L"):
        kw["icc_profile"] = icc
    return kw


def _is_animated(img: Image.Image) -> bool:
    return bool(getattr(img, "is_animated", False)) and getattr(img, "n_frames", 1) > 1


class _TooBigAnimation(Exception):
    pass


def _save_animation(img: Image.Image, dst: Path, target: str, opts: Options) -> None:
    """gif <-> webp with animation preserved (all frames, durations, loop)."""
    w, h = img.size
    if getattr(img, "n_frames", 1) * w * h > MAX_ANIMATION_PIXELS:
        log.info("animation too large (%s frames of %dx%d); using the first frame only",
                 getattr(img, "n_frames", 1), w, h)
        raise _TooBigAnimation()
    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        durations.append(frame.info.get("duration", 100) or 100)
        frames.append(frame.convert("RGBA"))
    kw: dict[str, Any] = dict(save_all=True, append_images=frames[1:], duration=durations,
                              loop=img.info.get("loop", 0))
    if target == "webp":
        kw["quality"] = LOSSY_QUALITY[opts.quality]
    else:
        kw["disposal"] = 2  # each frame is fully re-drawn (we expanded to full RGBA frames)
    frames[0].save(dst, format=FORMAT_NAME[target], **kw)


def _prepare(src: Path, target: str) -> Image.Image:
    """Open `src`, apply EXIF orientation, normalise mode for `target`."""
    img = _open(src)
    img = ImageOps.exif_transpose(img) or img
    return _normalize(img, target)


def _save(img: Image.Image, dst: Path, target: str, opts: Options) -> None:
    if target == "heic":
        if _have_sips():
            _save_heic_with_sips(img, dst, opts)
            return
        if not _have_pillow_heif():
            raise _heic_error()
    try:
        img.save(dst, format=FORMAT_NAME[target], **_save_kwargs(target, opts, img))
    except ConversionError:
        raise
    except Exception as e:
        log.debug("save failed (%s): %r", target, e)
        raise ConversionError(f"Couldn't save this image as {target.upper()}.")


# --- Registered converter -----------------------------------------------------

@converter("images", sources=_IMAGE_EXTS, targets=_IMAGE_EXTS + ["pdf"], engine="pillow")
def convert_image(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    target = normalize_ext(dst.suffix)
    if target == "heic" and not _heic_available():
        raise _heic_error()
    progress(0.05)
    raw = _open(src)
    progress(0.4)
    try:
        # Animated gif/webp -> gif/webp keeps the animation; everything else uses frame 1.
        if target in ANIMATED and _is_animated(raw):
            try:
                _save_animation(raw, dst, target, opts)
                progress(1.0)
                return
            except Exception as e:
                log.debug("animation save failed, falling back to first frame: %r", e)
                raw.seek(0)
        img = ImageOps.exif_transpose(raw) or raw
        img = _normalize(img, target)
        _save(img, dst, target, opts)
    finally:
        raw.close()
    progress(1.0)


# --- Combine (not registered; used by cli.py) ---------------------------------

def combine_images_to_pdf(srcs: list[Path], dst: Path, opts: Options, progress: Progress) -> None:
    """One PDF, one page per image, in the order given."""
    if not srcs:
        raise ConversionError("There are no images to combine.")
    pages: list[Image.Image] = []
    n = len(srcs)
    for i, s in enumerate(srcs):
        s = Path(s)
        if normalize_ext(s.suffix) not in _IMAGE_EXTS:
            raise ConversionError(f"{s.name} isn't an image, so it can't be combined into a PDF.")
        try:
            pages.append(_prepare(s, "pdf"))
        except ConversionError as e:
            raise ConversionError(f"{s.name}: {e.message}", missing_engine=e.missing_engine)
        progress(0.9 * (i + 1) / n)
    try:
        pages[0].save(dst, format="PDF", save_all=True, append_images=pages[1:],
                      **_save_kwargs("pdf", opts, pages[0]))
    except Exception as e:
        log.debug("combine save failed: %r", e)
        raise ConversionError("Couldn't create the combined PDF.")
    progress(1.0)
