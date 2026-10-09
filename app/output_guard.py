# Output safety boundary: filters the model's streamed text before it reaches the client
# (ADR-006 C4 and C5; REQ-062, REQ-072). Model output is untrusted (brief §5.6).
#
#   C5 - reasoning removal: text inside known reasoning markers (<think>...</think>,
#        <thinking>...</thinking>, any letter case) is dropped, including when a marker is
#        split across stream chunks or never closed.
#   C4 - instruction-leak block: if the answer reproduces any LEAK_WINDOW consecutive
#        characters of the system prompt (compared lower-cased with whitespace collapsed),
#        the stream is stopped and the caller replaces it with a fixed refusal.
#
# Streaming stays progressive (REQ-031): text is released as it arrives. The only text
# held back is a tail that could still turn out to be the start of a leak (a tail that
# also occurs in the system prompt) or the start of a reasoning marker. Ordinary
# answer text shares only a few characters with the prompt, so it is released almost
# immediately, while a leak in progress is held until it reaches LEAK_WINDOW characters
# and is blocked. Known limits: only the listed markers are recognised, and C4
# detects verbatim or near-verbatim copying, not paraphrase.
#
#   C9 - unsupported approval (ApprovalGuard, ADR-021): a sentence that states something
#        "is / has been / was approved", or, when the question is about an approval, a
#        sentence that opens with "yes" / "approved" / "correct", stops the stream unless
#        the sentence is negated; the caller replaces the answer with a fixed reply. The
#        service never confirms an approval itself: approvals are decisions people make,
#        and a document's injected text can make a small model affirm one (TS-017).
#        It runs on the text the OutputGuard releases. Only the last unfinished word is
#        held back, plus a sentence that has started to affirm an approval, until it ends.
#        Known limits: English phrasing only; a document that genuinely records an
#        approval also gets the fixed reply (the sources still show it).

import re
from dataclasses import dataclass, field

# Lower-cased (open, close) pairs; matching is case-insensitive.
REASONING_MARKERS = (("<think>", "</think>"), ("<thinking>", "</thinking>"))

# 60 characters is roughly a full sentence of the instructions: long enough that
# ordinary answers do not trip it by coincidence, short enough to catch a quoted rule.
LEAK_WINDOW = 60

# Upper bound on the held-back tail, in raw characters. Twice the window allows for
# whitespace that normalisation collapses inside a padded leak.
MAX_HOLD_BACK = 2 * LEAK_WINDOW

REFUSAL = "I can't share that. I can only answer questions using the documents in the corpus."

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text.lower())


@dataclass
class GuardEvents:
    reasoning_removed: int = 0
    leak_blocked: bool = False


@dataclass
class OutputGuard:
    system_prompt: str
    # Parts of the system prompt that are not secret and may appear in an honest answer
    # (e.g. the rule saying what to reply when evidence is missing). A window lying
    # entirely inside one of them is not treated as a leak (TS-014).
    quotable: tuple[str, ...] = ()
    events: GuardEvents = field(default_factory=GuardEvents)
    _pending: str = ""  # received, not yet classified as answer or reasoning
    _answer: str = ""  # classified as answer, not all released yet
    _released: int = 0  # how many characters of _answer were released
    _in_reasoning: str | None = None  # closing marker we are waiting for
    _windows: frozenset = frozenset()
    _checked: int = 0  # leak windows starting before this index were already checked
    _prompt_norm: str = ""

    def __post_init__(self) -> None:
        norm = _normalise(self.system_prompt)
        self._prompt_norm = norm
        windows = {norm[i : i + LEAK_WINDOW] for i in range(len(norm) - LEAK_WINDOW + 1)}
        for text in self.quotable:
            q = _normalise(text)
            windows -= {q[i : i + LEAK_WINDOW] for i in range(len(q) - LEAK_WINDOW + 1)}
        self._windows = frozenset(windows)

    @property
    def blocked(self) -> bool:
        return self.events.leak_blocked

    def feed(self, delta: str) -> str:
        """Accept the next streamed piece; return the text that is safe to send now."""
        if self.blocked:
            return ""
        self._pending += delta
        self._classify(final=False)
        return self._release(final=False)

    def finish(self) -> str:
        """End of stream: release whatever is left (an unclosed reasoning block is dropped)."""
        if self.blocked:
            return ""
        self._classify(final=True)
        return self._release(final=True)

    def _classify(self, final: bool) -> None:
        while self._pending:
            lower = self._pending.lower()
            if self._in_reasoning is None:
                hits = [(lower.find(o), o, c) for o, c in REASONING_MARKERS if lower.find(o) != -1]
                if hits:
                    pos, opener, closer = min(hits)
                    self._answer += self._pending[:pos]
                    self._pending = self._pending[pos + len(opener) :]
                    self._in_reasoning = closer
                    self.events.reasoning_removed += 1
                    continue
                keep = 0 if final else self._partial_suffix(lower, [o for o, _ in REASONING_MARKERS])
                self._answer += self._pending[: len(self._pending) - keep]
                self._pending = self._pending[len(self._pending) - keep :]
                return
            pos = lower.find(self._in_reasoning)
            if pos != -1:
                self._pending = self._pending[pos + len(self._in_reasoning) :]
                self._in_reasoning = None
                continue
            # Still inside reasoning: discard, keeping only a possible partial closer.
            keep = 0 if final else self._partial_suffix(lower, [self._in_reasoning])
            self._pending = self._pending[len(self._pending) - keep :] if keep else ""
            return

    @staticmethod
    def _partial_suffix(lower: str, markers: list[str]) -> int:
        """Length of the longest tail of `lower` that could be the start of a marker."""
        best = 0
        for marker in markers:
            for n in range(min(len(marker) - 1, len(lower)), 0, -1):
                if marker.startswith(lower[-n:]):
                    best = max(best, n)
                    break
        return best

    def _release(self, final: bool) -> str:
        # Every window is checked exactly once, however large the incoming piece was.
        # Start one position early: a trailing space can merge with the next piece's
        # whitespace under normalisation, shifting the last window.
        norm = _normalise(self._answer)
        last_start = len(norm) - LEAK_WINDOW
        for i in range(max(0, self._checked - 1), last_start + 1):
            if norm[i : i + LEAK_WINDOW] in self._windows:
                self.events.leak_blocked = True
                return ""
        self._checked = max(self._checked, last_start + 1)
        upto = len(self._answer) if final else max(self._released, len(self._answer) - self._hold_back())
        out = self._answer[self._released : upto]
        self._released = upto
        return out

    def _hold_back(self) -> int:
        """Length of the longest unreleased tail that also occurs in the system prompt."""
        unreleased = len(self._answer) - self._released
        for k in range(min(unreleased, MAX_HOLD_BACK), 0, -1):
            tail = _normalise(self._answer[-k:])
            # A tail starting with whitespace normalises to " ..."; compare it stripped
            # on the left too, so a leak beginning right after a space is still held.
            if tail in self._prompt_norm or (tail.lstrip() in self._prompt_norm and tail.strip()):
                return k
        return 0


