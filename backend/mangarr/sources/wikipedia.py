"""English Wikipedia chapter titles and printed-volume metadata.

Wikipedia's chapter-list articles ({{Graphic novel list}} templates) are the
most complete chapter→volume data available for licensed series — MangaDex's
aggregate loses chapters to license takedowns and MangaUpdates release tags
rarely carry volumes. This source serves no chapters or pages; it exists so
that data can compete in fetch_volume_map() like any other linked source.

Only explicitly numbered chapters are used ({{Numbered list|start=N}} items,
"* 12. …" bullets, "Chapters 1–8" prose). Volumes that list chapter titles
without numbers (e.g. Berserk's early volumes) contribute nothing — chapter
numbers are never inferred from list position.
"""

import re
import time

import httpx
import mwparserfromhell

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import ChapterMetadata, DirectSource, SourceChapter, SourceSeries

API_URL = "https://en.wikipedia.org/w/api.php"

# Wikimedia asks unauthenticated clients to stay around 1 req/s
_limiter = RateLimiter(rate=1, per_seconds=1)

# update_chapters() triggers fetch_volume_map() every monitor cycle for any
# ongoing series, so uncached fetches would hammer the API
VOLUME_MAP_CACHE_TTL = 24 * 3600.0

# columns beyond ChapterListCol2 are rare but the template allows them
_CHAPTER_PARAMS = ("ChapterList", "ChapterListCol1", "ChapterListCol2",
                   "ChapterListCol3", "ChapterListCol4")

# "* 12. {{Nihongo|…}}" / "# 12) …" — a list item whose explicit number leads
_LINE_NUMBER = re.compile(r"^\s*[*#:]+\s*(\d+(?:\.\d+)?)[.):]")
# "Chapters 17–25" / "Mission: 1–5" — the label word varies per series, so
# match the numeric span itself; lookarounds keep ISBN-like digit runs out
_PROSE_RANGE = re.compile(r"(?<![\d.–—-])(\d+)\s*[–—-]\s*(\d+)(?![\d.–—-])")
# "Chapter 26" singles (a bare number would match anything, so singles do
# need the label; the lookahead skips range openings — those matched above)
_PROSE_SINGLE = re.compile(r"chapters?\s+(\d+(?:\.\d+)?)(?!\s*[–—-])(?!\.?\d)",
                           re.IGNORECASE)
# a trailing disambiguator: "Berserk (manga)" → "Berserk"
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_EXTRA_LINE = re.compile(r"^\s*[*#:]+\s*(?:extra|bonus|omake)\s*[:.–—-]\s*(.+)$", re.I)


def _item_title(value: str, number: float | None = None) -> str:
    """Readable English title from a list item, including {{Nihongo}}."""
    code = mwparserfromhell.parse(value)
    for tpl in code.filter_templates(recursive=True):
        if tpl.name.strip().lower() == "nihongo":
            positional = [p for p in tpl.params if not p.showkey]
            if positional:
                text = positional[0].value.strip_code().strip()
                if text:
                    return text.strip(' "“”')
    text = " ".join(code.strip_code().replace("\n", " ").split())
    if number is not None:
        text = re.sub(
            rf"^\s*(?:chapter\s*)?{re.escape(str(number).removesuffix('.0'))}\s*[.):\-–—]?\s*",
            "", text, flags=re.I,
        )
    return text.strip(' "“”')


def _chapter_rows(value: str, volume: int) -> list[ChapterMetadata]:
    """Explicitly numbered rows plus explicitly labelled unnumbered extras."""
    rows: list[ChapterMetadata] = []
    code = mwparserfromhell.parse(value)
    for tpl in code.filter_templates(recursive=True):
        if tpl.name.strip().lower() != "numbered list":
            continue
        start = 1.0
        if tpl.has("start"):
            try:
                start = float(tpl.get("start").value.strip_code().strip())
            except ValueError:
                continue
        items = [p for p in tpl.params if not p.showkey and str(p.value).strip()]
        for index, item in enumerate(items):
            number = start + index
            rows.append(ChapterMetadata(
                source_name="wikipedia", number=number, volume=volume,
                title=_item_title(str(item.value), number),
            ))
    if rows:
        return rows
    for line in value.splitlines():
        match = _LINE_NUMBER.match(line)
        if match:
            number = float(match.group(1))
            rows.append(ChapterMetadata(
                source_name="wikipedia", number=number, volume=volume,
                title=_item_title(line[match.end():], number),
            ))
            continue
        extra = _EXTRA_LINE.match(line)
        if extra:
            rows.append(ChapterMetadata(
                source_name="wikipedia", number=None, volume=volume,
                title=_item_title(extra.group(1)), kind="extra",
            ))
    if rows:
        return rows
    # Prose ranges still provide useful volume mapping, but no invented title.
    return [ChapterMetadata(source_name="wikipedia", number=n, volume=volume)
            for n in _chapter_numbers(value)]


