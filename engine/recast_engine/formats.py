"""Format registry: which extensions exist, which group they belong to,
and which target formats each source format can become.

This file is the single source of truth for the conversion matrix.
Converters register the functions that actually do the work (see registry.py).
"""
from __future__ import annotations

IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"
DOC_TEXT = "doc_text"      # word-processing documents
DOC_SLIDES = "doc_slides"  # presentations
DOC_SHEET = "doc_sheet"    # spreadsheets
PDF = "pdf"

GROUPS: dict[str, list[str]] = {
    IMAGE: ["jpg", "png", "heic", "webp", "tiff", "bmp", "gif"],
    VIDEO: ["mp4", "mov", "mkv", "webm", "avi"],
    AUDIO: ["mp3", "wav", "m4a", "aac", "flac", "ogg"],
    DOC_TEXT: ["docx", "odt", "rtf", "txt"],
    DOC_SLIDES: ["pptx", "odp"],
    DOC_SHEET: ["xlsx", "ods", "csv"],
    PDF: ["pdf"],
}

# Alternate spellings normalised to the canonical extension.
ALIASES: dict[str, str] = {
    "jpeg": "jpg", "jpe": "jpg", "tif": "tiff", "heif": "heic",
    "m4v": "mp4", "qt": "mov", "oga": "ogg", "opus": "ogg", "aif": "wav",
    "aiff": "wav", "text": "txt", "md": "txt",
}

GROUP_OF: dict[str, str] = {ext: g for g, exts in GROUPS.items() for ext in exts}

# Human-friendly labels for the UI buttons.
LABELS: dict[str, str] = {e: e.upper() for e in GROUP_OF}
LABELS.update({"jpg": "JPG", "heic": "HEIC", "pdf": "PDF"})


def normalize_ext(ext: str) -> str:
    e = ext.lower().lstrip(".")
    return ALIASES.get(e, e)


def group_of(ext: str) -> str | None:
    return GROUP_OF.get(normalize_ext(ext))


def targets_for(ext: str) -> list[str]:
    """All formats a file with extension `ext` can be converted into (excluding itself)."""
    src = normalize_ext(ext)
    g = GROUP_OF.get(src)
    if g is None:
        return []
    out: list[str] = []
    if g == IMAGE:
        out = GROUPS[IMAGE] + ["pdf"]
    elif g == VIDEO:
        out = GROUPS[VIDEO] + ["gif"] + GROUPS[AUDIO]
    elif g == AUDIO:
        out = GROUPS[AUDIO]
    elif g in (DOC_TEXT, DOC_SLIDES, DOC_SHEET):
        out = GROUPS[g] + ["pdf"]
    elif g == PDF:
        out = ["png", "jpg"]
    # de-duplicate, keep order, drop the source format itself
    seen: set[str] = set()
    result = []
    for t in out:
        if t != src and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def common_targets(exts: list[str]) -> list[str]:
    """Targets valid for every file in a batch (order follows the first file)."""
    if not exts:
        return []
    norm = [normalize_ext(e) for e in exts]
    # candidate order: first file's targets, then its own format
    candidates = targets_for(norm[0]) + [norm[0]]
    result = []
    for t in candidates:
        # every file must either convert to t or already be t,
        # and at least one file must actually need converting
        if all(t == e or t in targets_for(e) for e in norm) and any(t != e for e in norm):
            result.append(t)
    return result


def is_video(ext: str) -> bool:
    return group_of(ext) == VIDEO


def export_matrix() -> dict:
    """The whole conversion matrix as plain data. The app ships this as formats.json
    (app/Recast/Resources) so its menus can never disagree with the engine."""
    return {
        "groups": GROUPS,
        "aliases": ALIASES,
        "labels": LABELS,
        "targets": {e: targets_for(e) for e in sorted(GROUP_OF)},
    }
