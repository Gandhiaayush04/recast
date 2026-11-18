"""Video / audio conversions via ffmpeg.

Registered converters:
  * video -> video (mp4, mov, mkv, webm, avi)
  * video -> gif
  * video -> audio (any audio format)
  * audio -> audio
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from .. import engines
from ..core import ConversionError, Options, Progress, converter, track_child
from ..formats import GROUPS

log = logging.getLogger(__name__)

VIDEO_EXTS = GROUPS["video"]
AUDIO_EXTS = GROUPS["audio"]

# ---- quality mapping (all tunables live here) ------------------------------
QUALITY = {
    # H.264 CRF for mp4/mov/mkv
    "x264_crf": {"smaller": 28, "balanced": 23, "best": 18},
    # VP9 CRF for webm (used with -b:v 0)
    "vp9_crf": {"smaller": 38, "balanced": 32, "best": 28},
    # mpeg4 -q:v for avi (lower = better)
    "mpeg4_q": {"smaller": 8, "balanced": 4, "best": 2},
    # AAC bitrate for video containers and m4a/aac
    "aac_bitrate": {"smaller": "96k", "balanced": "192k", "best": "256k"},
    # Opus bitrate for webm audio
    "opus_bitrate": {"smaller": "80k", "balanced": "128k", "best": "192k"},
    # mp3 VBR quality (libmp3lame -q:a) for mp3 files and avi audio
    "mp3_q": {"smaller": 7, "balanced": 4, "best": 0},
    # vorbis -q:a for ogg
    "vorbis_q": {"smaller": 3, "balanced": 5, "best": 8},
    # gif: (fps, max width)
    "gif": {"smaller": (10, 480), "balanced": (12, 640), "best": (15, 800)},
}
RESOLUTION_HEIGHT = {"1080p": 1080, "720p": 720}
GIF_MAX_SECONDS = 60

# Codecs that can be stream-copied into each video container (8-bit 4:2:0 only,
# see _can_remux) for the fast path.
REMUX_OK = {
    "mp4": ({"h264"}, {"aac", "mp3"}),
    "mov": ({"h264"}, {"aac", "mp3"}),
    "mkv": ({"h264", "hevc", "vp9", "av1", "vp8"}, {"aac", "mp3", "opus", "vorbis", "flac", "ac3"}),
    "webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
    "avi": ({"mpeg4"}, {"mp3"}),
}
SAFE_PIX_FMTS = {"yuv420p", "yuvj420p"}


# ---- process helpers -------------------------------------------------------

def _env() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C",
    }


def _ffmpeg() -> str:
    p = engines.find("ffmpeg")
    if not p:
        raise ConversionError("FFmpeg isn't installed yet.", missing_engine="ffmpeg")
    return p


def _ffprobe() -> str:
    p = engines.find("ffprobe")
    if not p:
        raise ConversionError("FFmpeg isn't installed yet.", missing_engine="ffmpeg")
    return p


@lru_cache(maxsize=None)
def _encoders(ffmpeg: str) -> frozenset[str]:
    """Names of encoders available in this ffmpeg build."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30, env=_env()).stdout
    except Exception:
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


# Only these container demuxers may be used to read an input file, and only
# local files/pipes may be opened. This stops a renamed playlist/manifest
# (HLS, DASH, concat, SDP...) from making ffmpeg fetch other files or URLs.
INPUT_FORMATS = ("mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mp3,wav,flac,ogg,aac,mpegts,asf,flv,"
                 "mpeg,ac3,eac3,aiff,caf,w64,amr,ape,wv,tta,au")
SAFE_INPUT_ARGS = ["-protocol_whitelist", "file,pipe", "-format_whitelist", INPUT_FORMATS]


class _Probe:
    def __init__(self, duration: float | None, v: dict | None, a: dict | None):
        self.duration = duration
        self.video = v
        self.audio = a


def _probe(src: Path) -> _Probe:
    try:
        r = subprocess.run(
            [_ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             *SAFE_INPUT_ARGS, "-i", str(src)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, env=_env())
        data = json.loads(r.stdout or "{}")
    except subprocess.TimeoutExpired:
        raise ConversionError("This file took too long to read. It may be damaged.")
    except (ValueError, OSError):
        data = {}
    streams = data.get("streams") or []
    if not streams:
        raise ConversionError(f"This file looks damaged or isn't really a .{src.suffix.lstrip('.').lower()} file.")
    # Skip attached cover art ("video" streams flagged attached_pic).
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = None
    for cand in ((data.get("format") or {}).get("duration"), (v or a or {}).get("duration")):
        try:
            if cand is not None and float(cand) > 0:
                dur = float(cand)
                break
        except (TypeError, ValueError):
            pass
    return _Probe(dur, v, a)


def _with_safe_input(args: list[str]) -> list[str]:
    """Put the input whitelists directly before every -i."""
    out: list[str] = []
    for a in args:
        if a == "-i":
            out += SAFE_INPUT_ARGS
        out.append(a)
    return out


def _run_ffmpeg(args: list[str], duration: float | None, progress: Progress, src: Path) -> None:
    """Run ffmpeg, reporting progress. Raises ConversionError on failure."""
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats"] + _with_safe_input(args)
    log.debug("ffmpeg: %s", cmd)
    timeout = max(600.0, 4 * (duration or 0))
    with tempfile.TemporaryFile(mode="w+") as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf,
                                text=True, env=_env())
        track_child(proc)
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and duration:
                    try:
                        # ffmpeg's out_time_ms is (confusingly) microseconds too.
                        progress(min(0.99, int(val) / 1_000_000 / duration))
                    except ValueError:
                        pass
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        errf.seek(0)
        err = errf.read()
    if timed_out.is_set():
        raise ConversionError("This conversion took too long and was stopped.")
    if proc.returncode != 0:
        log.debug("ffmpeg failed (%s): %s", proc.returncode, err[-2000:])
        raise _friendly_error(err, src)


