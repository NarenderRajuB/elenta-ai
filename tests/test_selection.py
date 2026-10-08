# Tests for chunking (app/chunking.py), the BM25 index (app/index.py), evidence
# selection with a token budget (app/selection.py) and token estimation (app/tokens.py).
#
# Organised by requirement with positive / negative / edge cases; mirrored in README.md.

from pathlib import Path

import pytest

from app.chunking import chunk_document, chunk_text
from app.corpus import Corpus
from app.index import STOP_WORDS, Bm25Index, IndexCache, query_terms
from app.ingestion import Document, Fingerprint
from app.selection import DROPPED_DETAIL_LIMIT, select
from app.tokens import METHOD, estimate_tokens

FP = Fingerprint(1, 1, 1, 1)


def doc(rel_path: str, text: str) -> Document:
    return Document(rel_path=rel_path, text=text, fingerprint=FP, sha256="0" * 64)


def corpus_with(tmp_path: Path, files: dict[str, str]) -> Corpus:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Corpus(str(tmp_path), 10**8, 500, 0.0)


def index_for(tmp_path: Path, files: dict[str, str], chunk_chars: int = 800) -> Bm25Index:
    corpus = corpus_with(tmp_path, files)
    return IndexCache(chunk_chars).get(corpus.refresh())


LEAVE = "Employees receive 25 days of annual leave per year. Unused leave may be carried over."
EXPENSES = "Expense claims must be submitted within 30 days with receipts. Travel is reimbursed at economy rates."


# ---------------------------------------------------------------------------
# REQ-051: stable chunk identifiers that name the source
# ---------------------------------------------------------------------------


class TestReq051ChunkIds:
    def test_positive_id_contains_path_ordinal_and_content_hash(self):
        [chunk] = chunk_document(doc("policies/leave.md", "Some text."), 800)
        path, rest = chunk.chunk_id.split("#")
        ordinal, digest = rest.split(":")
        assert (path, ordinal, len(digest)) == ("policies/leave.md", "0", 8)

    def test_positive_same_content_gives_same_ids(self):
        a = chunk_document(doc("a.md", "Para one.\n\nPara two."), 10)
        b = chunk_document(doc("a.md", "Para one.\n\nPara two."), 10)
        assert [c.chunk_id for c in a] == [c.chunk_id for c in b]

    def test_negative_changed_content_changes_id(self):
        [before] = chunk_document(doc("a.md", "Leave is 25 days."), 800)
        [after] = chunk_document(doc("a.md", "Leave is 30 days."), 800)
        assert before.chunk_id != after.chunk_id
        assert before.chunk_id.split(":")[0] == after.chunk_id.split(":")[0] == "a.md#0"

    def test_edge_renamed_file_changes_id_path(self):
        [a] = chunk_document(doc("old.md", "same"), 800)
        [b] = chunk_document(doc("new.md", "same"), 800)
        assert a.chunk_id.startswith("old.md#") and b.chunk_id.startswith("new.md#")
        assert a.chunk_id.split(":")[1] == b.chunk_id.split(":")[1]


# ---------------------------------------------------------------------------
# REQ-050: deliberate evidence selection (chunking + BM25)
# ---------------------------------------------------------------------------


class TestReq050Chunking:
    def test_positive_short_paragraphs_packed_together(self):
        assert chunk_text("One.\n\nTwo.\n\nThree.", 800) == ["One.\n\nTwo.\n\nThree."]

    def test_positive_paragraphs_split_when_over_limit(self):
        assert chunk_text("aaaa\n\nbbbb\n\ncccc", 10) == ["aaaa\n\nbbbb", "cccc"]

    def test_positive_long_paragraph_cut_at_whitespace(self):
        chunks = chunk_text("alpha beta gamma delta", 11)
        assert chunks == ["alpha beta", "gamma delta"]
        assert all(len(c) <= 11 for c in chunks)

    def test_negative_no_chunk_exceeds_limit(self):
        text = ("word " * 500 + "\n\n") * 5
        assert all(len(c) <= 50 for c in chunk_text(text, 50))

    def test_edge_no_whitespace_hard_cut(self):
        assert chunk_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]

    def test_edge_windows_newlines_and_blank_runs(self):
        assert chunk_text("One.\r\n\r\n\r\n   \r\nTwo.", 4) == ["One.", "Two."]
        assert chunk_text("One.\rTwo.", 800) == ["One.\nTwo."]

    def test_edge_whitespace_only_text_has_no_chunks(self):
        assert chunk_text(" \n\n \t ", 800) == []

    def test_edge_all_text_preserved(self):
        text = "First para has words.\n\nSecond one too.\n\n" + "long " * 100
        rejoined = " ".join(" ".join(chunk_text(text, 60)).split())
        assert rejoined == " ".join(text.split())


