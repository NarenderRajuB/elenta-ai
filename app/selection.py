# Context selection boundary: chooses which chunks become evidence for one question,
# within an explicit token budget (REQ-050, REQ-053, REQ-055, REQ-071, REQ-075).
#
# Steps, all deterministic and recorded in the returned Selection:
#   1. Reduce the question to meaningful words (stop words removed).
#   2. Score chunks with BM25 and keep those scoring above min_score.
#   3. Rank by score (ties broken by chunk id, so results are reproducible).
#   4. Fill the budget in rank order. A chunk that does not fit is dropped and the
#      next one is tried, so a smaller lower-ranked chunk can still use the space.
#      Only if the very first chunk alone exceeds the budget is it truncated.
#      A chunk costs its text plus its framing in the prompt (label and delimiters),
#      given by the caller, so the budget holds for evidence as the model receives it:
#      many short chunks can't overflow the prompt through their labels (TS-018).
#
# If nothing qualifies, the Selection says why (empty corpus, no meaningful words,
# no relevant chunk) so the caller can answer "not enough evidence" without asking
# the model at all, which is a deterministic guard rather than trusting the model to admit it.
#
# Competing sources (ADR-019, REQ-054): code can't tell whether two documents disagree,
# but it can tell when two or more documents match the question about equally well.
# A file competes when its best selected chunk scores at least COMPETING_SCORE_RATIO of
# the top score. If two or more files compete, they are reported so the caller can tell
# the user to check them, without choosing between them (no precedence, ADR-009).

from collections.abc import Callable
from dataclasses import dataclass

from app.index import Bm25Index, query_terms
from app.tokens import estimate_tokens, max_chars_for_tokens

# How many dropped chunks to list individually; the full count is always recorded.
DROPPED_DETAIL_LIMIT = 10

# A file's best chunk must score at least this fraction of the top chunk's score for the
# file to count as competing. 0.8 flags near-ties (the TS-008 case scored identically)
# while ignoring files that only share a word or two with the question.
COMPETING_SCORE_RATIO = 0.8


@dataclass(frozen=True)
class SelectedChunk:
    chunk_id: str
    rel_path: str
    text: str
    score: float
    estimated_tokens: int  # text plus its framing in the prompt
    truncated: bool


# (chunk_id, rel_path, truncated) -> estimated tokens of a chunk's framing in the prompt.
FramingCost = Callable[[str, str, bool], int]


def _no_framing(chunk_id: str, rel_path: str, truncated: bool) -> int:
    return 0


@dataclass(frozen=True)
class DroppedChunk:
    chunk_id: str
    score: float
    estimated_tokens: int


@dataclass(frozen=True)
class Selection:
    chunks: tuple[SelectedChunk, ...]
    # None when evidence was found; otherwise one of: empty_corpus,
    # no_meaningful_terms, no_relevant_evidence, evidence_too_large (relevant chunks
    # exist, but not even the best one's framing fits the budget).
    insufficient_reason: str | None
    query_terms: tuple[str, ...]
    budget_tokens: int
    used_tokens: int
    candidates_considered: int
    dropped_count: int
    dropped_top: tuple[DroppedChunk, ...]
    # Files that match the question about equally well, in rank order; empty unless 2+.
    competing_files: tuple[str, ...] = ()

    @property
    def sufficient(self) -> bool:
        return self.insufficient_reason is None

    @property
    def truncated(self) -> bool:
        return any(c.truncated for c in self.chunks)

    @property
    def source_files(self) -> tuple[str, ...]:
        """Distinct source files in rank order: what the user is shown as sources."""
        return tuple(dict.fromkeys(c.rel_path for c in self.chunks))


def _empty(reason: str, terms: list[str], budget: int, considered: int = 0) -> Selection:
    return Selection((), reason, tuple(terms), budget, 0, considered, 0, ())


def select(
    index: Bm25Index, question: str, budget_tokens: int, min_score: float, framing_tokens: FramingCost = _no_framing
) -> Selection:
    terms = query_terms(question)
    if len(index) == 0:
        return _empty("empty_corpus", terms, budget_tokens)
    if not terms:
        return _empty("no_meaningful_terms", terms, budget_tokens)

    scores = index.score(terms)
    ranked = sorted(
        ((score, index.chunks[pos]) for pos, score in scores.items() if score > min_score),
        key=lambda item: (-item[0], item[1].chunk_id),
    )
    if not ranked:
        return _empty("no_relevant_evidence", terms, budget_tokens, considered=len(scores))

    chosen: list[SelectedChunk] = []
    dropped: list[DroppedChunk] = []
    used = 0
    for score, chunk in ranked:
        tokens = estimate_tokens(chunk.text) + framing_tokens(chunk.chunk_id, chunk.rel_path, False)
        if used + tokens <= budget_tokens:
            chosen.append(SelectedChunk(chunk.chunk_id, chunk.rel_path, chunk.text, round(score, 4), tokens, False))
            used += tokens
            continue
        # The best chunk alone is over budget: keep its beginning rather than sending
        # nothing, leaving room for its framing. Marked truncated so it is visible to
        # the user and trace.
        room = budget_tokens - framing_tokens(chunk.chunk_id, chunk.rel_path, True)
        if not chosen and room > 0:
            text = chunk.text[: max_chars_for_tokens(room)]
            cut_tokens = estimate_tokens(text) + framing_tokens(chunk.chunk_id, chunk.rel_path, True)
            chosen.append(SelectedChunk(chunk.chunk_id, chunk.rel_path, text, round(score, 4), cut_tokens, True))
            used += cut_tokens
        else:
            dropped.append(DroppedChunk(chunk.chunk_id, round(score, 4), tokens))

    return Selection(
        chunks=tuple(chosen),
        insufficient_reason=None if chosen else "evidence_too_large",
        query_terms=tuple(terms),
        budget_tokens=budget_tokens,
        used_tokens=used,
        candidates_considered=len(ranked),
        dropped_count=len(dropped),
        dropped_top=tuple(dropped[:DROPPED_DETAIL_LIMIT]),
        competing_files=competing_files(chosen),
    )


def competing_files(chunks: list[SelectedChunk]) -> tuple[str, ...]:
    """Files whose best chunk scores at least COMPETING_SCORE_RATIO of the top score.

    Returns them in rank order when there are two or more, otherwise an empty tuple.
    """
    best: dict[str, float] = {}
    for chunk in chunks:  # already in rank order, so the first chunk per file is its best
        best.setdefault(chunk.rel_path, chunk.score)
    if not best:
        return ()
    top = max(best.values())
    files = tuple(path for path, score in best.items() if score >= COMPETING_SCORE_RATIO * top)
    return files if len(files) >= 2 else ()
