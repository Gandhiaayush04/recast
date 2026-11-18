"""Shared types every converter uses: options, errors, progress, and the
converter registry. Converter modules import from here and nothing else
from the engine internals.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .formats import normalize_ext

QUALITIES = ("smaller", "balanced", "best")
RESOLUTIONS = ("original", "1080p", "720p")


@dataclass(frozen=True)
class Options:
    quality: str = "balanced"        # one of QUALITIES
    resolution: str = "original"     # one of RESOLUTIONS (video only)

    def __post_init__(self):
        if self.quality not in QUALITIES:
            raise ValueError(f"quality must be one of {QUALITIES}")
        if self.resolution not in RESOLUTIONS:
            raise ValueError(f"resolution must be one of {RESOLUTIONS}")


class ConversionError(Exception):
    """Raised by converters. `message` is shown to the user verbatim,
    so it must be plain English with no stack traces or jargon.
    `missing_engine` is set (e.g. "ffmpeg", "libreoffice") when the failure
    is because a required engine is not installed, so the app can offer Install."""

    def __init__(self, message: str, missing_engine: str | None = None):
        super().__init__(message)
        self.message = message
        self.missing_engine = missing_engine


Progress = Callable[[float], None]  # called with 0.0..1.0


class ConvertFn(Protocol):
    def __call__(self, src: Path, dst: Path, opts: Options, progress: Progress) -> None: ...


@dataclass
class Converter:
    name: str
    sources: frozenset[str]
    targets: frozenset[str]
    fn: ConvertFn
    engine: str  # "pillow" | "ffmpeg" | "libreoffice" | "pdfium"
    # If True, `dst` passed to fn is an EMPTY DIRECTORY and fn may write one or
    # more files into it (e.g. one PNG per PDF page, one CSV per sheet).
    # Files must be named "<anything>.<target ext>"; they are ordered by name.
    multi_output: bool = False


_REGISTRY: list[Converter] = []


def converter(name: str, sources: set[str] | list[str], targets: set[str] | list[str],
              engine: str, multi_output: bool = False):
    """Decorator used by converter modules to register a conversion function.

    Contract for fn(src, dst, opts, progress):
      * write the result to `dst` (a path inside a private temp dir, already
        carrying the correct extension), or into `dst` as a directory if
        multi_output=True. Never write anywhere else; never touch `src`.
      * call progress(x) with 0..1 when meaningful (optional).
      * on any expected failure raise ConversionError with a plain-English message.
    """
    def wrap(fn: ConvertFn) -> ConvertFn:
        _REGISTRY.append(Converter(name, frozenset(sources), frozenset(targets), fn, engine, multi_output))
        return fn
    return wrap


def find_converter(src_ext: str, dst_ext: str) -> Converter | None:
    s, d = normalize_ext(src_ext), normalize_ext(dst_ext)
    for c in _REGISTRY:
        if s in c.sources and d in c.targets:
            return c
    return None


def registry() -> list[Converter]:
    return list(_REGISTRY)


# ---- child process tracking --------------------------------------------------
# Converters register every external process they start, so the engine can kill
# them all immediately if the app stops it (Stop button, quit, crash).

_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def track_child(proc: subprocess.Popen) -> None:
    with _children_lock:
        _children.add(proc)


def untrack_child(proc: subprocess.Popen) -> None:
    with _children_lock:
        _children.discard(proc)


def kill_all_children() -> None:
    with _children_lock:
        procs = list(_children)
    for p in procs:
        try:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)   # children started in their own session
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                p.kill()
        except Exception:
            pass
