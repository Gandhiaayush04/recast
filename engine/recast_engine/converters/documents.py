"""Document conversions via headless LibreOffice (engine "libreoffice").

Groups handled (each within its own group, plus -> pdf):
  text:   docx odt rtf txt
  slides: pptx odp
  sheets: xlsx ods csv   (xlsx/ods -> csv writes ONE csv per sheet)

Parallel safety: every soffice call gets its own throw-away user profile,
otherwise concurrent runs fight over one profile lock and silently fail.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import engines
from ..core import ConversionError, Options, Progress, converter, track_child
from ..formats import GROUPS, DOC_SHEET, DOC_SLIDES, DOC_TEXT, group_of, normalize_ext

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 180

# Quality -> PDF export tuning (only affects embedded images). Applied as JSON
# filter options (LibreOffice 7.4+); other targets have no quality knobs.
PDF_QUALITY: dict[str, dict] = {
    "smaller":  {"ReduceImageResolution": True, "MaxImageResolution": 110, "UseLosslessCompression": False, "Quality": 60},
    "balanced": {"ReduceImageResolution": True, "MaxImageResolution": 200, "UseLosslessCompression": False, "Quality": 85},
    "best":     {"ReduceImageResolution": False, "UseLosslessCompression": True},
}

# target ext -> LibreOffice filter name (pdf and csv are special-cased below)
FILTERS: dict[str, str] = {
    "docx": "MS Word 2007 XML",
    "odt": "writer8",
    "rtf": "Rich Text Format",
    "txt": "Text (encoded):UTF8",
    "xlsx": "Calc MS Excel 2007 XML",
    "ods": "calc8",
    "pptx": "Impress MS PowerPoint 2007 XML",
    "odp": "impress8",
}
# trailing -1 = export every sheet, each to its own file
# (filter name must NOT be quoted when passed without a shell; quotes make LO reject it)
CSV_FILTER = 'csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1'

PDF_FILTER_BY_GROUP = {
    DOC_TEXT: "writer_pdf_Export",
    DOC_SLIDES: "impress_pdf_Export",
    DOC_SHEET: "calc_pdf_Export",
}

# Input filters force UTF-8 for plain-text inputs.
INFILTERS: dict[str, str] = {
    "txt": "Text (encoded):UTF8",
    # Last-but-one field "false" = do NOT evaluate formulas: cells starting with "="
    # stay plain text, so a hostile CSV can't run =HYPERLINK(...) style formulas.
    "csv": "CSV:44,34,76,1,,0,false,true,false,false,false,,false",
}

BAD_FILE = "Couldn't convert this document. It may be damaged or password-protected."
EXTERNAL_CONTENT = ("This document links to files or websites outside itself, "
                    "so Recast won't open it for your safety.")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = "(version 1)(allow default)(deny network-outbound (remote ip))(deny network-inbound (local ip))(deny network-bind (local ip))"

_MAX_SCAN_BYTES = 64 * 1024 * 1024   # per XML part we are willing to inspect

# Settings written into the throwaway LibreOffice profile before launch:
# never update links on load, and keep macros off.
REGISTRY_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
"""

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# xlink:href on these is a clickable hyperlink, not something that gets loaded.
_ODF_LINK_TAGS = {"a"}


