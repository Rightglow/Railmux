"""Lightweight fuzzy matching for pane filtering.

No dependencies — the algorithm is character-sequential matching so it works
like fzf / fuzzyfinder: the needle's characters must appear in the haystack
in order, but not necessarily contiguously.  Multi-word needles split on
whitespace and every word must match independently (AND logic).
"""
from __future__ import annotations


def fuzzy_match(needle: str, haystack: str) -> bool:
    """Return True when every character of *needle* appears in *haystack*
    in order, case-insensitive.

    An empty needle matches everything (clear filter = show all).
    """
    if not needle:
        return True
    needle_lower = needle.lower()
    haystack_lower = haystack.lower()
    for word in needle_lower.split():
        pos = 0
        for ch in word:
            pos = haystack_lower.find(ch, pos)
            if pos == -1:
                return False
            pos += 1
    return True


def fuzzy_match_positions(terms: list[str] | tuple[str, ...], haystack: str) -> set[int]:
    """Return character indices in *haystack* matched by *terms*.

    Each term that matches *haystack* highlights its first contiguous
    case-insensitive occurrence when present, falling back to sequential
    fuzzy character positions.
    """
    if not terms or not haystack:
        return set()
    haystack_lower = haystack.lower()
    hits: set[int] = set()
    for raw_term in terms:
        word = raw_term.lower()
        if not word:
            continue
        exact = haystack_lower.find(word)
        if exact != -1:
            hits.update(range(exact, exact + len(word)))
            continue
        pos = 0
        matched: list[int] = []
        for ch in word:
            pos = haystack_lower.find(ch, pos)
            if pos == -1:
                matched = []
                break
            matched.append(pos)
            pos += 1
        hits.update(matched)
    return hits
