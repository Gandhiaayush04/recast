"""Internal command line used by the Recast app (never by the user).

Every command prints JSON to stdout. `convert` and `combine` stream one JSON
object per line (JSON Lines) so the app can show live progress.

  python -m recast_engine status
  python -m recast_engine targets FILE...
  python -m recast_engine convert --to EXT [--quality Q] [--resolution R] [--out DIR] FILE...
  python -m recast_engine combine [--out DIR] FILE...
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from . import converters  # noqa: F401  (registers all converters)
from .core import ConversionError, Options
from .engines import INSTALL_HINT, status
from .formats import GROUP_OF, IMAGE, LABELS, common_targets, group_of, normalize_ext
from .jobs import default_output_dir, publish, run_batch

_out_lock = threading.Lock()


def emit(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def cmd_status(_a) -> int:
    emit({"engines": status(), "install_hints": INSTALL_HINT})
    return 0


def cmd_targets(a) -> int:
    exts = [normalize_ext(Path(f).suffix) for f in a.files]
    unknown = [f for f, e in zip(a.files, exts) if e not in GROUP_OF]
    known = [e for e in exts if e in GROUP_OF]
    targets = common_targets(known) if known else []
    groups = sorted({group_of(e) for e in known})
    emit({
        "targets": [{"ext": t, "label": LABELS.get(t, t.upper())} for t in targets],
        "unsupported_files": unknown,
        "groups": groups,
        "can_combine_pdf": len(known) > 1 and all(group_of(e) == IMAGE for e in known),
        "mixed_no_common": len(known) > 1 and not targets,
    })
    return 0


def cmd_convert(a) -> int:
    opts = Options(quality=a.quality, resolution=a.resolution)
    out = Path(a.out) if a.out else default_output_dir()
    jobs = run_batch([Path(f) for f in a.files], a.to, opts, emit, out, group_folder=a.folder)
    return 0 if all(j.status in ("done", "skipped") for j in jobs) else 1


def cmd_combine(a) -> int:
    from .converters.images import combine_images_to_pdf
    files = [Path(f) for f in a.files]
    out = Path(a.out) if a.out else default_output_dir()
    stem = files[0].stem + " (combined)" if files else "combined"
    tmpdir = Path(tempfile.mkdtemp(prefix="recast-"))
    tmp = tmpdir / "combined.pdf"
    emit({"event": "started", "id": 0})
    try:
        combine_images_to_pdf(files, tmp, Options(quality=a.quality),
                              lambda p: emit({"event": "progress", "id": 0, "progress": round(p, 3)}))
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise ConversionError("The combined PDF came out empty.")
        final = publish(tmp, out, stem, "pdf")
        emit({"event": "done", "id": 0, "outputs": [str(final)]})
        emit({"event": "batch_done", "ok": 1, "failed": 0})
        return 0
    except ConversionError as e:
        emit({"event": "failed", "id": 0, "message": e.message, "missing_engine": e.missing_engine})
    except Exception as e:
        emit({"event": "failed", "id": 0, "message": "Something went wrong combining these images.",
              "detail": f"{type(e).__name__}: {e}"})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    emit({"event": "batch_done", "ok": 0, "failed": 1})
    return 1


def sweep_stale_sessions(tmp_root: str | None = None) -> list[str]:
    """Delete leftover recast-session-<pid>-* folders from engines that are no
    longer running (crash, force quit). Only folders owned by this user are touched."""
    import os
    import re
    root = Path(tmp_root or tempfile.gettempdir())
    removed: list[str] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return removed
    for d in entries:
        m = re.fullmatch(r"recast-session-(\d+)-[A-Za-z0-9_]+", d.name)
        if not m:
            continue
        try:
            if d.is_symlink() or not d.is_dir() or d.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        pid = int(m.group(1))
        try:
            os.kill(pid, 0)
            continue                      # still running
        except ProcessLookupError:
            pass
        except OSError:                   # e.g. PermissionError: alive, not ours
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(str(d))
    return removed


def _install_shutdown_handler() -> None:
    """Make the engine clean up after itself when the app stops it.

    * All temp files (converter scratch dirs, LibreOffice profiles) go into one
      session folder that is deleted on exit, on SIGTERM, or if the app dies.
    * We become a process-group leader so SIGTERM can take ffmpeg/soffice
      children down with us.
    * A watchdog thread notices if the parent app disappears (force quit/crash)
      and stops the engine so it doesn't keep writing into Downloads."""
    import atexit
    import os
    import signal
    import time

    sweep_stale_sessions()
    session = tempfile.mkdtemp(prefix=f"recast-session-{os.getpid()}-")
    tempfile.tempdir = session          # every later mkdtemp() lands inside it
    atexit.register(shutil.rmtree, session, True)

    try:
        os.setpgrp()
        own_group = True
    except OSError:
        own_group = False

    def _stop(signum=None, _frame=None):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        from .core import kill_all_children
        kill_all_children()
        shutil.rmtree(session, ignore_errors=True)
        if own_group:
            try:
                os.killpg(os.getpgrp(), signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(session, ignore_errors=True)
        os._exit(143)

    signal.signal(signal.SIGTERM, _stop)

    parent = os.getppid()

    def _watch():
        while True:
            time.sleep(1)
            if os.getppid() != parent:
                # signal handlers must run on the main thread: signal ourselves
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=_watch, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    if argv is None:  # real CLI run (not tests)
        _install_shutdown_handler()
    p = argparse.ArgumentParser(prog="recast_engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    t = sub.add_parser("targets"); t.add_argument("files", nargs="+"); t.set_defaults(fn=cmd_targets)
    c = sub.add_parser("convert")
    c.add_argument("--to", required=True)
    c.add_argument("--quality", default="balanced", choices=["smaller", "balanced", "best"])
    c.add_argument("--resolution", default="original", choices=["original", "1080p", "720p"])
    c.add_argument("--out")
    c.add_argument("--folder", action="store_true",
                   help="put multi-file results (PDF pages, sheets) in one folder")
    c.add_argument("files", nargs="+")
    c.set_defaults(fn=cmd_convert)
    m = sub.add_parser("combine")
    m.add_argument("--quality", default="balanced", choices=["smaller", "balanced", "best"])
    m.add_argument("--out")
    m.add_argument("files", nargs="+")
    m.set_defaults(fn=cmd_combine)
    a = p.parse_args(argv)
    return a.fn(a)
