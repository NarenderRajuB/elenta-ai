# Tests for ingestion (app/ingestion.py) and corpus state (app/corpus.py).
#
# Organised by requirement with positive / negative / edge cases. Every test uses a
# real temporary directory, so filesystem behaviour (permissions, symlinks, FIFOs,
# renames) is exercised for real rather than mocked. The scenario list is mirrored
# in README.md.

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from app import ingestion
from app.corpus import Corpus
from app.ingestion import Candidate, Skip

MB = 1024 * 1024


def make_corpus(root: Path, **overrides) -> Corpus:
    options = {"max_file_bytes": MB, "max_files": 500, "settle_seconds": 0.0}
    options.update(overrides)
    return Corpus(root=str(root), **options)


def write(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def served(snapshot) -> dict[str, str]:
    return {d.rel_path: d.text for d in snapshot.documents}


def reasons(snapshot) -> dict[str, str]:
    return {s.rel_path: s.reason for s in snapshot.skips}


# ---------------------------------------------------------------------------
# REQ-041: UTF-8 .txt and .md supported; every format and limit stated
# ---------------------------------------------------------------------------

class TestReq041Formats:
    def test_positive_txt_and_md_are_served(self, tmp_path):
        write(tmp_path, "a.txt", "alpha")
        write(tmp_path, "b.md", "# beta")
        assert served(make_corpus(tmp_path).refresh()) == {"a.txt": "alpha", "b.md": "# beta"}

    def test_positive_nested_directories_use_posix_relative_paths(self, tmp_path):
        write(tmp_path, "policies/hr/leave.md", "leave")
        assert list(served(make_corpus(tmp_path).refresh())) == ["policies/hr/leave.md"]

    @pytest.mark.parametrize("name", ["notes.pdf", "image.png", "data.json", "README", "archive.txt.gz"])
    def test_negative_unsupported_types_skipped_and_recorded(self, tmp_path, name):
        write(tmp_path, name, "content")
        snap = make_corpus(tmp_path).refresh()
        assert served(snap) == {}
        assert reasons(snap) == {name: "unsupported_type"}

    @pytest.mark.parametrize("name", ["UPPER.TXT", "Mixed.Md"])
    def test_edge_extension_match_is_case_insensitive(self, tmp_path, name):
        write(tmp_path, name, "x")
        assert name in served(make_corpus(tmp_path).refresh())

    def test_edge_utf8_bom_is_removed(self, tmp_path):
        write(tmp_path, "bom.txt", "\ufeffhello".encode("utf-8"))
        assert served(make_corpus(tmp_path).refresh())["bom.txt"] == "hello"

    def test_edge_unicode_and_space_in_filename(self, tmp_path):
        write(tmp_path, "café notes.md", "naïve résumé")
        assert served(make_corpus(tmp_path).refresh()) == {"café notes.md": "naïve résumé"}

    def test_edge_non_ascii_content_preserved(self, tmp_path):
        write(tmp_path, "multi.txt", "日本語 — العربية — emoji 🚀")
        assert served(make_corpus(tmp_path).refresh())["multi.txt"] == "日本語 — العربية — emoji 🚀"


# ---------------------------------------------------------------------------
# Limits: per-file size (50 MB default) and file count (500 default)
# ---------------------------------------------------------------------------

class TestLimits:
    def test_positive_file_exactly_at_size_limit_served(self, tmp_path):
        write(tmp_path, "edge.txt", "x" * 100)
        assert "edge.txt" in served(make_corpus(tmp_path, max_file_bytes=100).refresh())

    def test_negative_file_over_size_limit_skipped_without_reading(self, tmp_path, monkeypatch):
        write(tmp_path, "big.txt", "x" * 101)
        reads = []
        monkeypatch.setattr(ingestion, "_read_bytes", lambda fd, limit: reads.append(fd) or b"")
        snap = make_corpus(tmp_path, max_file_bytes=100).refresh()
        assert reasons(snap) == {"big.txt": "too_large"} and reads == []

    def test_edge_file_grown_past_limit_between_scan_and_read(self, tmp_path):
        root = tmp_path
        path = write(root, "grow.txt", "x" * 50)
        [candidate], _ = ingestion.scan(str(root))
        path.write_text("x" * 500, encoding="utf-8")
        result = ingestion.read(str(root), candidate, 100, 0, time.time())
        # Detected as a change (fingerprint differs) and never served.
        assert isinstance(result, Skip) and result.reason in {"changing", "too_large"}

    def test_negative_files_over_count_limit_skipped_deterministically(self, tmp_path):
        for name in ["c.txt", "a.txt", "b.txt"]:
            write(tmp_path, name, name)
        snap = make_corpus(tmp_path, max_files=2).refresh()
        assert sorted(served(snap)) == ["a.txt", "b.txt"]
        assert reasons(snap) == {"c.txt": "file_limit_exceeded"}

    def test_edge_count_limit_counts_only_supported_files(self, tmp_path):
        write(tmp_path, ".hidden.txt", "h")
        write(tmp_path, "skip.pdf", "p")
        write(tmp_path, "a.txt", "a")
        write(tmp_path, "b.txt", "b")
        assert sorted(served(make_corpus(tmp_path, max_files=2).refresh())) == ["a.txt", "b.txt"]

    def test_edge_default_limits_from_config(self):
        from app.config import DEFAULT_CORPUS_MAX_FILE_BYTES, DEFAULT_CORPUS_MAX_FILES
        assert DEFAULT_CORPUS_MAX_FILE_BYTES == 50 * MB
        assert DEFAULT_CORPUS_MAX_FILES == 500


# ---------------------------------------------------------------------------
# REQ-042 / REQ-003: add, modify, rename, remove reflected without restart
# ---------------------------------------------------------------------------

class TestReq042LiveChanges:
    def test_positive_added_file_appears_on_next_refresh(self, tmp_path):
        corpus = make_corpus(tmp_path)
        assert served(corpus.refresh()) == {}
        write(tmp_path, "new.txt", "fresh")
        snap = corpus.refresh()
        assert served(snap) == {"new.txt": "fresh"} and snap.stats.added == 1

    def test_positive_modified_file_shows_new_content_only(self, tmp_path):
        corpus = make_corpus(tmp_path)
        write(tmp_path, "doc.txt", "version one")
        corpus.refresh()
        write(tmp_path, "doc.txt", "version two, longer")
        snap = corpus.refresh()
        assert served(snap) == {"doc.txt": "version two, longer"} and snap.stats.modified == 1

    def test_positive_renamed_file_is_remove_plus_add(self, tmp_path):
        corpus = make_corpus(tmp_path)
        write(tmp_path, "old.txt", "same content")
        corpus.refresh()
        (tmp_path / "old.txt").rename(tmp_path / "new.txt")
        snap = corpus.refresh()
        assert served(snap) == {"new.txt": "same content"}
        assert (snap.stats.added, snap.stats.removed) == (1, 1)

    def test_positive_unchanged_files_are_not_re_read(self, tmp_path, monkeypatch):
        corpus = make_corpus(tmp_path)
        write(tmp_path, "a.txt", "a")
        corpus.refresh()
        calls = []
        real_read = ingestion.read
        monkeypatch.setattr(ingestion, "read", lambda *a, **k: calls.append(a) or real_read(*a, **k))
        snap = corpus.refresh()
        assert calls == [] and snap.stats.unchanged == 1

    def test_positive_version_increments_only_when_served_set_changes(self, tmp_path):
        corpus = make_corpus(tmp_path)
        write(tmp_path, "a.txt", "a")
        v1 = corpus.refresh().version
        v2 = corpus.refresh().version
        write(tmp_path, "a.txt", "changed")
        v3 = corpus.refresh().version
        assert v1 == v2 and v3 == v1 + 1

    def test_edge_same_size_edit_with_restored_mtime_detected(self, tmp_path):
        # Content changes but size and mtime are forced back: ctime still moves.
        corpus = make_corpus(tmp_path)
        path = write(tmp_path, "doc.txt", "AAAA")
        st = path.stat()
        corpus.refresh()
        time.sleep(0.01)
        path.write_text("BBBB", encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert served(corpus.refresh()) == {"doc.txt": "BBBB"}

    def test_edge_atomic_replace_via_rename_detected(self, tmp_path):
        # Editors often save by writing a temp file and renaming it over the original.
        corpus = make_corpus(tmp_path)
        write(tmp_path, "doc.md", "before")
        corpus.refresh()
        write(tmp_path, "tmp-save", "after")
        os.replace(tmp_path / "tmp-save", tmp_path / "doc.md")
        assert served(corpus.refresh()) == {"doc.md": "after"}

    def test_edge_rapid_successive_changes_each_reflected(self, tmp_path):
        corpus = make_corpus(tmp_path)
        for i in range(20):
            write(tmp_path, "doc.txt", f"revision {i}")
            assert served(corpus.refresh()) == {"doc.txt": f"revision {i}"}


# ---------------------------------------------------------------------------
# REQ-044: removed documents stop contributing; nothing stale survives deletion
# ---------------------------------------------------------------------------

class TestReq044Deletion:
    def test_positive_deleted_file_is_gone(self, tmp_path):
        corpus = make_corpus(tmp_path)
        path = write(tmp_path, "secret-plan.txt", "delete me")
        corpus.refresh()
        path.unlink()
        snap = corpus.refresh()
        assert served(snap) == {} and snap.stats.removed == 1

    def test_positive_deleted_content_not_retained_anywhere_in_snapshot(self, tmp_path):
        corpus = make_corpus(tmp_path)
        write(tmp_path, "keep.txt", "keep")
        path = write(tmp_path, "gone.txt", "UNIQUE-MARKER-123")
        corpus.refresh()
        path.unlink()
        snap = corpus.refresh()
        assert "UNIQUE-MARKER-123" not in repr(snap)
        assert "UNIQUE-MARKER-123" not in repr(corpus.snapshot)

    def test_negative_deleting_directory_removes_all_its_documents(self, tmp_path):
        corpus = make_corpus(tmp_path)
        write(tmp_path, "dir/a.txt", "a")
        write(tmp_path, "dir/b.txt", "b")
        corpus.refresh()
        for p in (tmp_path / "dir").iterdir():
            p.unlink()
        (tmp_path / "dir").rmdir()
        assert served(corpus.refresh()) == {}

    def test_edge_served_file_that_becomes_unreadable_is_not_served_stale(self, tmp_path):
        corpus = make_corpus(tmp_path)
        path = write(tmp_path, "doc.txt", "old good content")
        corpus.refresh()
        path.write_bytes(b"\xff\xfe broken")
        snap = corpus.refresh()
        assert served(snap) == {} and reasons(snap) == {"doc.txt": "not_utf8"}

    def test_edge_corpus_root_removed_while_running(self, tmp_path):
        root = tmp_path / "data"
        write(root, "a.txt", "a")
        corpus = make_corpus(root)
        corpus.refresh()
        (root / "a.txt").unlink()
        root.rmdir()
        snap = corpus.refresh()
        assert served(snap) == {} and reasons(snap) == {".": "corpus_dir_missing"}


# ---------------------------------------------------------------------------
# REQ-045: rapid changes and partially written files never served mixed
# ---------------------------------------------------------------------------

class TestReq045PartialWrites:
    def test_positive_file_served_once_settled(self, tmp_path):
        path = write(tmp_path, "doc.txt", "complete")
        mtime = path.stat().st_mtime
        clock = {"now": mtime + 0.1}
        corpus = make_corpus(tmp_path, settle_seconds=0.5, clock=lambda: clock["now"])
        assert reasons(corpus.refresh()) == {"doc.txt": "settling"}
        clock["now"] = mtime + 1.0
        assert served(corpus.refresh()) == {"doc.txt": "complete"}

    def test_negative_file_changing_during_read_is_not_served(self, tmp_path, monkeypatch):
        path = write(tmp_path, "doc.txt", "first half")
        real = ingestion._read_bytes

        def read_then_append(fd, limit):
            data = real(fd, limit)
            with open(path, "a", encoding="utf-8") as f:
                f.write(" second half")
            return data

        monkeypatch.setattr(ingestion, "_read_bytes", read_then_append)
        snap = make_corpus(tmp_path).refresh()
        assert served(snap) == {} and reasons(snap) == {"doc.txt": "changing"}

    def test_negative_previous_version_not_served_while_file_is_changing(self, tmp_path, monkeypatch):
        # Conservative choice (ADR-005): only content just read in full is served.
        corpus = make_corpus(tmp_path)
        path = write(tmp_path, "doc.txt", "v1")
        corpus.refresh()
        write(tmp_path, "doc.txt", "v2 partial")
        real = ingestion._read_bytes

        def read_then_append(fd, limit):
            data = real(fd, limit)
            with open(path, "a", encoding="utf-8") as f:
                f.write("...")
            return data

        monkeypatch.setattr(ingestion, "_read_bytes", read_then_append)
        snap = corpus.refresh()
        assert served(snap) == {} and reasons(snap) == {"doc.txt": "changing"}
        monkeypatch.setattr(ingestion, "_read_bytes", real)
        assert served(corpus.refresh()) == {"doc.txt": "v2 partial..."}

    def test_edge_future_mtime_waits_rather_than_serving(self, tmp_path):
        path = write(tmp_path, "doc.txt", "x")
        future = time.time() + 3600
        os.utime(path, (future, future))
        assert reasons(make_corpus(tmp_path, settle_seconds=0.5).refresh()) == {"doc.txt": "settling"}

    def test_edge_settle_zero_serves_even_with_slightly_future_mtime(self, tmp_path):
        # Regression for TS-004: Docker Desktop's VM clock can trail the host by a few
        # milliseconds, so a just-written file looks slightly "from the future".
        path = write(tmp_path, "doc.txt", "x")
        future = time.time() + 0.05
        os.utime(path, (future, future))
        assert served(make_corpus(tmp_path, settle_seconds=0).refresh()) == {"doc.txt": "x"}

    def test_edge_settle_zero_serves_immediately(self, tmp_path):
        write(tmp_path, "doc.txt", "now")
        assert served(make_corpus(tmp_path, settle_seconds=0).refresh()) == {"doc.txt": "now"}

    def test_edge_concurrent_refreshes_do_not_corrupt_state(self, tmp_path):
        for i in range(30):
            write(tmp_path, f"f{i:02}.txt", f"content {i}")
        corpus = make_corpus(tmp_path)
        errors = []

        def worker():
            try:
                for _ in range(5):
                    snap = corpus.refresh()
                    assert len(snap.documents) == 30
            except Exception as exc:  # noqa: BLE001 - surfaced via the errors list
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [] and len(corpus.snapshot.documents) == 30


# ---------------------------------------------------------------------------
# REQ-056 / REQ-073: unreadable, unsupported or corrupt files do not take down the service
# ---------------------------------------------------------------------------

class TestReq056CorruptFiles:
    @pytest.mark.parametrize(
        "content, reason",
        [
            (b"\xff\xfe\xfd invalid utf-8", "not_utf8"),
            (b"text with \x00 NUL byte", "binary_content"),
            (b"", "empty"),
            (b"   \n\t  ", "empty"),
            (b"\xef\xbb\xbf", "empty"),
        ],
        ids=["invalid-utf8", "nul-byte", "zero-bytes", "whitespace-only", "bom-only"],
    )
    def test_negative_bad_file_skipped_valid_files_still_served(self, tmp_path, content, reason):
        write(tmp_path, "bad.txt", content)
        write(tmp_path, "good.txt", "valid evidence")
        snap = make_corpus(tmp_path).refresh()
        assert served(snap) == {"good.txt": "valid evidence"}
        assert reasons(snap) == {"bad.txt": reason}

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_negative_permission_denied_file_recorded(self, tmp_path):
        path = write(tmp_path, "locked.txt", "secret")
        write(tmp_path, "open.txt", "fine")
        path.chmod(0o000)
        try:
            snap = make_corpus(tmp_path).refresh()
        finally:
            path.chmod(0o644)
        assert served(snap) == {"open.txt": "fine"} and reasons(snap) == {"locked.txt": "unreadable"}

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_negative_unlistable_subdirectory_recorded(self, tmp_path):
        write(tmp_path, "locked/inner.txt", "x")
        write(tmp_path, "ok.txt", "fine")
        (tmp_path / "locked").chmod(0o000)
        try:
            snap = make_corpus(tmp_path).refresh()
        finally:
            (tmp_path / "locked").chmod(0o755)
        assert served(snap) == {"ok.txt": "fine"} and reasons(snap) == {"locked": "unreadable"}

    def test_edge_error_skips_logged_as_warnings_without_content(self, tmp_path, caplog):
        write(tmp_path, "bad.txt", b"\xff\xfe CONTENT-MARKER")
        write(tmp_path, "good.txt", "GOOD-CONTENT-MARKER")
        with caplog.at_level(logging.DEBUG, logger="app.corpus"):
            make_corpus(tmp_path).refresh()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == ["corpus file skipped: path=bad.txt reason=not_utf8"]
        assert "MARKER" not in caplog.text

    def test_edge_policy_skips_are_not_warnings(self, tmp_path, caplog):
        write(tmp_path, ".gitkeep", "")
        write(tmp_path, "x.pdf", "p")
        with caplog.at_level(logging.DEBUG, logger="app.corpus"):
            make_corpus(tmp_path).refresh()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_edge_invalid_filename_detection(self):
        # APFS refuses non-UTF-8 names, so the check is tested directly with the
        # surrogate form Python uses for undecodable bytes on Linux.
        assert ingestion._is_valid_name("ok-name.txt")
        assert not ingestion._is_valid_name("bad-\udcff.txt")


# ---------------------------------------------------------------------------
# REQ-064: reads restricted to the corpus root; symlinks, hidden, special files
# ---------------------------------------------------------------------------

class TestReq064Boundary:
    def test_positive_only_files_under_root_are_read(self, tmp_path):
        root = tmp_path / "data"
        write(root, "in.txt", "inside")
        write(tmp_path, "outside.txt", "OUTSIDE")
        assert served(make_corpus(root).refresh()) == {"in.txt": "inside"}

    def test_negative_symlinked_file_skipped_even_if_target_inside_root(self, tmp_path):
        write(tmp_path, "real.txt", "real")
        os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
        snap = make_corpus(tmp_path).refresh()
        assert served(snap) == {"real.txt": "real"} and reasons(snap) == {"link.txt": "symlink"}

    def test_negative_symlink_to_outside_root_never_read(self, tmp_path):
        root = tmp_path / "data"
        root.mkdir()
        write(tmp_path, "secret.txt", "TOP-SECRET")
        os.symlink(tmp_path / "secret.txt", root / "escape.txt")
        snap = make_corpus(root).refresh()
        assert served(snap) == {} and "TOP-SECRET" not in repr(snap)

    def test_negative_symlinked_directory_not_traversed(self, tmp_path):
        root = tmp_path / "data"
        root.mkdir()
        write(tmp_path, "outside/doc.txt", "OUTSIDE")
        os.symlink(tmp_path / "outside", root / "linkdir")
        snap = make_corpus(root).refresh()
        assert served(snap) == {} and reasons(snap) == {"linkdir": "symlink"}

    def test_negative_hidden_files_and_directories_skipped(self, tmp_path):
        write(tmp_path, ".env.txt", "hidden file")
        write(tmp_path, ".git/notes.md", "hidden dir")
        write(tmp_path, "visible.txt", "ok")
        snap = make_corpus(tmp_path).refresh()
        assert served(snap) == {"visible.txt": "ok"}
        assert reasons(snap) == {".env.txt": "hidden", ".git": "hidden"}

    def test_negative_fifo_never_opened(self, tmp_path):
        # Opening a FIFO for reading blocks until a writer appears; it must be skipped.
        os.mkfifo(tmp_path / "pipe.txt")
        write(tmp_path, "ok.txt", "ok")
        start = time.monotonic()
        snap = make_corpus(tmp_path).refresh()
        assert time.monotonic() - start < 2
        assert reasons(snap) == {"pipe.txt": "not_regular_file"}

    def test_edge_file_swapped_for_symlink_after_scan_not_followed(self, tmp_path):
        root = tmp_path / "data"
        path = write(root, "doc.txt", "original")
        write(tmp_path, "secret.txt", "TOP-SECRET")
        [candidate], _ = ingestion.scan(str(root))
        path.unlink()
        os.symlink(tmp_path / "secret.txt", path)
        result = ingestion.read(str(root), candidate, MB, 0, time.time())
        assert isinstance(result, Skip) and result.reason in {"unreadable", "outside_root"}

    def test_edge_directory_swapped_for_symlink_detected_as_outside_root(self, tmp_path):
        root = tmp_path / "data"
        write(root, "sub/doc.txt", "inside")
        [candidate], _ = ingestion.scan(str(root))
        write(tmp_path, "elsewhere/doc.txt", "OUTSIDE")
        for p in (root / "sub").iterdir():
            p.unlink()
        (root / "sub").rmdir()
        os.symlink(tmp_path / "elsewhere", root / "sub")
        result = ingestion.read(str(root), candidate, MB, 0, time.time())
        assert isinstance(result, Skip) and result.reason == "outside_root"

    def test_edge_missing_root_recorded_not_raised(self, tmp_path):
        snap = make_corpus(tmp_path / "missing").refresh()
        assert served(snap) == {} and reasons(snap) == {".": "corpus_dir_missing"}

    def test_edge_root_is_a_file_recorded_not_raised(self, tmp_path):
        path = write(tmp_path, "file.txt", "x")
        assert reasons(make_corpus(path).refresh()) == {".": "corpus_dir_missing"}

    def test_edge_candidate_path_is_never_taken_from_outside(self, tmp_path):
        # read() rejects a hand-built candidate pointing outside the root.
        root = tmp_path / "data"
        root.mkdir()
        outside = write(tmp_path, "x.txt", "OUTSIDE")
        st = outside.stat()
        fp = ingestion.Fingerprint(st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        result = ingestion.read(str(root), Candidate("../x.txt", str(outside), fp), MB, 0, time.time())
        assert isinstance(result, Skip) and result.reason == "outside_root"
