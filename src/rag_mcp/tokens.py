"""Token counting with the embedding model's own tokenizer.

Chunk sizes are measured in the same tokens the model sees, so the 512-token
limit is a guarantee rather than a character-count estimate.
"""

from __future__ import annotations


class TokenCounter:
    def __init__(self, tokenizer) -> None:
        self._tokenizer = tokenizer

    @classmethod
    def from_pretrained(cls, model_name: str) -> TokenCounter:
        from transformers import AutoTokenizer  # heavy import, only when needed

        return cls(AutoTokenizer.from_pretrained(model_name))

    def count(self, text: str) -> int:
        """Tokens excluding the special [CLS]/[SEP] tokens."""
        return len(self._encode(text)["input_ids"])

    def split(self, text: str, max_tokens: int) -> list[str]:
        """Cut text into pieces of at most max_tokens, on token boundaries.

        Slices the original string via character offsets (decoding would
        lowercase it: bge's tokenizer is uncased).
        """
        offsets = self._encode(text, offsets=True)["offset_mapping"]
        starts = [offsets[i][0] for i in range(0, len(offsets), max_tokens)]
        bounds = starts[1:] + [len(text)]
        return [p for p in (text[s:e].strip() for s, e in zip(starts, bounds)) if p]

    def tail(self, text: str, max_tokens: int) -> str:
        """The end of text, at most max_tokens long, starting at a word boundary."""
        offsets = self._encode(text, offsets=True)["offset_mapping"]
        if len(offsets) <= max_tokens:
            return text.strip()
        start = offsets[-max_tokens][0]
        while start < len(text) and not text[start - 1].isspace():  # don't start mid-word
            start += 1
        return text[start:].strip()

    def _encode(self, text: str, offsets: bool = False):
        return self._tokenizer(
            text,
            add_special_tokens=False,
            return_offsets_mapping=offsets,
            truncation=False,
            verbose=False,  # no "sequence longer than 512" warnings: we split ourselves
        )
