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


def test_convert_out_creates_missing_nested_folder(tmp_path, monkeypatch):
    """--out into a folder that doesn't exist yet: the engine creates it."""
    from PIL import Image
    from recast_engine import cli
    src = tmp_path / "a.png"
    Image.new("RGB", (8, 8), "red").save(src)
    out = tmp_path / "new" / "nested"
    assert cli.main(["convert", "--to", "jpg", "--out", str(out), str(src)]) == 0
    assert (out / "a.jpg").is_file()


def test_files_from_reads_nul_separated_paths(tmp_path):
    from PIL import Image
    from recast_engine import cli
    names = ["plain.png", "with space.png", "new\nline.png"]
    for n in names:
        Image.new("RGB", (4, 4), "blue").save(tmp_path / n)
    listing = tmp_path / "list.bin"
    listing.write_bytes("\0".join(str(tmp_path / n) for n in names).encode())
    out = tmp_path / "out"
    assert cli.main(["convert", "--to", "jpg", "--out", str(out), "--files-from", str(listing)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["new\nline.jpg", "plain.jpg", "with space.jpg"]


def test_files_from_handles_thousands_of_paths(tmp_path):
    from recast_engine import cli
    paths = [f"/nowhere/file-{i:05d}-{'x' * 60}.png" for i in range(5000)]
    listing = tmp_path / "list.bin"
    listing.write_bytes("\0".join(paths).encode())

    class P:  # a stand-in parser: read_files only calls .error()
        def error(self, msg): raise AssertionError(msg)

    class A: files = []; files_from = str(listing)
    assert cli.read_files(A, P()) == paths


def test_convert_without_any_files_is_an_error():
    import pytest
    from recast_engine import cli
    with pytest.raises(SystemExit):
        cli.main(["convert", "--to", "jpg"])


def test_unreadable_file_list_is_a_clean_error(tmp_path):
    import pytest
    from recast_engine import cli
    with pytest.raises(SystemExit):
        cli.main(["convert", "--to", "jpg", "--files-from", str(tmp_path / "missing.bin")])
