"""Unit tests for the conversion matrix in recast_engine.formats (no engines needed)."""
from __future__ import annotations

import pytest

from recast_engine.formats import (
    GROUPS, common_targets, group_of, normalize_ext, targets_for,
)


@pytest.mark.parametrize("raw, expected", [
    ("JPG", "jpg"),
    (".MOV", "mov"),
    ("jpeg", "jpg"),
    ("JPE", "jpg"),
    ("tif", "tiff"),
    ("heif", "heic"),
    ("m4v", "mp4"),
    ("qt", "mov"),
    ("opus", "ogg"),
    ("aiff", "wav"),
    ("md", "txt"),
    ("unknown", "unknown"),
])
def test_normalize_ext(raw, expected):
    assert normalize_ext(raw) == expected


def test_group_of_uses_normalised_ext():
    assert group_of(".JPEG") == "image"
    assert group_of("m4v") == "video"
    assert group_of("exe") is None


def test_every_group_format_has_targets():
    for exts in GROUPS.values():
        for e in exts:
            assert targets_for(e), f"{e} has no targets"


@pytest.mark.parametrize("ext", ["exe", "zip", ""])
def test_unknown_format_has_no_targets(ext):
    assert targets_for(ext) == []


# ---- targets_for spot checks against PROJECT.md ----------------------------

def test_image_targets_include_all_images_and_pdf():
    t = targets_for("jpg")
    for e in ["png", "heic", "webp", "tiff", "bmp", "gif", "pdf"]:
        assert e in t
    assert "jpg" not in t


def test_heic_targets_include_jpg_and_pdf():
    t = targets_for("heic")
    assert "jpg" in t and "pdf" in t and "heic" not in t


def test_video_targets_include_video_gif_and_audio():
    t = targets_for("mp4")
    for e in ["mov", "mkv", "webm", "avi", "gif", "mp3", "wav", "m4a", "aac", "flac", "ogg"]:
        assert e in t
    assert "mp4" not in t


def test_audio_targets_are_audio_only():
    assert targets_for("mp3") == ["wav", "m4a", "aac", "flac", "ogg"]
    for e in ["mp4", "gif", "pdf", "jpg"]:
        assert e not in targets_for("wav")


def test_text_documents_convert_among_themselves_and_to_pdf():
    t = targets_for("docx")
    assert set(t) == {"odt", "rtf", "txt", "pdf"}
    assert "pptx" not in t and "xlsx" not in t


def test_slides_convert_among_themselves_and_to_pdf():
    assert set(targets_for("pptx")) == {"odp", "pdf"}
    assert set(targets_for("odp")) == {"pptx", "pdf"}


def test_sheets_convert_among_themselves_and_to_pdf():
    assert set(targets_for("xlsx")) == {"ods", "csv", "pdf"}
    assert set(targets_for("ods")) == {"xlsx", "csv", "pdf"}
    assert set(targets_for("csv")) == {"xlsx", "ods", "pdf"}


def test_pdf_targets_are_png_and_jpg_only():
    assert targets_for("pdf") == ["png", "jpg"]


def test_alias_gives_same_targets_as_canonical():
    assert targets_for("jpeg") == targets_for("jpg")
    assert targets_for("MOV") == targets_for("mov")


# ---- common_targets --------------------------------------------------------

def test_heic_and_jpg_batch_offers_jpg():
    assert "jpg" in common_targets(["heic", "jpg"])


def test_video_and_image_batch_offers_only_gif():
    assert common_targets(["mp4", "jpg"]) == ["gif"]


def test_audio_and_document_batch_offers_nothing():
    assert common_targets(["mp3", "docx"]) == []


def test_pdf_and_document_batch_offers_only_pdf():
    # a PDF already is a PDF, so PDF is the one target both files can reach
    assert common_targets(["pdf", "docx"]) == ["pdf"]


def test_mixed_documents_offer_pdf():
    t = common_targets(["docx", "pptx"])
    assert "pdf" in t and "odt" not in t


def test_all_same_format_excludes_that_format():
    t = common_targets(["jpg", "jpg"])
    assert "jpg" not in t and "png" in t


def test_single_file_matches_targets_for():
    assert common_targets(["mp3"]) == targets_for("mp3")


def test_common_targets_takes_extensions_not_filenames():
    # cli.cmd_targets passes bare extensions; a filename is not a known format
    assert common_targets(["a.jpg"]) == []


def test_empty_batch():
    assert common_targets([]) == []


def test_common_targets_order_follows_first_file():
    t = common_targets(["png", "webp"])
    # first file (png) decides the order: its own targets in GROUPS order, jpg comes first
    assert t[0] == "jpg"
    assert "pdf" in t and "webp" in t  # webp -> png is valid, and png -> webp too
