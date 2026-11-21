"""The services the provider sells. Swap these for a real model call."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were "
    "will with".split()
)


def summarize(text: str, sentences: int = 2) -> str:
    """Extractive summary: keep the sentences whose words are most frequent in the text."""
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if len(parts) <= sentences:
        return " ".join(parts)
    freq = Counter(w for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOPWORDS)

    def score(sentence: str) -> float:
        words = [w for w in re.findall(r"[a-z']+", sentence.lower()) if w not in _STOPWORDS]
        return sum(freq[w] for w in words) / (len(words) or 1)

    best = sorted(range(len(parts)), key=lambda i: score(parts[i]), reverse=True)[:sentences]
    return " ".join(parts[i] for i in sorted(best))


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    price: int  # in token base units (USDC has 6 decimals: 10_000 = 0.01 USDC)
    run: Callable[[dict], str]


def default_tools(price: int) -> dict[str, Tool]:
    return {
        "summarize": Tool(
            name="summarize",
            description="Summarize text by picking its most representative sentences.",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "sentences": {"type": "integer", "minimum": 1, "default": 2},
                },
                "required": ["text"],
            },
            price=price,
            run=lambda args: summarize(str(args["text"]), int(args.get("sentences", 2))),
        )
    }