def _friendly_error(err: str, src: Path) -> ConversionError:
    low = err.lower()
    ext = src.suffix.lstrip(".").lower()
    if any(s in low for s in ("invalid data found", "moov atom not found", "could not find codec parameters",
                              "end of file", "error while decoding")):
        return ConversionError(f"This file looks damaged or isn't really a .{ext} file.")
    if "no space left" in low:
        return ConversionError("There isn't enough free disk space to finish this conversion.")
    if "permission denied" in low:
        return ConversionError("Recast doesn't have permission to read this file.")
    return ConversionError("Couldn't convert this file. It may be damaged or use an unsupported format.")


# ---- encoder selection -----------------------------------------------------

def _pick(ffmpeg: str, *names: str) -> str | None:
    have = _encoders(ffmpeg)
    for n in names:
        if n in have:
            return n
    return None


def _scale_filter(opts: Options, even_only: bool) -> str | None:
    """Scale filter for the resolution option (never upscales; keeps even dimensions)."""
    h = RESOLUTION_HEIGHT.get(opts.resolution)
    if h:
        return f"scale=-2:'trunc(min({h},ih)/2)*2'"
    if even_only:
        # yuv420p needs even dimensions; odd-sized sources would otherwise fail.
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    return None


def _video_audio_args(target: str, ffmpeg: str, opts: Options, has_audio: bool) -> tuple[list[str], list[str]]:
    """(video codec args, audio codec args) for re-encoding to `target`."""
    q = opts.quality
    vargs: list[str]
    aargs: list[str]
    if target in ("mp4", "mov", "mkv"):
        enc = _pick(ffmpeg, "libx264")
        if enc:
            vargs = ["-c:v", "libx264", "-preset", "medium", "-crf", str(QUALITY["x264_crf"][q])]
        else:
            enc = _pick(ffmpeg, "h264_videotoolbox", "mpeg4")
            if not enc:
                raise ConversionError("This copy of FFmpeg can't create that video format.")
            log.warning("libx264 missing; falling back to %s", enc)
            if enc == "h264_videotoolbox":
                vargs = ["-c:v", enc, "-q:v", {"smaller": "45", "balanced": "65", "best": "80"}[q]]
            else:
                vargs = ["-c:v", "mpeg4", "-q:v", str(QUALITY["mpeg4_q"][q])]
        vargs += ["-pix_fmt", "yuv420p"]
        if target in ("mp4", "mov") and enc != "mpeg4":
            vargs += ["-tag:v", "avc1"]
        aenc = _pick(ffmpeg, "aac", "libopus")
        aargs = ["-c:a", aenc or "aac", "-b:a", QUALITY["aac_bitrate"][q]]
        if aenc == "libopus":
            log.warning("native aac missing; using libopus")
    elif target == "webm":
        if not _pick(ffmpeg, "libvpx-vp9"):
            raise ConversionError("This copy of FFmpeg can't create WebM videos.")
        vargs = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(QUALITY["vp9_crf"][q]),
                 "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p"]
        aenc = _pick(ffmpeg, "libopus", "libvorbis")
        if not aenc:
            raise ConversionError("This copy of FFmpeg can't create WebM audio.")
        aargs = ["-c:a", aenc, "-b:a", QUALITY["opus_bitrate"][q]] if aenc == "libopus" \
            else ["-c:a", aenc, "-q:a", str(QUALITY["vorbis_q"][q])]
        if aenc != "libopus":
            log.warning("libopus missing; using libvorbis")
    elif target == "avi":
        vargs = ["-c:v", "mpeg4", "-q:v", str(QUALITY["mpeg4_q"][q]), "-pix_fmt", "yuv420p", "-vtag", "XVID"]
        aenc = _pick(ffmpeg, "libmp3lame")
        if aenc:
            aargs = ["-c:a", "libmp3lame", "-q:a", str(QUALITY["mp3_q"][q])]
        else:
            log.warning("libmp3lame missing; AVI audio falls back to ac3")
            aargs = ["-c:a", "ac3", "-b:a", QUALITY["aac_bitrate"][q]]
    else:  # pragma: no cover - registry prevents this
        raise ConversionError(f"Can't convert to {target.upper()}.")
    return vargs, (aargs if has_audio else [])