class TestReq050Ranking:
    def test_positive_relevant_document_ranks_first(self, tmp_path):
        idx = index_for(tmp_path, {"leave.md": LEAVE, "expenses.txt": EXPENSES})
        sel = select(idx, "How many days of annual leave do I get?", 1500, 0.0)
        assert sel.chunks[0].rel_path == "leave.md"

    def test_positive_rare_words_outweigh_common_ones(self, tmp_path):
        # "days" appears in both files; "receipts" only in expenses.
        idx = index_for(tmp_path, {"leave.md": LEAVE, "expenses.txt": EXPENSES})
        assert select(idx, "days receipts", 1500, 0.0).chunks[0].rel_path == "expenses.txt"

    def test_positive_stop_words_removed_from_question(self):
        assert query_terms("What is the leave policy for the team?") == ["leave", "policy", "team"]

    def test_negative_question_word_absent_from_corpus_matches_nothing(self, tmp_path):
        idx = index_for(tmp_path, {"leave.md": LEAVE})
        assert select(idx, "capital France", 1500, 0.0).insufficient_reason == "no_relevant_evidence"

    def test_edge_case_insensitive_matching(self, tmp_path):
        idx = index_for(tmp_path, {"leave.md": LEAVE})
        assert select(idx, "ANNUAL LEAVE", 1500, 0.0).sufficient

    def test_edge_no_stemming_documented_limit(self, tmp_path):
        # Known limit (ADR-007): "reimbursement" does not match "reimbursed".
        idx = index_for(tmp_path, {"expenses.txt": EXPENSES})
        assert select(idx, "reimbursement", 1500, 0.0).insufficient_reason == "no_relevant_evidence"

    def test_edge_ties_broken_deterministically_by_chunk_id(self, tmp_path):
        idx = index_for(tmp_path, {"b.txt": "identical text", "a.txt": "identical text"})
        assert [c.rel_path for c in select(idx, "identical", 1500, 0.0).chunks] == ["a.txt", "b.txt"]

    def test_edge_min_score_filters_weak_matches(self, tmp_path):
        idx = index_for(tmp_path, {"leave.md": LEAVE, "expenses.txt": EXPENSES})
        weak = select(idx, "days", 1500, 0.0)
        strict = select(idx, "days", 1500, 100.0)
        assert weak.sufficient and strict.insufficient_reason == "no_relevant_evidence"

    def test_edge_duplicate_question_words_count_once(self):
        assert query_terms("leave leave LEAVE") == ["leave"]


# ---------------------------------------------------------------------------
# REQ-044 (index side): deleted or changed documents leave nothing in the index
# ---------------------------------------------------------------------------


class TestIndexFreshness:
    def test_positive_index_reuses_unchanged_version(self, tmp_path):
        corpus = corpus_with(tmp_path, {"a.txt": "alpha"})
        cache = IndexCache(800)
        snap = corpus.refresh()
        assert cache.get(snap) is cache.get(corpus.refresh())

    def test_negative_deleted_document_not_selectable(self, tmp_path):
        corpus = corpus_with(tmp_path, {"a.txt": "zebra facts", "b.txt": "other"})
        cache = IndexCache(800)
        assert select(cache.get(corpus.refresh()), "zebra", 1500, 0).sufficient
        (tmp_path / "a.txt").unlink()
        sel = select(cache.get(corpus.refresh()), "zebra", 1500, 0)
        assert sel.insufficient_reason == "no_relevant_evidence"

    def test_negative_modified_document_old_words_gone(self, tmp_path):
        corpus = corpus_with(tmp_path, {"a.txt": "old wording"})
        cache = IndexCache(800)
        cache.get(corpus.refresh())
        (tmp_path / "a.txt").write_text("new phrasing", encoding="utf-8")
        idx = cache.get(corpus.refresh())
        assert not select(idx, "wording", 1500, 0).sufficient
        assert select(idx, "phrasing", 1500, 0).sufficient

    def test_edge_internal_cache_evicts_deleted_documents(self, tmp_path):
        corpus = corpus_with(tmp_path, {"a.txt": "a", "b.txt": "b"})
        cache = IndexCache(800)
        cache.get(corpus.refresh())
        (tmp_path / "a.txt").unlink()
        cache.get(corpus.refresh())
        assert [path for path, _ in cache._stats_by_doc] == ["b.txt"]


# ---------------------------------------------------------------------------
# REQ-053 / REQ-075: insufficient evidence is detected before the model is asked
# ---------------------------------------------------------------------------


class TestReq053Insufficient:
    def test_positive_sufficient_when_relevant_chunk_exists(self, tmp_path):
        sel = select(index_for(tmp_path, {"leave.md": LEAVE}), "annual leave", 1500, 0.0)
        assert sel.sufficient and sel.insufficient_reason is None

    def test_negative_empty_corpus(self, tmp_path):
        sel = select(index_for(tmp_path, {}), "annual leave", 1500, 0.0)
        assert sel.insufficient_reason == "empty_corpus" and sel.chunks == ()

    def test_negative_question_with_only_stop_words(self, tmp_path):
        sel = select(index_for(tmp_path, {"leave.md": LEAVE}), "what is the", 1500, 0.0)
        assert sel.insufficient_reason == "no_meaningful_terms"

    def test_negative_unrelated_question(self, tmp_path):
        sel = select(index_for(tmp_path, {"leave.md": LEAVE}), "Who won the 1966 World Cup?", 1500, 0.0)
        assert sel.insufficient_reason == "no_relevant_evidence"

    @pytest.mark.parametrize("question", ["", "   ", "?!.,", "🙂🙂"])
    def test_edge_empty_or_symbol_only_question(self, tmp_path, question):
        sel = select(index_for(tmp_path, {"leave.md": LEAVE}), question, 1500, 0.0)
        assert sel.insufficient_reason == "no_meaningful_terms"

    def test_edge_corpus_of_only_skipped_files_is_empty(self, tmp_path):
        sel = select(index_for(tmp_path, {"notes.pdf": "annual leave"}), "annual leave", 1500, 0.0)
        assert sel.insufficient_reason == "empty_corpus"


