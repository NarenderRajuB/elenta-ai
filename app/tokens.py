# Token estimation shared by selection (evidence budget) and prompt assembly (context check).
#
# The app cannot use the model's real tokenizer without depending on a specific model
# (ADR-007, REQ-022), so it estimates. Every count produced here is reported as
# "estimated"; counts the model endpoint returns are reported as "reported" (REQ-083).
#
# Method: ceil(characters / 4). Roughly right for English with the Qwen/Llama family
# of tokenizers; it undercounts for scripts like CJK, where one character is often one
# or more tokens. The 1500-token budget inside a 4096-token context leaves margin for that.

import math

CHARS_PER_TOKEN = 4
METHOD = "chars/4"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def max_chars_for_tokens(tokens: int) -> int:
    """Inverse of estimate_tokens: the longest text that still fits in `tokens`."""
    return tokens * CHARS_PER_TOKEN
