"""Shared lexical normalization for deterministic retrieval and reranking."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import cast

import snowballstemmer  # type: ignore[import-untyped]

_TOKEN = re.compile(r"[\w]{2,}", flags=re.UNICODE)
_STEMMER = snowballstemmer.stemmer("english")


def raw_terms(text: str, *, limit: int = 64) -> list[str]:
    return list(dict.fromkeys(_TOKEN.findall(text.casefold())))[:limit]


def normalized_terms(text: str, *, limit: int = 64) -> list[str]:
    return list(dict.fromkeys(_normalize_term(term) for term in raw_terms(text, limit=limit)))


@lru_cache(maxsize=131_072)
def _normalize_term(term: str) -> str:
    if not term.isalpha():
        return term
    return cast(str, _STEMMER.stemWord(term))