# --- C9: unsupported approval (ADR-021) -------------------------------------------------

APPROVAL_REFUSAL = (
    "The documents don't confirm an approval, and I can't grant or confirm one. "
    "Please check the sources listed and the approval process they describe."
)

# Questions that ask about an approval or authorisation (English stems).
_APPROVAL_TOPIC = re.compile(r"\b(approv|authori[sz]|sign(ed)?[ -]?off)", re.I)
# "X is approved", "has been approved", ... anywhere in a sentence.
_STATES_APPROVED = re.compile(
    r"\b(is|are|has been|have been|was|were|been|gets?|got)\s+(now\s+|automatically\s+|fully\s+)?approved\b", re.I
)
# A sentence opening with an affirmation answers "is it approved?" with yes.
_AFFIRMATIVE_OPENING = re.compile(r"^[\W_]*(yes|yeah|yep|correct|approved|confirmed)\b", re.I)
_NEGATION = re.compile(r"\b(not|no|never|cannot)\b|n't\b", re.I)
# "If / once / when ... is approved" describes a condition, not a decision.
_CONDITIONAL = re.compile(r"\b(if|once|when|whether|until|unless|after|before)\b", re.I)
_SENTENCE_END = re.compile(r"[.!?\n]")


@dataclass
class ApprovalGuard:
    """Stops an answer that affirms an approval the service can't vouch for (C9)."""

    question: str
    blocked: bool = False
    _sentence: str = ""  # released part of the current sentence
    _held: str = ""  # received, not yet released

    def __post_init__(self) -> None:
        self._about_approval = bool(_APPROVAL_TOPIC.search(self.question))

    def _affirms(self, sentence: str) -> bool:
        for match in _STATES_APPROVED.finditer(sentence):
            if not _CONDITIONAL.search(sentence[: match.start()]):
                return True
        return self._about_approval and bool(_AFFIRMATIVE_OPENING.search(sentence))

    def _unsafe(self, sentence: str) -> bool:
        return self._affirms(sentence) and not _NEGATION.search(sentence)

    def feed(self, text: str) -> str:
        """Accept released answer text; return what is safe to send now."""
        if self.blocked:
            return ""
        self._held += text
        out = ""
        # Complete sentences are judged whole.
        while (match := _SENTENCE_END.search(self._held)) is not None:
            part = self._held[: match.end()]
            if self._unsafe(self._sentence + part):
                self.blocked = True
                return out
            out += part
            self._held = self._held[match.end() :]
            self._sentence = ""
        # Unfinished sentence: once it affirms, hold it until it ends (a negation may
        # still follow). Otherwise release all but the last unfinished word, which
        # could still become "yes" or "approved".
        if not self._affirms(self._sentence + self._held):
            cut = max(self._held.rfind(" "), self._held.rfind("\t")) + 1
            out += self._held[:cut]
            self._sentence += self._held[:cut]
            self._held = self._held[cut:]
        return out

    def finish(self) -> str:
        """End of answer: judge the last sentence and release it if safe."""
        if self.blocked:
            return ""
        if self._unsafe(self._sentence + self._held):
            self.blocked = True
            return ""
        out, self._held = self._held, ""
        return out
