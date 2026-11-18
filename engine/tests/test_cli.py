"""Subprocess tests for `python3 -m recast_engine`: every command prints valid JSON lines."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "recast_engine", *args], cwd=ENGINE_ROOT,
                          capture_output=True, text=True, timeout=180)


def _json_lines(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]  # raises if any line is not JSON


def test_status_prints_one_json_object():
    proc = _cli("status")
    assert proc.returncode == 0, proc.stderr
    (obj,) = _json_lines(proc.stdout)
    assert set(obj["engines"]) >= {"ffmpeg", "libreoffice", "heic", "pdf"}
    assert all(isinstance(v, bool) for v in obj["engines"].values())
    assert "ffmpeg" in obj["install_hints"]


def test_targets_for_two_images_offers_jpg_and_can_combine(tmp_path):
    proc = _cli("targets", "photo.heic", "photo.jpg")
    assert proc.returncode == 0, proc.stderr
    (obj,) = _json_lines(proc.stdout)
    exts = [t["ext"] for t in obj["targets"]]
    assert "jpg" in exts and "pdf" in exts
    assert obj["can_combine_pdf"] is True
    assert obj["mixed_no_common"] is False
    assert obj["unsupported_files"] == []


def test_targets_for_video_and_image_offers_only_gif():
    (obj,) = _json_lines(_cli("targets", "clip.mp4", "photo.png").stdout)
    assert [t["ext"] for t in obj["targets"]] == ["gif"]


def test_targets_reports_mixed_without_common_target():
    (obj,) = _json_lines(_cli("targets", "song.mp3", "notes.docx").stdout)
    assert obj["targets"] == []
    assert obj["mixed_no_common"] is True


def test_targets_lists_unknown_files():
    (obj,) = _json_lines(_cli("targets", "archive.zip", "photo.png").stdout)
    assert obj["unsupported_files"] == ["archive.zip"]


def test_convert_streams_json_lines_and_ends_with_batch_done(tmp_path):
    src = tmp_path / "in" / "tiny.png"
    src.parent.mkdir()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(src)
    out = tmp_path / "out"

    proc = _cli("convert", "--to", "jpg", "--out", str(out), str(src))
    events = _json_lines(proc.stdout)  # every line must be valid JSON
    assert events, "no output"
    assert "Traceback" not in proc.stderr
    assert events[0]["event"] == "queued"
    assert events[-1] == {"event": "batch_done", "ok": events[-1]["ok"],
                          "skipped": events[-1]["skipped"], "failed": events[-1]["failed"]}
    assert proc.returncode == (0 if events[-1]["failed"] == 0 else 1)


def test_convert_unknown_target_reports_failed_event_and_exit_1(tmp_path):
    src = tmp_path / "tiny.png"
    Image.new("RGB", (8, 8)).save(src)
    proc = _cli("convert", "--to", "exe", "--out", str(tmp_path / "out"), str(src))
    events = _json_lines(proc.stdout)
    failed = [e for e in events if e["event"] == "failed"]
    assert len(failed) == 1 and failed[0]["message"] == "Can't convert .png files to EXE."
    assert proc.returncode == 1


def test_convert_rejects_bad_quality_argument(tmp_path):
    proc = _cli("convert", "--to", "jpg", "--quality", "ultra", str(tmp_path / "x.png"))
    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr
