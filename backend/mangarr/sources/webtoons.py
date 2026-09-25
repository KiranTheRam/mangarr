"""WEBTOON (webtoons.com) source — official English Originals.

Search scrapes the site's result cards (Originals only; user-made Canvas
series are skipped). Episodes come from the mobile site's JSON API, which lists
exactly the episodes that are free to read right now: coin-locked Daily Pass
and Fast Pass episodes are absent, so nothing unreadable is ever offered.
Page images are the ``_images`` strips on each episode's viewer page, fetched
exactly as the official viewer shows them (full resolution; the uncompressed
originals are ~3.5x larger for no visible difference).

Episode numbering: most series title episodes "Episode 12" / "Ep. 12", and
those numbers are used as-is. Some restart per season ("[Season 3] Ep. 1"),
which would collide with earlier seasons, so those series fall back to
counting episodes in release order. Untitled extras between numbered
episodes ("Special", "Hiatus Notice") become decimal specials.
"""

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import DirectSource, SourceChapter, SourceSeries

SITE_URL = "https://www.webtoons.com"
MOBILE_URL = "https://m.webtoons.com"

_limiter = RateLimiter(rate=2, per_seconds=1)
_image_limiter = RateLimiter(rate=8, per_seconds=1)
_EPISODE_NUMBER = re.compile(r"\b(?:episode|ep|chapter|ch)\.?\s*(\d+(?:\.\d+)?)", re.I)
# "[Season 2] Ep. 5" / "(S2) Episode 12" — a number leading the title is the episode's own
_LEADING_EPISODE = re.compile(
    r"^\s*(?:[\[(][^\])]*[\])]\s*)?(?:episode|ep|chapter|ch)\.?\s*(\d+(?:\.\d+)?)", re.I
)
# extras sometimes carry their own counter ("Bonus Episode 2") that is not
# the series' episode number
_EXTRA = re.compile(
    r"\b(?:bonus|special|side\s*story|extra|recap|omake|notice|announcement|message)\b", re.I
)
_SEASON = re.compile(r"\bseason\s*(\d+)", re.I)
# "[Season 2] Ep. 5 - " / "Episode 12: " prefixes in front of the real title
_TITLE_PREFIX = re.compile(
    r"^\s*(?:[\[(][^\])]*[\])]\s*)?(?:episode|ep|chapter|ch)\.?\s*\d+(?:\.\d+)?\s*(?:[-:–—|]\s*)?",
    re.I,
)


def episode_number(title: str) -> float | None:
    title = title or ""
    if match := _LEADING_EPISODE.match(title):
        return float(match.group(1))
    if _EXTRA.search(title):
        return None
    match = _EPISODE_NUMBER.search(title)
    return float(match.group(1)) if match else None


def _next_special(previous: float, used: set[float]) -> float:
    """The next free decimal after ``previous`` that stays below the next
    whole episode (.1 … .9, then .91 … .99)."""
    whole = float(int(previous))
    steps = [whole + k / 10 for k in range(1, 10)] + [whole + 0.9 + k / 100 for k in range(1, 10)]
    for candidate in (round(step, 2) for step in steps):
        if candidate > previous and candidate not in used:
            return candidate
    return round(previous + 0.001, 3)


def _restarts(titles: list[str], parsed: list[float | None]) -> bool:
    """True when episode numbers go backwards, or repeat across a season change."""
    numbered = [
        (number, season.group(1) if (season := _SEASON.search(title)) else "")
        for title, number in zip(titles, parsed)
        if number is not None
    ]
    return any(
        later < earlier or (later == earlier and later_season != earlier_season)
        for (earlier, earlier_season), (later, later_season) in zip(numbered, numbered[1:])
    )


def assign_numbers(titles: list[str]) -> list[float]:
    """Chapter numbers for episodes given in release order (see module doc)."""
    parsed = [episode_number(title) for title in titles]
    known = [number for number in parsed if number is not None]
    if not known or _restarts(titles, parsed):
        # numbering restarts (or is absent): count in release order, anchored
        # so a series that opens with "Episode 0" keeps starting at 0
        first = next((i for i, number in enumerate(parsed) if number is not None), None)
        start = parsed[first] - first if first is not None else 1.0
        start = max(start, 0.0)
        return [start + i for i in range(len(titles))]

    numbers: list[float] = []
    used: set[float] = set()
    previous: float | None = None
    for number in parsed:
        if number is None and previous is None and known[0] >= 1 and 0.0 not in used:
            # an untitled opener ("Prologue") ahead of Episode 1 is episode 0
            number = 0.0
        elif number is None or number in used:
            # an extra (or a repeated number): a decimal after the last episode
            base = previous if previous is not None else max(known[0] - 1, 0.0)
            number = _next_special(base, used)
        numbers.append(number)
        used.add(number)
        previous = number
    return numbers


class WebtoonsSource(DirectSource):
    name = "webtoons"
    image_limiter = _image_limiter

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Referer": f"{SITE_URL}/"},
            timeout=60,
            trust_env=False,
            follow_redirects=True,
        )

    def image_headers(self) -> dict:
        # the image CDN answers 403 without a webtoons.com referer
        return {"Referer": f"{SITE_URL}/"}

    async def _request(self, url: str, **kwargs) -> httpx.Response:
        response = await rl_request(self._client, "GET", url, limiter=_limiter, **kwargs)
        response.raise_for_status()
        return response

    async def search_series(self, query: str) -> list[SourceSeries]:
        if not query.strip():
            return []
        response = await self._request(f"{SITE_URL}/en/search", params={"keyword": query})
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SourceSeries] = []
        seen: set[str] = set()
        for card in soup.select("a._card_item[data-title-no]"):
            href = card.get("href") or ""
            title_no = str(card.get("data-title-no") or "").strip()
            title_el = card.select_one(".title")
            if (
                card.get("data-webtoon-type") != "WEBTOON"
                or "/canvas/" in href
                or not title_no
                or title_no in seen
                or title_el is None
            ):
                continue
            seen.add(title_no)
            results.append(
                SourceSeries(
                    source_name=self.name,
                    external_id=title_no,
                    title=title_el.get_text(strip=True),
                    url=urljoin(SITE_URL, href),
                )
            )
        return results

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        response = await self._request(
            f"{MOBILE_URL}/api/v1/webtoon/{external_id}/episodes",
            params={"pageSize": 99999},
            headers={"Referer": f"{MOBILE_URL}/"},
        )
        episodes = [
            episode
            for episode in (response.json().get("result") or {}).get("episodeList") or []
            if isinstance(episode, dict) and episode.get("viewerLink")
        ]
        episodes.sort(key=lambda episode: episode.get("episodeNo") or 0)
        titles = [str(episode.get("episodeTitle") or "").strip() for episode in episodes]
        chapters = []
        for episode, title, number in zip(episodes, titles, assign_numbers(titles)):
            link = str(episode["viewerLink"])
            chapters.append(
                SourceChapter(
                    source_name=self.name,
                    # the viewer path is all get_pages needs
                    external_id=link,
                    number=number,
                    title=_TITLE_PREFIX.sub("", title).strip(),
                    url=urljoin(SITE_URL, link),
                )
            )
        return chapters

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        response = await self._request(urljoin(SITE_URL, chapter_external_id))
        soup = BeautifulSoup(response.text, "lxml")
        urls = []
        for image in soup.select("#_imageList img._images") or soup.select("img._images"):
            url = str(image.get("data-url") or "").strip()
            if url:
                urls.append(url)
        return urls

    # download_page inherited: rate-limited image fetch with back-off + Referer


source = WebtoonsSource()
