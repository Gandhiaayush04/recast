"""Batch runner: parallel limits, output naming, and progress events.

Converters always write into a private temp dir. Only a finished, non-empty
result is moved into the output folder, so partial files never appear there.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .core import ConversionError, Options, find_converter
from .formats import is_video, normalize_ext, targets_for

MAX_PARALLEL = 10
MAX_PARALLEL_VIDEO = 3
MAX_PARALLEL_LIBREOFFICE = 2

Event = dict
Emit = Callable[[Event], None]

LIBREOFFICE_SEM = threading.Semaphore(MAX_PARALLEL_LIBREOFFICE)


def default_output_dir() -> Path:
    return Path(os.environ.get("RECAST_OUTPUT_DIR", Path.home() / "Downloads"))


# ---- name allocation -------------------------------------------------------

_name_lock = threading.Lock()
_reserved: set[str] = set()


def _numbered(stem: str, ext: str | None, n: int) -> str:
    base = stem if n == 0 else f"{stem} ({n})"
    return f"{base}.{ext}" if ext else base


def reserve_name(out_dir: Path, stem: str, ext: str | None) -> Path:
    """Pick `stem.ext` / `stem (1).ext` ... (or a folder name when ext is None)
    that doesn't exist on disk and isn't reserved by another running job."""
    with _name_lock:
        n = 0
        while True:
            p = out_dir / _numbered(stem, ext, n)
            if str(p) not in _reserved and not p.exists():
                _reserved.add(str(p))
                return p
            n += 1


def release_name(p: Path) -> None:
    with _name_lock:
        _reserved.discard(str(p))


def publish(tmp_result: Path, out_dir: Path, stem: str, ext: str) -> Path:
    """Move a finished file into out_dir under a unique name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final = reserve_name(out_dir, stem, ext)
    try:
        shutil.move(str(tmp_result), str(final))
    finally:
        release_name(final)
    return final


# kept for backwards compatibility with callers that want a reserved path up front
def unique_output_path(out_dir: Path, stem: str, ext: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = reserve_name(out_dir, stem, ext)
    release_name(p)
    return p


# ---- jobs ------------------------------------------------------------------

@dataclass
class Job:
    id: int
    src: Path
    target: str
    outputs: list[Path] = field(default_factory=list)
    status: str = "queued"
    error: str | None = None
    missing_engine: str | None = None


def _fail(job: Job, emit: Emit, message: str, **extra):
    job.status, job.error = "failed", message
    job.missing_engine = extra.get("missing_engine")
    emit({"event": "failed", "id": job.id, "message": message, **extra})


def _run_one(job: Job, opts: Options, out_dir: Path, emit: Emit, group_folder: bool = False):
    src_ext = normalize_ext(job.src.suffix)
    target = normalize_ext(job.target)
    if src_ext == target:
        job.status = "skipped"
        emit({"event": "skipped", "id": job.id, "message": f"Already {target.upper()} — nothing to do."})
        return
    if target not in targets_for(src_ext):
        return _fail(job, emit, f"Can't convert .{src_ext} files to {target.upper()}.")
    conv = find_converter(src_ext, target)
    if conv is None:
        return _fail(job, emit, f"Converting .{src_ext} to {target.upper()} isn't supported yet.")
    if not job.src.is_file():
        return _fail(job, emit, "The original file couldn't be found. It may have been moved or deleted.")

    tmpdir = Path(tempfile.mkdtemp(prefix="recast-"))
    try:
        emit({"event": "started", "id": job.id})
        last = [-1.0]

        def progress(p: float):
            p = max(0.0, min(1.0, float(p)))
            if p - last[0] >= 0.01 or p >= 1.0:
                last[0] = p
                emit({"event": "progress", "id": job.id, "progress": round(p, 3)})

        if conv.multi_output:
            dst = tmpdir / "out"
            dst.mkdir()
        else:
            dst = tmpdir / f"result.{target}"

        conv.fn(job.src, dst, opts, progress)

        job.outputs = _collect(job, conv.multi_output, dst, out_dir, target, group_folder)
        job.status = "done"
        emit({"event": "done", "id": job.id, "outputs": [str(p) for p in job.outputs]})
    except ConversionError as e:
        _fail(job, emit, e.message, missing_engine=e.missing_engine)
    except Exception as e:  # unexpected: friendly message, details for the log
        _fail(job, emit, "Something went wrong converting this file.",
              detail=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=5))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _collect(job: Job, multi: bool, dst: Path, out_dir: Path, target: str,
             group_folder: bool = False) -> list[Path]:
    stem = job.src.stem
    if not multi:
        if not dst.is_file() or dst.stat().st_size == 0:
            raise ConversionError("The conversion finished but produced an empty file.")
        return [publish(dst, out_dir, stem, target)]
    files = sorted(p for p in dst.iterdir()
                   if p.is_file() and p.stat().st_size > 0 and normalize_ext(p.suffix) == target)
    if not files:
        raise ConversionError("The conversion finished but produced no files.")
    if len(files) == 1:
        return [publish(files[0], out_dir, stem, target)]
    if not group_folder:
        # Default: each page/sheet as its own file straight in the output dir.
        return [publish(f, out_dir, f.stem, target) for f in files]
    # Several results (PDF pages, spreadsheet sheets): put them in one folder
    # named after the original, e.g. Downloads/report/report-1.png
    out_dir.mkdir(parents=True, exist_ok=True)
    while True:
        folder = reserve_name(out_dir, stem, None)
        try:
            folder.mkdir()
            break
        except FileExistsError:  # created externally in the gap; try the next name
            release_name(folder)
            continue
    try:
        outs = []
        for f in files:
            final = folder / f.name
            shutil.move(str(f), str(final))
            outs.append(final)
        return outs
    finally:
        release_name(folder)


def run_batch(files: list[Path], target: str, opts: Options, emit: Emit,
              out_dir: Path | None = None, group_folder: bool = False) -> list[Job]:
    out_dir = out_dir or default_output_dir()
    jobs = [Job(i, Path(f), target) for i, f in enumerate(files)]
    for j in jobs:
        emit({"event": "queued", "id": j.id, "source": str(j.src), "target": target})

    # Videos get their own pool (max 3) so they never block lighter files;
    # a global semaphore keeps the total at MAX_PARALLEL.
    total_sem = threading.Semaphore(MAX_PARALLEL)

    def task(j: Job):
        conv = find_converter(normalize_ext(j.src.suffix), normalize_ext(target))
        if conv is not None and conv.engine == "libreoffice":
            with LIBREOFFICE_SEM, total_sem:
                _run_one(j, opts, out_dir, emit, group_folder)
        else:
            with total_sem:
                _run_one(j, opts, out_dir, emit, group_folder)

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_VIDEO) as vpool, \
         ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        for j in jobs:
            (vpool if is_video(j.src.suffix) else pool).submit(task, j)
    emit({"event": "batch_done", "ok": sum(j.status == "done" for j in jobs),
          "skipped": sum(j.status == "skipped" for j in jobs),
          "failed": sum(j.status == "failed" for j in jobs)})
    return jobs
