"""PDF -> PNG / JPG (one image per page), rendered with pypdfium2 (engine "pdfium")."""
from __future__ import annotations

import logging
from pathlib import Path

from ..core import ConversionError, Options, Progress, converter

log = logging.getLogger(__name__)

MAX_PAGES = 500

# Render scale (1.0 = 72 dpi): smaller 108 dpi, balanced 144 dpi, best 216 dpi.
SCALE = {"smaller": 1.5, "balanced": 2.0, "best": 3.0}
# JPEG quality per level (PNG is lossless, so quality only affects scale).
JPG_QUALITY = {"smaller": 70, "balanced": 85, "best": 95}

MAX_PIXELS = 64_000_000
MIN_SCALE = 0.25
TOO_BIG = "This PDF page is too large to convert."

DAMAGED = "This file looks damaged or isn't really a PDF file."


def _fit_scale(page, scale: float) -> float:
    """Reduce `scale` so the rendered page stays within MAX_PIXELS."""
    try:
        w, h = page.get_size()
    except Exception:
        raise ConversionError(DAMAGED)
    if not (w > 0 and h > 0):
        return scale
    if w * h * scale * scale <= MAX_PIXELS:
        return scale
    fitted = (MAX_PIXELS / (w * h)) ** 0.5 * 0.999
    if fitted < MIN_SCALE:
        raise ConversionError(TOO_BIG)
    return fitted


def _render(src: Path, dst: Path, opts: Options, progress: Progress, target: str) -> None:
    try:
        import pypdfium2 as pdfium
    except Exception:
        raise ConversionError("PDF support isn't installed.", missing_engine=None)

    try:
        pdf = pdfium.PdfDocument(str(src))
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg:
            raise ConversionError("This PDF is password-protected.")
        log.debug("pdfium open failed: %s", e)
        raise ConversionError(DAMAGED)
    except Exception as e:
        log.debug("pdf open failed: %s", e)
        raise ConversionError(DAMAGED)

    try:
        n = len(pdf)
        if n == 0:
            raise ConversionError("This PDF has no pages.")
        if n > MAX_PAGES:
            raise ConversionError("This PDF has too many pages (over 500).")

        width = max(2, len(str(n))) if n > 9 else 1
        scale = SCALE[opts.quality]
        for i in range(n):
            page = pdf[i]
            try:
                page_scale = _fit_scale(page, scale)
                img = page.render(scale=page_scale).to_pil().convert("RGB")
            except ConversionError:
                raise
            except Exception as e:
                log.debug("render failed on page %d: %s", i + 1, e)
                raise ConversionError(f"Couldn't read page {i + 1}. The PDF may be damaged.")
            finally:
                page.close()
            name = f"{src.stem}-{str(i + 1).zfill(width)}.{target}"
            out = dst / name
            if target == "jpg":
                img.save(out, "JPEG", quality=JPG_QUALITY[opts.quality], optimize=True)
            else:
                img.save(out, "PNG")
            img.close()
            progress((i + 1) / n)
    finally:
        pdf.close()



# multi_output converters don't receive the target extension, so register one
# thin wrapper per target (find_converter matches on source+target).
@converter("pdf-to-png", ["pdf"], ["png"], engine="pdfium", multi_output=True)
def pdf_to_png(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _render(src, dst, opts, progress, "png")


@converter("pdf-to-jpg", ["pdf"], ["jpg"], engine="pdfium", multi_output=True)
def pdf_to_jpg(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _render(src, dst, opts, progress, "jpg")
