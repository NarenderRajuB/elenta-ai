# Ingestion boundary: finds candidate files under the corpus root and reads them safely.
#
# This module only touches the filesystem and turns bytes into text. It holds no state;
# deciding what is new, changed or deleted belongs to app/corpus.py (ADR-005).
#
# Safety rules (REQ-064, REQ-056, REQ-045, REQ-073), each producing a recorded skip
# rather than an exception:
#   - only regular files with a supported extension under the root are read;
#   - symbolic links (files and directories) are never followed;
#   - hidden files and directories (name starts with ".") are skipped;
#   - pipes, sockets and devices are never opened (opening a FIFO can block forever);
#   - files over the size limit are skipped without being read;
#   - a file must be unchanged across the whole read, and older than the settle
#     window, so a half-written or concurrently modified file is never served;
#   - content must be valid UTF-8 without NUL bytes (NUL means binary or corrupt).
# Document text is only ever treated as data: nothing here parses, renders or executes it.

import hashlib
import os
import stat
from dataclasses import dataclass

# Extensions are matched case-insensitively (README.MD is still Markdown).
SUPPORTED_EXTENSIONS = (".txt", ".md")


@dataclass(frozen=True)
class Fingerprint:
    # Cheap change detection without reading content. ctime cannot be set by tools
    # like `touch -m` or `cp -p`, so it catches edits that keep size and mtime.
    # inode catches a file replaced by rename (editors' atomic save).
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int


@dataclass(frozen=True)
class Candidate:
    rel_path: str  # POSIX-style path relative to the corpus root, e.g. "policies/leave.md"
    abs_path: str
    fingerprint: Fingerprint


@dataclass(frozen=True)
class Document:
    rel_path: str
    text: str
    fingerprint: Fingerprint
    # SHA-256 of the raw bytes: a content identity that later stages can use for
    # stable chunk identifiers (INT-04) without keeping the bytes around.
    sha256: str


@dataclass(frozen=True)
class Skip:
    rel_path: str
    # Fixed reason codes, documented in README. "error" severity means a supported
    # file could not be used; "info" means it was skipped by policy.
    reason: str
    severity: str


# reason -> severity. Policy skips are expected (e.g. .gitkeep); errors need attention.
SKIP_SEVERITY = {
    "hidden": "info",
    "symlink": "info",
    "unsupported_type": "info",
    "not_regular_file": "info",
    "empty": "info",
    "settling": "info",
    "too_large": "error",
    "file_limit_exceeded": "error",
    "changing": "error",
    "not_utf8": "error",
    "binary_content": "error",
    "invalid_filename": "error",
    "unreadable": "error",
    "outside_root": "error",
    "corpus_dir_missing": "error",
}


def _skip(rel_path: str, reason: str) -> Skip:
    return Skip(rel_path=rel_path, reason=reason, severity=SKIP_SEVERITY[reason])


def _fingerprint(st: os.stat_result) -> Fingerprint:
    return Fingerprint(size=st.st_size, mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, inode=st.st_ino)


