# Corpus state boundary: turns the files on disk into one immutable snapshot per refresh.
#
# Refresh model (ADR-005): called at the start of each chat request. It re-scans the
# root, reuses documents whose fingerprint is unchanged, re-reads new or changed ones,
# and builds the next snapshot only from what is on disk now. Documents that are gone,
# or that could not be read consistently this time, are simply absent from the new
# snapshot, so nothing stale can survive a deletion (REQ-044).
#
# Consistency: each request uses exactly one snapshot, built in full before it is
# published, so a request never sees half of one refresh and half of another.
# A change is "ready to serve" on the first refresh that starts after the file has
# been quiet for the settle window and is then read without changing.

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app import ingestion
from app.ingestion import Document, Skip

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshStats:
    added: int
    modified: int
    removed: int
    unchanged: int
    skipped: int
    duration_ms: float


@dataclass(frozen=True)
class Snapshot:
    # Sorted by rel_path so downstream ordering is deterministic.
    documents: tuple[Document, ...]
    skips: tuple[Skip, ...]
    # Increases only when the set or content of served documents changes, so two
    # requests can tell whether they saw the same corpus.
    version: int
    stats: RefreshStats


EMPTY = Snapshot(documents=(), skips=(), version=0, stats=RefreshStats(0, 0, 0, 0, 0, 0.0))


class Corpus:
    def __init__(
        self,
        root: str,
        max_file_bytes: int,
        max_files: int,
        settle_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = root
        self._max_file_bytes = max_file_bytes
        self._max_files = max_files
        self._settle_seconds = settle_seconds
        # Wall clock, because it is compared with file mtimes. Injectable for tests.
        self._clock = clock
        # Serialises refreshes: concurrent requests must not interleave reads of the
        # cache. File I/O is blocking, so callers on the event loop run refresh() in a
        # worker thread.
        self._lock = threading.Lock()
        self._snapshot = EMPTY

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def refresh(self) -> Snapshot:
        with self._lock:
            started = time.perf_counter()
            previous = {d.rel_path: d for d in self._snapshot.documents}
            candidates, skips = ingestion.scan(self._root)

            # Deterministic cut-off: the first max_files paths in sorted order are served.
            for extra in candidates[self._max_files:]:
                skips.append(Skip(extra.rel_path, "file_limit_exceeded", ingestion.SKIP_SEVERITY["file_limit_exceeded"]))
            candidates = candidates[: self._max_files]

            documents: list[Document] = []
            added = modified = unchanged = 0
            now = self._clock()
            for candidate in candidates:
                cached = previous.get(candidate.rel_path)
                if cached is not None and cached.fingerprint == candidate.fingerprint:
                    documents.append(cached)
                    unchanged += 1
                    continue
                result = ingestion.read(self._root, candidate, self._max_file_bytes, self._settle_seconds, now)
                if isinstance(result, Skip):
                    # Not served this time, even if an older version was cached: we only
                    # serve content we have just read in full in its current state.
                    skips.append(result)
                    continue
                documents.append(result)
                if cached is None:
                    added += 1
                else:
                    modified += 1

            served = {d.rel_path for d in documents}
            removed = sum(1 for path in previous if path not in served)
            changed = added or modified or removed
            snapshot = Snapshot(
                documents=tuple(documents),
                skips=tuple(sorted(skips, key=lambda s: s.rel_path)),
                version=self._snapshot.version + 1 if changed else self._snapshot.version,
                stats=RefreshStats(
                    added=added,
                    modified=modified,
                    removed=removed,
                    unchanged=unchanged,
                    skipped=len(skips),
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                ),
            )
            self._snapshot = snapshot

        self._log(snapshot)
        return snapshot

    def _log(self, snapshot: Snapshot) -> None:
        # Paths and counts only: never document content (REQ-068, REQ-084).
        s = snapshot.stats
        log.info(
            "corpus refreshed: version=%d documents=%d added=%d modified=%d removed=%d "
            "unchanged=%d skipped=%d duration_ms=%.1f",
            snapshot.version, len(snapshot.documents), s.added, s.modified, s.removed,
            s.unchanged, s.skipped, s.duration_ms,
        )
        for skip in snapshot.skips:
            level = logging.WARNING if skip.severity == "error" else logging.DEBUG
            log.log(level, "corpus file skipped: path=%s reason=%s", skip.rel_path, skip.reason)