def _read_part(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > _MAX_SCAN_BYTES:
        raise ConversionError(BAD_FILE)
    with zf.open(info) as f:
        data = f.read(_MAX_SCAN_BYTES + 1)
    if len(data) > _MAX_SCAN_BYTES:
        raise ConversionError(BAD_FILE)
    return data


def _is_external_href(v: str) -> bool:
    v = v.strip()
    if not v or v.startswith("#"):
        return False
    return bool(_SCHEME.match(v)) or v[0] in "/\\" or v.startswith("..") or v.startswith("~")


def check_no_external_content(path: Path) -> None:
    """Refuse documents that would make LibreOffice load things from outside the
    file (remote images, linked OLE objects, templates, external sheets...).
    Plain clickable hyperlinks are fine."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ConversionError(BAD_FILE)
    with zf:
        for info in zf.infolist():
            name = info.filename
            lower = name.lower()
            try:
                if lower.endswith(".rels"):
                    root = ET.fromstring(_read_part(zf, info))
                    for rel in root.iter():
                        if not rel.tag.endswith("Relationship"):
                            continue
                        if (rel.get("TargetMode") or "").lower() != "external":
                            continue
                        if (rel.get("Type") or "").rstrip("/").lower().endswith("/hyperlink"):
                            continue
                        raise ConversionError(EXTERNAL_CONTENT)
                elif lower in ("content.xml", "styles.xml"):
                    root = ET.fromstring(_read_part(zf, info))
                    for el in root.iter():
                        href = el.get(_XLINK_HREF)
                        if href is None:
                            continue
                        local = el.tag.rsplit("}", 1)[-1]
                        if local in _ODF_LINK_TAGS:
                            continue
                        if _is_external_href(href):
                            raise ConversionError(EXTERNAL_CONTENT)
            except ET.ParseError:
                raise ConversionError(BAD_FILE)
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
                raise ConversionError(BAD_FILE)


ZIP_BASED = {"docx", "pptx", "xlsx", "odt", "odp", "ods"}


def _env() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", tempfile.gettempdir()),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
    }


def _filter_arg(target: str, src_group: str, opts: Options) -> str:
    if target == "pdf":
        name = PDF_FILTER_BY_GROUP[src_group]
        return f"pdf:{name}:{_pdf_json(opts)}"
    if target == "csv":
        return CSV_FILTER
    return f"{target}:{FILTERS[target]}"


def _pdf_json(opts: Options) -> str:
    # LibreOffice JSON filter options want {"Name":{"type":..,"value":..}}
    out = {}
    for k, v in PDF_QUALITY[opts.quality].items():
        if isinstance(v, bool):
            out[k] = {"type": "boolean", "value": "true" if v else "false"}
        else:
            out[k] = {"type": "long", "value": str(v)}
    return json.dumps(out, separators=(",", ":"))


def _run_soffice(soffice: str, src: Path, outdir: Path, target: str, src_group: str,
                 opts: Options, workdir: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="recast-lo-profile-", dir=workdir))
    user_dir = profile / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "registrymodifications.xcu").write_text(REGISTRY_XCU, encoding="utf-8")
    cmd = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--nolockcheck"]
    infilter = INFILTERS.get(normalize_ext(src.suffix))
    if infilter:
        cmd.append(f"--infilter={infilter}")
    cmd += ["--convert-to", _filter_arg(target, src_group, opts), "--outdir", str(outdir), str(src)]
    if os.path.exists(SANDBOX_EXEC):     # macOS: no network access at all for soffice
        cmd = [SANDBOX_EXEC, "-p", SANDBOX_PROFILE] + cmd
    log.debug("soffice cmd: %s", cmd)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_env(), start_new_session=True)
    track_child(proc)
    try:
        out, err = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # soffice launches soffice.bin as a child: kill the whole process group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise ConversionError("This document took too long to convert and was stopped.")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    log.debug("soffice rc=%s out=%r err=%r", proc.returncode, out[-500:], err[-500:])


def _convert(src: Path, dst: Path, target: str, multi: bool, opts: Options, progress: Progress) -> None:
    soffice = engines.find("libreoffice")
    if not soffice:
        raise ConversionError("LibreOffice isn't installed yet.", missing_engine="libreoffice")
    src_ext = normalize_ext(src.suffix)
    src_group = group_of(src_ext)
    progress(0.05)
    with tempfile.TemporaryDirectory(prefix="recast-doc-") as w:
        work = Path(w)
        # Work on a private copy: keeps the original untouched (no lock files
        # beside it) and avoids trouble with odd characters in the real path.
        indir = work / "in"
        outdir = work / "out"
        indir.mkdir()
        outdir.mkdir()
        local = indir / f"{src.stem}.{src_ext}"
        try:
            shutil.copyfile(src, local)
        except OSError:
            raise ConversionError("This file couldn't be read.")
        if src_ext in ZIP_BASED:
            check_no_external_content(local)
        progress(0.1)
        try:
            _run_soffice(soffice, local, outdir, target, src_group, opts, work)
        except OSError:
            raise ConversionError("LibreOffice couldn't be started.", missing_engine="libreoffice")
        progress(0.9)
        results = sorted(p for p in outdir.iterdir() if p.is_file() and p.stat().st_size > 0)
        if not results:
            raise ConversionError(BAD_FILE)
        if multi:
            dst.mkdir(parents=True, exist_ok=True)
            for p in results:
                shutil.move(str(p), str(dst / p.name))
        else:
            shutil.move(str(results[0]), str(dst))
    progress(1.0)


@converter("doc_text", GROUPS[DOC_TEXT], GROUPS[DOC_TEXT] + ["pdf"], "libreoffice")
def convert_text(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _convert(src, dst, dst.suffix.lstrip(".").lower(), False, opts, progress)


@converter("doc_slides", GROUPS[DOC_SLIDES], GROUPS[DOC_SLIDES] + ["pdf"], "libreoffice")
def convert_slides(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _convert(src, dst, dst.suffix.lstrip(".").lower(), False, opts, progress)


# Registered before the generic sheet converter so xlsx/ods -> csv resolves here.
@converter("sheet_to_csv", ["xlsx", "ods"], ["csv"], "libreoffice", multi_output=True)
def convert_sheets_to_csv(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _convert(src, dst, "csv", True, opts, progress)


@converter("doc_sheet", GROUPS[DOC_SHEET], ["xlsx", "ods", "pdf"], "libreoffice")
def convert_sheet(src: Path, dst: Path, opts: Options, progress: Progress) -> None:
    _convert(src, dst, dst.suffix.lstrip(".").lower(), False, opts, progress)
