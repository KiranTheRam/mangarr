"""FanFox's public mobile catalog and HTML reader."""

import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .. import USER_AGENT
from ..util import RateLimiter, normalize_title, rl_request
from .base import DirectSource, SourceChapter, SourceSeries
from .routing import source_client

BASE_URL = "https://m.fanfox.net"
SLUG = r"[a-z0-9_][a-z0-9_-]{0,199}"
SERIES_PATH = re.compile(rf"/manga/(?P<slug>{SLUG})/?")
CHAPTER_ID = re.compile(
    rf"(?P<slug>{SLUG})/(?:(?P<volume>v[0-9]{{1,4}}|vTBE|vTBD)/)?"
    r"c(?P<number>[0-9]{1,7}(?:\.[0-9]{1,7})?)"
)
CHAPTER_LABEL = re.compile(r"Ch\s+([0-9]+(?:\.[0-9]+)?)(?:\s+(.*))?")
READER_IDS = re.compile(r"\bvar\s+(chapter_id|series_page)\s*=\s*([0-9]+)\s*;")
MAX_PAGES = 300

_limiter = RateLimiter(rate=2)
_image_limiter = RateLimiter(rate=3)


def _site_path(href: str) -> str:
    try:
        url = urlsplit(urljoin(BASE_URL, href.strip()))
    except ValueError:
        return ""
    if url.scheme == "https" and url.netloc == "m.fanfox.net" and not url.query and not url.fragment:
        return url.path
    return ""


def _cdn_url(src: str, host: str) -> str:
    try:
        url = urlsplit(urljoin(BASE_URL, src.strip()))
    except ValueError:
        return ""
    if url.scheme == "https" and url.netloc == host and url.path.startswith("/store/manga/"):
        return url.geturl()
    return ""


