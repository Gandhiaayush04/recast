"""Unit tests for recast_engine.jobs: output naming, parallel safety, multi-output
folders, skipped files and failures. Uses a fake converter pair, so no real engine
is needed. The fake extensions (fkin -> fkout / fkpages) exist only inside this file."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from recast_engine import core, jobs
from recast_engine.core import ConversionError, Options
from recast_engine.formats import normalize_ext
from recast_engine.formats import targets_for as real_targets


def _fake_single(src: Path, dst: Path, opts: Options, progress) -> None:
    if "fail" in src.stem:
        raise ConversionError("Fake failure for testing.")
    dst.write_bytes(b"fake:" + src.read_bytes())


def _fake_multi(src: Path, dst: Path, opts: Options, progress) -> None:
    if "empty" in src.stem:
        return  # writes nothing -> runner must report a failure
    pages = 1 if src.stem.startswith("one") else 3
    for i in range(1, pages + 1):
        (dst / f"page-{i}.fkpages").write_bytes(f"page {i} of {src.name}".encode())


@pytest.fixture
def fake_engine(monkeypatch):
    """Register fake converters and route fkin -> {fkout, fkpages} through the runner."""
    saved = list(core._REGISTRY)
    core.converter("fake-single", {"fkin"}, {"fkout"}, "fake")(_fake_single)
    core.converter("fake-multi", {"fkin"}, {"fkpages"}, "fake", multi_output=True)(_fake_multi)

    def fake_targets(ext: str) -> list[str]:
        if normalize_ext(ext) == "fkin":
            return ["fkout", "fkpages"]
        return real_targets(ext)

    monkeypatch.setattr(jobs, "targets_for", fake_targets)
    yield
    core._REGISTRY[:] = saved


def _sources(root: Path, *names: str) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in names:
        p = root / n
        p.write_bytes(f"content of {n}".encode())
        paths.append(p)
    return paths


def _run(files, target, out, events=None, group_folder=False):
    events = [] if events is None else events
    jobs_ = jobs.run_batch([Path(f) for f in files], target, Options(), events.append, out,
                           group_folder=group_folder)
    return jobs_, events


# ---- naming collisions ------------------------------------------------------

def test_publish_adds_numbers_on_collision(tmp_path):
    out = tmp_path / "out"
    names = []
    for i in range(3):
        src = tmp_path / f"tmp{i}.bin"
        src.write_bytes(b"%d" % i)
        names.append(jobs.publish(src, out, "x", "pdf").name)
    assert names == ["x.pdf", "x (1).pdf", "x (2).pdf"]


def test_second_run_of_same_file_gets_numbered_name(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "x.fkin")
    first, _ = _run([src], "fkout", out)
    second, _ = _run([src], "fkout", out)
    assert first[0].outputs[0].name == "x.fkout"
    assert second[0].outputs[0].name == "x (1).fkout"
    assert sorted(p.name for p in out.iterdir()) == ["x (1).fkout", "x.fkout"]


def test_collision_within_one_batch(fake_engine, tmp_path):
    out = tmp_path / "out"
    a, b = _sources(tmp_path / "a", "x.fkin") + _sources(tmp_path / "b", "x.fkin")
    jobs_, _ = _run([a, b], "fkout", out)
    assert all(j.status == "done" for j in jobs_)
    assert sorted(p.name for p in out.iterdir()) == ["x (1).fkout", "x.fkout"]


def test_existing_file_on_disk_is_never_overwritten(fake_engine, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "x.fkout").write_bytes(b"original")
    (src,) = _sources(tmp_path / "in", "x.fkin")
    jobs_, _ = _run([src], "fkout", out)
    assert jobs_[0].outputs[0].name == "x (1).fkout"
    assert (out / "x.fkout").read_bytes() == b"original"


def test_default_output_dir_comes_from_environment(fake_engine, tmp_path, isolated_output):
    (src,) = _sources(tmp_path / "in", "y.fkin")
    jobs_, _ = _run([src], "fkout", None)
    assert jobs_[0].outputs[0].parent == isolated_output


# ---- parallel naming --------------------------------------------------------

def test_parallel_publish_with_20_threads_never_collides(tmp_path):
    out = tmp_path / "out"
    sources = []
    for i in range(20):
        p = tmp_path / f"tmp{i}.bin"
        p.write_bytes(f"payload-{i}".encode())
        sources.append(p)

    barrier = threading.Barrier(20)

    def work(p: Path) -> Path:
        barrier.wait()  # release all threads at once to maximise contention
        return jobs.publish(p, out, "same", "txt")

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(work, sources))

    names = sorted(r.name for r in results)
    expected = sorted(["same.txt"] + [f"same ({n}).txt" for n in range(1, 20)])
    assert names == expected
    assert len({r for r in results}) == 20
    assert all(r.exists() for r in results)
    payloads = sorted(r.read_bytes().decode() for r in results)
    assert payloads == sorted(f"payload-{i}" for i in range(20))


def test_parallel_batch_of_20_same_named_files(fake_engine, tmp_path):
    out = tmp_path / "out"
    files = []
    for i in range(20):
        files += _sources(tmp_path / f"dir{i}", "same.fkin")
    jobs_, events = _run(files, "fkout", out)
    assert all(j.status == "done" for j in jobs_), [j.error for j in jobs_ if j.status != "done"]
    names = sorted(p.name for p in out.iterdir())
    assert len(names) == 20 and len(set(names)) == 20
    assert events[-1] == {"event": "batch_done", "ok": 20, "skipped": 0, "failed": 0}


# ---- multi-output folders ---------------------------------------------------

def test_multi_output_default_is_separate_files_in_output_dir(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "report.fkin")
    jobs_, _ = _run([src], "fkpages", out)
    assert jobs_[0].status == "done"
    assert [p.name for p in jobs_[0].outputs] == ["page-1.fkpages", "page-2.fkpages", "page-3.fkpages"]
    assert all(p.parent == out for p in jobs_[0].outputs)
    # running again never overwrites: numbered names
    jobs2, _ = _run([src], "fkpages", out)
    assert jobs2[0].outputs[0].name == "page-1 (1).fkpages"


def test_multi_output_goes_into_folder_named_after_source(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "report.fkin")
    jobs_, _ = _run([src], "fkpages", out, group_folder=True)
    job = jobs_[0]
    assert job.status == "done"
    assert len(job.outputs) == 3
    folder = out / "report"
    assert folder.is_dir()
    assert all(p.parent == folder for p in job.outputs)
    assert [p.name for p in job.outputs] == ["page-1.fkpages", "page-2.fkpages", "page-3.fkpages"]


def test_second_multi_output_run_gets_numbered_folder(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "report.fkin")
    _run([src], "fkpages", out, group_folder=True)
    jobs_, _ = _run([src], "fkpages", out, group_folder=True)
    assert jobs_[0].outputs[0].parent == out / "report (1)"
    assert (out / "report").is_dir() and (out / "report (1)").is_dir()


def test_single_result_from_multi_output_is_a_plain_file(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "one-page.fkin")
    jobs_, _ = _run([src], "fkpages", out)
    assert jobs_[0].outputs == [out / "one-page.fkpages"]
    assert (out / "one-page.fkpages").is_file()


def test_multi_output_with_no_files_fails_and_creates_no_folder(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "empty.fkin")
    jobs_, events = _run([src], "fkpages", out)
    assert jobs_[0].status == "failed"
    assert "no files" in jobs_[0].error
    assert not out.exists() or list(out.iterdir()) == []


# ---- skipped, failed and missing ---------------------------------------------

def test_same_format_is_skipped_and_writes_nothing(tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "photo.jpg")
    jobs_, events = _run([src], "jpeg", out)  # alias of jpg
    assert jobs_[0].status == "skipped"
    assert not out.exists() or list(out.iterdir()) == []
    assert any(e["event"] == "skipped" for e in events)
    assert events[-1] == {"event": "batch_done", "ok": 0, "skipped": 1, "failed": 0}


def test_skipped_and_real_files_in_one_batch(fake_engine, tmp_path):
    out = tmp_path / "out"
    skip = _sources(tmp_path / "a", "keep.fkin")[0]
    (fkout_src,) = _sources(tmp_path / "b", "photo.jpg")
    jobs_, events = _run([skip, fkout_src], "jpg", out)
    statuses = {j.src.name: j.status for j in jobs_}
    assert statuses["photo.jpg"] == "skipped"
    assert statuses["keep.fkin"] == "failed"  # fkin cannot become jpg


def test_failure_does_not_stop_other_files(fake_engine, tmp_path):
    out = tmp_path / "out"
    ok, bad = _sources(tmp_path / "a", "ok.fkin") + _sources(tmp_path / "b", "fail-me.fkin")
    jobs_, events = _run([ok, bad], "fkout", out)
    by_name = {j.src.name: j for j in jobs_}
    assert by_name["ok.fkin"].status == "done"
    assert by_name["fail-me.fkin"].status == "failed"
    assert by_name["fail-me.fkin"].error == "Fake failure for testing."
    assert events[-1] == {"event": "batch_done", "ok": 1, "skipped": 0, "failed": 1}
    assert [p.name for p in out.iterdir()] == ["ok.fkout"]


def test_unsupported_pair_fails_with_plain_message(fake_engine, tmp_path):
    out = tmp_path / "out"
    (src,) = _sources(tmp_path / "in", "x.fkin")
    jobs_, _ = _run([src], "png", out)
    assert jobs_[0].status == "failed"
    assert jobs_[0].error == "Can't convert .fkin files to PNG."


def test_missing_source_fails_with_plain_message(fake_engine, tmp_path):
    out = tmp_path / "out"
    jobs_, _ = _run([tmp_path / "gone.fkin"], "fkout", out)
    assert jobs_[0].status == "failed"
    assert "couldn't be found" in jobs_[0].error


def test_queued_events_come_first_for_every_file(fake_engine, tmp_path):
    out = tmp_path / "out"
    files = _sources(tmp_path / "in", "a.fkin", "b.fkin", "c.fkin")
    _, events = _run(files, "fkout", out)
    queued = [e for e in events if e["event"] == "queued"]
    assert len(queued) == 3
    assert {e["id"] for e in queued} == {0, 1, 2}
    assert events.index(queued[-1]) < min(i for i, e in enumerate(events) if e["event"] in ("started", "done"))