# ---------------------------------------------------------------------------
# REQ-055 / REQ-071: explicit token budget; nothing overflows silently
# ---------------------------------------------------------------------------


class TestReq055Budget:
    @staticmethod
    def _many(tmp_path, n=20, size=400):
        return index_for(tmp_path, {f"doc{i:02}.txt": f"budget topic {i} " + "filler " * (size // 7) for i in range(n)})

    def test_positive_selected_evidence_within_budget(self, tmp_path):
        sel = select(self._many(tmp_path), "budget topic", 300, 0.0)
        assert sel.used_tokens <= 300
        assert sel.used_tokens == sum(c.estimated_tokens for c in sel.chunks)

    def test_positive_dropped_chunks_counted_and_listed(self, tmp_path):
        sel = select(self._many(tmp_path), "budget topic", 300, 0.0)
        assert sel.dropped_count > 0
        assert sel.dropped_count + len(sel.chunks) == sel.candidates_considered
        assert len(sel.dropped_top) == min(sel.dropped_count, DROPPED_DETAIL_LIMIT)

    def test_positive_everything_fits_nothing_dropped(self, tmp_path):
        sel = select(index_for(tmp_path, {"leave.md": LEAVE}), "leave", 1500, 0.0)
        assert sel.dropped_count == 0 and not sel.truncated

    def test_negative_oversized_corpus_never_exceeds_budget(self, tmp_path):
        # Corpus far larger than the budget (scenario 7 of the review).
        sel = select(self._many(tmp_path, n=60, size=780), "budget topic", 500, 0.0)
        assert sel.used_tokens <= 500 and sel.dropped_count > 0

    def test_negative_single_chunk_over_budget_is_truncated_and_marked(self, tmp_path):
        idx = index_for(tmp_path, {"big.txt": "needle " + "x" * 3000}, chunk_chars=4000)
        sel = select(idx, "needle", 100, 0.0)
        [chunk] = sel.chunks
        assert chunk.truncated and sel.truncated
        assert chunk.estimated_tokens <= 100 and sel.used_tokens <= 100

    def test_edge_smaller_lower_ranked_chunk_fills_remaining_space(self, tmp_path):
        # a and b match both words (b is slightly longer, so ranks just below a);
        # c matches one word and is tiny. Budget 170 fits a (153) but not a+b (311),
        # so b is dropped and c (3) still fits in the remaining space.
        idx = index_for(
            tmp_path,
            {
                "a.txt": "zebra apple " + "x " * 300,
                "b.txt": "zebra apple " + "y " * 310,
                "c.txt": "apple tiny",
            },
        )
        sel = select(idx, "zebra apple", 170, 0.0)
        assert [c.rel_path for c in sel.chunks] == ["a.txt", "c.txt"]
        assert [d.chunk_id.split("#")[0] for d in sel.dropped_top] == ["b.txt"]

    def test_edge_budget_of_one_token(self, tmp_path):
        sel = select(index_for(tmp_path, {"leave.md": LEAVE}), "leave", 1, 0.0)
        assert sel.used_tokens <= 1 and sel.chunks[0].truncated

    def test_edge_source_files_distinct_in_rank_order(self, tmp_path):
        idx = index_for(tmp_path, {"a.md": "kiwi one\n\nkiwi two", "b.md": "kiwi"}, chunk_chars=9)
        sel = select(idx, "kiwi", 1500, 0.0)
        assert len(sel.chunks) == 3
        assert sorted(sel.source_files) == ["a.md", "b.md"]
        # Order follows the first appearance of each file in the ranking.
        assert sel.source_files == tuple(dict.fromkeys(c.rel_path for c in sel.chunks))


# ---------------------------------------------------------------------------
# REQ-083 (estimation side): token counts are estimates with a documented method
# ---------------------------------------------------------------------------


class TestTokenEstimate:
    @pytest.mark.parametrize("text, expected", [("", 0), ("a", 1), ("abcd", 1), ("abcde", 2), ("x" * 400, 100)])
    def test_positive_ceil_chars_over_four(self, text, expected):
        assert estimate_tokens(text) == expected

    def test_edge_method_label_is_published(self):
        assert METHOD == "chars/4"

    def test_edge_stop_words_are_lowercase_single_words(self):
        assert all(w == w.lower() and " " not in w for w in STOP_WORDS)