class FanFoxSource(DirectSource):
    name = "fanfox"
    image_limiter = _image_limiter

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def image_headers(self) -> dict:
        # The browser sends the mobile reader origin to the cross-origin CDN.
        return {"Referer": f"{BASE_URL}/"}

    async def _get_html(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> BeautifulSoup:
        resp = await rl_request(
            client, "GET", f"{BASE_URL}{path}", limiter=_limiter,
            params=params, follow_redirects=False,
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")

    def _client_context(self):
        return source_client(
            self.name, {"User-Agent": USER_AGENT, "Accept": "text/html"}, self._client,
        )

    async def search_series(self, query: str) -> list[SourceSeries]:
        if not query.strip():
            return []
        async with self._client_context() as client:
            soup = await self._get_html(client, "/search", {"k": query})
        if not soup.select_one(".search-main"):
            raise ValueError("FanFox search returned an unexpected page")

        results: dict[str, SourceSeries] = {}
        for link in soup.select(".search-main .post-list .post-one > a[href]"):
            match = SERIES_PATH.fullmatch(_site_path(link["href"]))
            if not match:
                continue
            slug = match["slug"]
            cover = link.select_one(".cover img")
            label = link.select_one(".cover-info .title")
            title = (cover.get("title", "").strip() if cover else "") or (
                label.get_text(" ", strip=True) if label else ""
            )
            if title and slug not in results:
                results[slug] = SourceSeries(
                    self.name, slug, title, f"{BASE_URL}/manga/{slug}/",
                    cover_url=_cdn_url(cover.get("src", ""), "fmcdn.mfcdn.net") if cover else "",
                )
        return list(results.values())

    async def _series_html(self, external_id: str) -> BeautifulSoup:
        if not re.fullmatch(SLUG, external_id):
            raise ValueError("Invalid FanFox series ID")
        async with self._client_context() as client:
            return await self._get_html(client, f"/manga/{external_id}/")

    async def get_series_details(self, external_id: str) -> SourceSeries | None:
        soup = await self._series_html(external_id)
        title = soup.select_one(".manga-detail-top .title")
        if title is None:
            return None
        cover = soup.select_one(".manga-detail-top img")
        summary = soup.select_one(".manga-summary")
        return SourceSeries(
            self.name, external_id, title.get_text(" ", strip=True),
            f"{BASE_URL}/manga/{external_id}/",
            cover_url=_cdn_url(cover.get("src", ""), "fmcdn.mfcdn.net") if cover else "",
            description=summary.get_text(" ", strip=True) if summary else "",
        )

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        soup = await self._series_html(external_id)
        if not soup.select_one(".manga-detail"):
            raise ValueError("FanFox series returned an unexpected page")

        rows: dict[float, list[SourceChapter]] = {}
        seen: set[str] = set()
        for link in soup.select(".manga-chapters .chlist a[href]"):
            path = _site_path(link["href"])
            if not path.startswith("/manga/") or not path.endswith("/1.html"):
                continue
            chapter_id = path.removeprefix("/manga/").removesuffix("/1.html")
            match = CHAPTER_ID.fullmatch(chapter_id)
            if not match or match["slug"] != external_id or chapter_id in seen:
                continue
            # The date is in a child span. Use only the label's own text.
            label = " ".join(" ".join(link.find_all(string=True, recursive=False)).split())
            chapter = CHAPTER_LABEL.fullmatch(label)
            number = float(match["number"])
            if not chapter or float(chapter[1]) != number:
                continue
            seen.add(chapter_id)
            volume_tag = (match["volume"] or "")[1:]
            volume = int(volume_tag) if volume_tag.isdigit() else None
            rows.setdefault(number, []).append(SourceChapter(
                self.name, chapter_id, number, volume, chapter[2] or "", url=f"{BASE_URL}{path}",
            ))

        chapters = []
        for _, copies in sorted(rows.items()):
            volumes = {c.volume for c in copies if c.volume is not None}
            titles = {normalize_title(c.title) for c in copies if c.title}
            # Some series restart numbering in another volume or have a pilot
            # labeled Ch 1. Mangarr keys its chapters by number across volumes.
            if len(volumes) > 1 or len(titles) > 1:
                continue
            chapters.append(max(copies, key=lambda c: (c.volume is not None, bool(c.title))))
        return chapters

    @staticmethod
    def _reader_page(soup: BeautifulSoup, page: int) -> tuple[str, str]:
        img = soup.select_one("#viewer img#image")
        image_url = _cdn_url(img.get("src", ""), "zjcdn.mangafox.me") if img else ""
        if not image_url:
            raise ValueError(f"FanFox reader page {page} is unavailable or has no supported image")
        ids = dict(
            pair for script in soup.find_all("script")
            for pair in READER_IDS.findall(script.string or "")
        )
        if ids.get("series_page") != str(page) or not ids.get("chapter_id"):
            raise ValueError(f"FanFox reader returned the wrong page for page {page}")
        return ids["chapter_id"], image_url

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        if not CHAPTER_ID.fullmatch(chapter_external_id):
            raise ValueError("Invalid FanFox chapter ID")
        root = f"/manga/{chapter_external_id}/"
        async with self._client_context() as client:
            first = await self._get_html(client, f"{root}1.html")
            chapter_id, image = self._reader_page(first, 1)
            manifest = first.select("select.mangaread-page option")
            paths = [_site_path(option.get("value", "")) for option in manifest]
            if (
                not 1 <= len(paths) <= MAX_PAGES
                or any(path != f"{root}{i}.html" for i, path in enumerate(paths, 1))
            ):
                raise ValueError("FanFox reader returned an invalid page list")

            images = [image]
            for page, path in enumerate(paths[1:], 2):
                soup = await self._get_html(client, path)
                actual_id, image = self._reader_page(soup, page)
                if actual_id != chapter_id:
                    raise ValueError(f"FanFox reader returned a different chapter on page {page}")
                images.append(image)
            return images


source = FanFoxSource()
