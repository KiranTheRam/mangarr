"""Title helpers shared by metadata display and source searches."""

from __future__ import annotations

import re

from .util import normalize_title

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_ENGLISH_WORDS = {
    "a",
    "about",
    "after",
    "again",
    "all",
    "and",
    "at",
    "beyond",
    "boy",
    "boys",
    "by",
    "can",
    "day",
    "days",
    "do",
    "does",
    "don't",
    "end",
    "family",
    "for",
    "from",
    "girl",
    "girls",
    "has",
    "have",
    "her",
    "him",
    "his",
    "how",
    "in",
    "is",
    "journey",
    "king",
    "life",
    "love",
    "man",
    "me",
    "miss",
    "mr",
    "my",
    "not",
    "of",
    "on",
    "one",
    "our",
    "queen",
    "s",
    "school",
    "story",
    "that",
    "the",
    "this",
    "to",
    "tomorrow",
    "toy",
    "with",
    "world",
    "you",
    "your",
}
_ROMAJI_HINTS = (
    "-chan",
    "-kun",
    "-sama",
    "-san",
    " chan",
    " kun",
    " sama",
    " san",
)


def split_alt_titles(raw: str | None) -> list[str]:
    return [title.strip() for title in (raw or "").split("\n") if title.strip()]


def unique_titles(titles: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for title in titles:
        title = title.strip()
        key = normalize_title(title) or title.casefold()
        if not title or key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out


def _query_match_score(candidate: str, query: str | None) -> int:
    nq = normalize_title(query or "")
    nc = normalize_title(candidate)
    if not nq or not nc:
        return 0
    if nc == nq:
        return 100
    if len(nq) >= 5 and (nq in nc or nc in nq):
        return 70
    query_words = set(nq.split())
    cand_words = set(nc.split())
    if query_words and query_words <= cand_words:
        return 50
    return 0


def _english_score(title: str) -> int:
    if not _LATIN_RE.search(title) or _CJK_RE.search(title):
        return -20
    letters = [ch for ch in title if ch.isalpha()]
    ascii_letters = [ch for ch in letters if ch.isascii()]
    ascii_ratio = len(ascii_letters) / max(1, len(letters))
    if ascii_ratio < 0.85:
        return -10

    normalized = normalize_title(title)
    words = set(normalized.split())
    score = 0
    score += int(ascii_ratio * 10)
    score += min(18, 3 * len(words & _ENGLISH_WORDS))
    if " " in title:
        score += 2
    if "'" in title or ":" in title:
        score += 3
    lower = title.lower()
    if any(hint in lower for hint in _ROMAJI_HINTS):
        score -= 8
    return score


def english_title(primary: str, alt_titles: list[str], query: str | None = None) -> str:
    """Best effort English display title.

    MangaUpdates often returns the canonical Japanese/romaji title while the
    user's English search lands in associated titles. Prefer the title that
    matched the user's query, then fall back to likely English aliases.
    """

    primary_key = normalize_title(primary) or primary.casefold()
    candidates = [
        title
        for title in unique_titles(alt_titles)
        if (normalize_title(title) or title.casefold()) != primary_key
    ]
    if not candidates:
        return ""

    if query:
        by_query = sorted(
            ((title, _query_match_score(title, query)) for title in candidates),
            key=lambda item: item[1],
            reverse=True,
        )
        if by_query and by_query[0][1] >= 50:
            return by_query[0][0]

    scored = sorted(
        ((title, _english_score(title)) for title in candidates),
        key=lambda item: item[1],
        reverse=True,
    )
    if scored and scored[0][1] >= 14:
        return scored[0][0]
    if scored and scored[0][1] >= 12 and len(normalize_title(scored[0][0]).split()) <= 2:
        return scored[0][0]
    return ""


def title_queries(primary: str, alt_titles: list[str], limit: int = 6) -> list[str]:
    """Search queries ordered by value: canonical title, likely English title,
    then other alternates. Raw CJK titles are kept; callers that need loose
    matching can still skip empty normalized forms."""

    english = english_title(primary, alt_titles)
    ordered = [primary]
    if english:
        ordered.append(english)
    ordered.extend(alt_titles)
    return unique_titles(ordered)[:limit]