def _audio_args(target: str, ffmpeg: str, opts: Options) -> list[str]:
    q = opts.quality
    if target == "mp3":
        if not _pick(ffmpeg, "libmp3lame"):
            raise ConversionError("This copy of FFmpeg can't create MP3 files.")
        return ["-c:a", "libmp3lame", "-q:a", str(QUALITY["mp3_q"][q]), "-f", "mp3"]
    if target == "m4a":
        return ["-c:a", "aac", "-b:a", QUALITY["aac_bitrate"][q], "-f", "ipod"]
    if target == "aac":
        return ["-c:a", "aac", "-b:a", QUALITY["aac_bitrate"][q], "-f", "adts"]
    if target == "ogg":
        if _pick(ffmpeg, "libvorbis"):
            return ["-c:a", "libvorbis", "-q:a", str(QUALITY["vorbis_q"][q]), "-f", "ogg"]
        if _pick(ffmpeg, "libopus"):
            log.warning("libvorbis missing; using opus inside ogg")
            return ["-c:a", "libopus", "-b:a", QUALITY["opus_bitrate"][q], "-f", "ogg"]
        raise ConversionError("This copy of FFmpeg can't create OGG files.")
    if target == "flac":
        return ["-c:a", "flac", "-compression_level", "8", "-f", "flac"]
    if target == "wav":
        return ["-c:a", "pcm_s16le", "-f", "wav"]
    raise ConversionError(f"Can't convert to {target.upper()}.")  # pragma: no cover


# ---- conversions -----------------------------------------------------------

def _can_remux(target: str, p: _Probe, opts: Options) -> bool:
    if opts.resolution != "original" or not p.video:
        return False
    vcodecs, acodecs = REMUX_OK[target]
    if p.video.get("codec_name") not in vcodecs or p.video.get("pix_fmt") not in SAFE_PIX_FMTS:
        return False
    if p.audio and p.audio.get("codec_name") not in acodecs:
        return False
    return True


@converter("media_video_to_video", VIDEO_EXTS, VIDEO_EXTS, "ffmpeg")
def convert_video(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    ffmpeg = _ffmpeg()
    target = dst.suffix.lstrip(".").lower()
    p = _probe(src)
    if not p.video:
        raise ConversionError("This file doesn't contain any video.")
    base = ["-i", str(src), "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "0"]
    tail = ["-movflags", "+faststart"] if target in ("mp4", "mov") else []
    fmt = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "webm": "webm", "avi": "avi"}[target]

    if _can_remux(target, p, opts):
        try:
            _run_ffmpeg(base + ["-c", "copy"] + tail + ["-f", fmt, str(dst)], p.duration, progress, src)
            progress(1.0)
            return
        except ConversionError:
            log.debug("remux failed, re-encoding")
            progress(0.0)
            dst.unlink(missing_ok=True)

    vargs, aargs = _video_audio_args(target, ffmpeg, opts, p.audio is not None)
    flt = _scale_filter(opts, even_only=True)
    vf = ["-vf", flt] if flt else []
    _run_ffmpeg(base + vf + vargs + aargs + tail + ["-f", fmt, str(dst)], p.duration, progress, src)
    progress(1.0)


@converter("media_video_to_gif", VIDEO_EXTS, ["gif"], "ffmpeg")
def convert_video_to_gif(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _ffmpeg()
    p = _probe(src)
    if not p.video:
        raise ConversionError("This file doesn't contain any video.")
    if p.duration and p.duration > GIF_MAX_SECONDS:
        raise ConversionError("GIFs are limited to 60 seconds — trim the video first.")
    fps, width = QUALITY["gif"][opts.quality]
    vf = (f"fps={fps},scale='min({width},iw)':-2:flags=lanczos,split[a][b];"
          f"[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer")
    args = ["-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf, "-loop", "0", "-f", "gif", str(dst)]
    _run_ffmpeg(args, p.duration, progress, src)
    progress(1.0)


@converter("media_to_audio", VIDEO_EXTS + AUDIO_EXTS, AUDIO_EXTS, "ffmpeg")
def convert_to_audio(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    ffmpeg = _ffmpeg()
    target = dst.suffix.lstrip(".").lower()
    p = _probe(src)
    if not p.audio:
        if p.video and src.suffix.lstrip(".").lower() in VIDEO_EXTS:
            raise ConversionError("This video has no sound to extract.")
        raise ConversionError("This file doesn't contain any audio.")
    # -vn drops video and embedded cover art (wav/flac/etc. can't all carry it).
    args = ["-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "0"]
    args += _audio_args(target, ffmpeg, opts) + [str(dst)]
    _run_ffmpeg(args, p.duration, progress, src)
    progress(1.0)
