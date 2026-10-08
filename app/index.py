# Indexing boundary: a BM25 keyword index over the chunks of one corpus snapshot.
#
# Why BM25 (ADR-007): it runs fully offline with no second model, and every score can
# be explained by hand: a chunk ranks higher when it contains more of the question's
# words, when those words are rare across the corpus, and when the chunk is not
# padded with unrelated text. Known limits: exact word matching only, so no synonyms
# ("holiday" does not match "leave"), no stemming ("policy" does not match "policies"),
# and the stop-word list is English.
#
# The index is rebuilt only when the corpus snapshot version changes. Chunk statistics
# are cached per (path, content hash, chunk size), so an unchanged document is never
# re-chunked. Cache entries for documents no longer in the snapshot are dropped on each
# rebuild, so a deleted document cannot survive in the index (REQ-044).

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass

from app.chunking import Chunk, chunk_document
from app.corpus import Snapshot

# Standard BM25 parameters (Robertson/Lucene defaults). Not configurable: they are
# well-established, and changing them is a tuning exercise, not an operational setting.
K1 = 1.5
B = 0.75

_WORD = re.compile(r"\w+")

# Dropped from questions only, so "what is the leave policy" scores on "leave" and
# "policy" instead of matching every chunk containing "the". Documents are indexed in
# full, so chunk lengths stay true.
STOP_WORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have how i if in "
    "into is it its me my no not of on or our please should so tell than that the their them "
    "then there these they this to us was we were what when where which who why will with "
    "would you your".split()
)


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def query_terms(question: str) -> list[str]:
    """Distinct meaningful words of a question, in first-seen order."""
    seen: dict[str, None] = {}
    for word in tokenize(question):
        if word not in STOP_WORDS:
            seen.setdefault(word)
    return list(seen)


@dataclass(frozen=True)
class _ChunkStats:
    chunk: Chunk
    term_counts: Counter
    length: int


class Bm25Index:
    def __init__(self, stats: list[_ChunkStats]) -> None:
        self.chunks = [s.chunk for s in stats]
        self._lengths = [s.length for s in stats]
        self._avg_length = (sum(self._lengths) / len(stats)) if stats else 0.0
        # Inverted index: term -> [(chunk position, count in chunk)]. Scoring only
        # touches chunks that contain at least one question word.
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for position, s in enumerate(stats):
            for term, count in s.term_counts.items():
                self._postings.setdefault(term, []).append((position, count))

    def __len__(self) -> int:
        return len(self.chunks)

    def idf(self, term: str) -> float:
        # Lucene's variant: always positive, so a very common word adds little
        # instead of subtracting.
        n = len(self.chunks)
        df = len(self._postings.get(term, ()))
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, terms: list[str]) -> dict[int, float]:
        """BM25 score per chunk position, for chunks matching at least one term."""
        scores: dict[int, float] = {}
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for position, tf in postings:
                norm = K1 * (1 - B + B * self._lengths[position] / self._avg_length)
                scores[position] = scores.get(position, 0.0) + idf * tf * (K1 + 1) / (tf + norm)
        return scores


class IndexCache:
    """Holds the index for the latest snapshot version; rebuilds when the version changes."""

    def __init__(self, chunk_max_chars: int) -> None:
        self._chunk_max_chars = chunk_max_chars
        self._lock = threading.Lock()
        self._version: int | None = None
        self._index = Bm25Index([])
        self._stats_by_doc: dict[tuple[str, str], list[_ChunkStats]] = {}

    def get(self, snapshot: Snapshot) -> Bm25Index:
        with self._lock:
            if snapshot.version == self._version:
                return self._index
            fresh: dict[tuple[str, str], list[_ChunkStats]] = {}
            for doc in snapshot.documents:
                key = (doc.rel_path, doc.sha256)
                stats = self._stats_by_doc.get(key)
                if stats is None:
                    stats = []
                    for chunk in chunk_document(doc, self._chunk_max_chars):
                        words = tokenize(chunk.text)
                        stats.append(_ChunkStats(chunk, Counter(words), len(words)))
                fresh[key] = stats
            # Replacing the dict (not merging) is what evicts deleted documents.
            self._stats_by_doc = fresh
            self._index = Bm25Index([s for doc_stats in fresh.values() for s in doc_stats])
            self._version = snapshot.version
            return self._index