def _is_valid_name(name: str) -> bool:
    # Undecodable bytes in a filename surface as lone surrogates; such a name cannot be
    # shown, logged or cited reliably, so the file is skipped. So is a name containing
    # control characters (line breaks, tabs, escape codes...): it would start new lines
    # inside the prompt's evidence header and in log output (ADR-006 C2).
    if any(ch < " " or ch == "\x7f" for ch in name):
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def scan(root: str) -> tuple[list[Candidate], list[Skip]]:
    """Walk the corpus root without following links; return readable-looking candidates and skips.

    Candidates are sorted by relative path so limits and ordering are deterministic.
    """
    candidates: list[Candidate] = []
    skips: list[Skip] = []
    try:
        root_st = os.stat(root)
    except OSError:
        return [], [_skip(".", "corpus_dir_missing")]
    if not stat.S_ISDIR(root_st.st_mode):
        return [], [_skip(".", "corpus_dir_missing")]

    # Explicit stack instead of os.walk: every entry is classified with lstat
    # semantics (follow_symlinks=False), so no link is ever traversed.
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            entries = list(os.scandir(abs_dir))
        except OSError:
            skips.append(_skip(rel_dir or ".", "unreadable"))
            continue
        for entry in entries:
            rel_path = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if not _is_valid_name(entry.name):
                skips.append(_skip(rel_path.encode("utf-8", "replace").decode("utf-8"), "invalid_filename"))
                continue
            if entry.name.startswith("."):
                skips.append(_skip(rel_path, "hidden"))
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                skips.append(_skip(rel_path, "unreadable"))
                continue
            if stat.S_ISLNK(st.st_mode):
                skips.append(_skip(rel_path, "symlink"))
            elif stat.S_ISDIR(st.st_mode):
                stack.append(rel_path)
            elif not stat.S_ISREG(st.st_mode):
                skips.append(_skip(rel_path, "not_regular_file"))
            elif not entry.name.lower().endswith(SUPPORTED_EXTENSIONS):
                skips.append(_skip(rel_path, "unsupported_type"))
            else:
                candidates.append(Candidate(rel_path=rel_path, abs_path=entry.path, fingerprint=_fingerprint(st)))

    candidates.sort(key=lambda c: c.rel_path)
    skips.sort(key=lambda s: s.rel_path)
    return candidates, skips


def _read_bytes(fd: int, limit: int) -> bytes:
    # Reads at most limit+1 bytes so a file that grew past the limit after the scan
    # is detected without reading it all. Separate function so tests can interpose.
    chunks = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _is_within(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root


def read(root: str, candidate: Candidate, max_bytes: int, settle_seconds: float, now: float) -> Document | Skip:
    """Read one candidate as a single consistent version, or return why it was skipped."""
    rel = candidate.rel_path
    if candidate.fingerprint.size > max_bytes:
        return _skip(rel, "too_large")
    if candidate.fingerprint.size == 0:
        return _skip(rel, "empty")
    # Recently modified: a writer may still be appending. Retried on the next refresh.
    # A future mtime (clock skew between host and container) also waits, by design.
    # settle_seconds == 0 disables the check entirely: on Docker Desktop the VM clock
    # can trail the host by milliseconds, so a fresh file's age can be slightly
    # negative and "age < 0" would otherwise still skip it (TS-004).
    if settle_seconds > 0 and now - candidate.fingerprint.mtime_ns / 1e9 < settle_seconds:
        return _skip(rel, "settling")
    # Defence in depth: the walk never follows links, but a directory could be swapped
    # for a link between scan and read.
    if not _is_within(root, candidate.abs_path):
        return _skip(rel, "outside_root")

    try:
        # O_NOFOLLOW: if the file was replaced by a symlink after the scan, open fails
        # instead of reading the link target. O_NONBLOCK: never hang on a FIFO swap-in.
        fd = os.open(candidate.abs_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return _skip(rel, "unreadable")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return _skip(rel, "not_regular_file")
        data = _read_bytes(fd, max_bytes)
        after = os.fstat(fd)
    except OSError:
        return _skip(rel, "unreadable")
    finally:
        os.close(fd)

    # The open file must match what the scan saw, must not change while being read,
    # and the path must still point at it (not replaced by a rename mid-read).
    try:
        on_disk = os.stat(candidate.abs_path, follow_symlinks=False)
    except OSError:
        return _skip(rel, "changing")
    seen = {_fingerprint(before), _fingerprint(after), _fingerprint(on_disk), candidate.fingerprint}
    if len(seen) != 1 or len(data) != before.st_size:
        return _skip(rel, "changing")
    if len(data) > max_bytes:
        return _skip(rel, "too_large")

    if b"\x00" in data:
        return _skip(rel, "binary_content")
    try:
        # utf-8-sig accepts and drops a leading BOM, common in files saved on Windows.
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _skip(rel, "not_utf8")
    # A BOM-only or whitespace-only file has no evidence to offer.
    if not text.strip():
        return _skip(rel, "empty")
    return Document(rel_path=rel, text=text, fingerprint=candidate.fingerprint, sha256=hashlib.sha256(data).hexdigest())