def _chapter_numbers(value: str) -> list[float]:
    """Explicit chapter numbers in a ChapterList cell — never positional."""
    numbers: list[float] = []
    code = mwparserfromhell.parse(value)
    for tpl in code.filter_templates(recursive=True):
        if tpl.name.strip().lower() != "numbered list":
            continue
        start = 1.0
        if tpl.has("start"):
            try:
                start = float(tpl.get("start").value.strip_code().strip())
            except ValueError:
                continue  # can't tell where the numbering starts — no data
        items = [p for p in tpl.params if not p.showkey and str(p.value).strip()]
        # explicit: the start number is stated and items are consecutive by
        # the template's own semantics
        numbers.extend(start + i for i in range(len(items)))
    if numbers:
        return numbers
    for line in value.splitlines():
        m = _LINE_NUMBER.match(line)
        if m:
            numbers.append(float(m.group(1)))
    if numbers:
        return numbers
    text = code.strip_code()
    for m in _PROSE_RANGE.finditer(text):
        low, high = int(m.group(1)), int(m.group(2))
        if high < low or high - low > 60:
            continue  # distrust absurd ranges
        numbers.extend(float(n) for n in range(low, high + 1))
    for m in _PROSE_SINGLE.finditer(text):
        numbers.append(float(m.group(1)))
    return numbers


def parse_volume_map(wikitext: str) -> dict[float, int]:
    """Chapter→volume assignments from {{Graphic novel list}} templates.
    The first occurrence of a chapter number wins (split list pages can
    overlap at their boundaries)."""
    return _rows_to_volume_map(parse_chapter_metadata(wikitext))


def _rows_to_volume_map(rows: list[ChapterMetadata]) -> dict[float, int]:
    mapping: dict[float, int] = {}
    for row in rows:
        if row.number is not None and row.volume is not None:
            mapping.setdefault(float(row.number), row.volume)
    return mapping


def parse_chapter_metadata(wikitext: str) -> list[ChapterMetadata]:
    rows: list[ChapterMetadata] = []
    seen: set[tuple[float | None, int, str]] = set()
    for tpl in mwparserfromhell.parse(wikitext).filter_templates():
        if tpl.name.strip().lower() != "graphic novel list" or not tpl.has("VolumeNumber"):
            continue
        vol_text = tpl.get("VolumeNumber").value.strip_code().strip()
        if not re.fullmatch(r"\d+", vol_text):
            continue  # "Omnibus 1" and friends — not a plain volume
        volume = int(vol_text)
        for param in _CHAPTER_PARAMS:
            if not tpl.has(param):
                continue
            for row in _chapter_rows(str(tpl.get(param).value), volume):
                key = (row.number, volume, row.title)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
    return rows


def _article_url(title: str) -> str:
    return f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"


class WikipediaSource(DirectSource):
    name = "wikipedia"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30, trust_env=False,
            follow_redirects=True,
        )
        self._metadata_cache: dict[str, tuple[float, list[ChapterMetadata]]] = {}

    async def _query(self, params: dict) -> dict:
        resp = await rl_request(
            self._client, "GET", API_URL, limiter=_limiter,
            params={**params, "format": "json", "formatversion": 2},
        )
        resp.raise_for_status()
        return resp.json()

    async def _search(self, text: str, limit: int) -> list[str]:
        data = await self._query({
            "action": "query", "list": "search",
            "srsearch": text, "srnamespace": 0, "srlimit": limit,
        })
        return [hit["title"] for hit in (data.get("query") or {}).get("search") or []]

    async def search_series(self, query: str) -> list[SourceSeries]:
        # bias toward manga articles ("Monster" alone finds anything but)
        titles = await self._search(f"{query} manga", 10)
        if not titles:
            titles = await self._search(query, 10)
        results = []
        for title in titles:
            plain = _PARENTHETICAL.sub("", title)
            results.append(SourceSeries(
                source_name=self.name,
                external_id=title,
                title=title,
                # the disambiguator would break normalized-title matching:
                # "Berserk (manga)" has to match a series called "Berserk"
                alt_titles=[plain] if plain and plain != title else [],
                url=_article_url(title),
            ))
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        return []  # Wikipedia serves no readable chapters

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        raise NotImplementedError("wikipedia is metadata-only")

    async def get_volume_map(self, external_id: str) -> dict[float, int]:
        # a cheap derivation of get_chapter_metadata, which handles caching
        return _rows_to_volume_map(await self.get_chapter_metadata(external_id))

    async def get_chapter_metadata(self, external_id: str) -> list[ChapterMetadata]:
        cached = self._metadata_cache.get(external_id)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
        rows: list[ChapterMetadata] = []
        for page in await self._find_list_pages(external_id):
            rows.extend(parse_chapter_metadata(await self._fetch_wikitext(page)))
        self._metadata_cache[external_id] = (
            time.monotonic() + VOLUME_MAP_CACHE_TTL, rows,
        )
        return list(rows)

    async def _find_list_pages(self, article_title: str) -> list[str]:
        """The chapter-list page(s) for a series article, discovered at fetch
        time: long series split their list across parts whose titles change as
        the series grows ("(1016–current)" → "(1016–1088)"), so only the
        article title is stored and parts are looked up fresh."""
        base = _PARENTHETICAL.sub("", article_title)
        hits = await self._search(f'intitle:"List of {base} chapters"', 50)
        wanted = re.compile(rf"list of {re.escape(base)} chapters(?: \(.+\))?$", re.IGNORECASE)
        parts = sorted(h for h in hits if wanted.fullmatch(h.lower()))
        # no dedicated list page → the volume tables sit in the article itself
        return parts or [article_title]

    async def _fetch_wikitext(self, title: str) -> str:
        data = await self._query({
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "redirects": 1, "titles": title,
        })
        for page in (data.get("query") or {}).get("pages") or []:
            for rev in page.get("revisions") or []:
                return ((rev.get("slots") or {}).get("main") or {}).get("content") or ""
        return ""


source = WikipediaSource()
