"""MANGA Plus by SHUEISHA — the official free same-day source for Shonen
Jump titles (One Piece, Kagurabachi, Dandadan, Jujutsu Kaisen…).

Uses the public web API's protobuf responses. Page images are XOR-encrypted
with a per-image hex key returned alongside each page; the key is smuggled
through the page URL fragment and applied in download_page.

Notes:
- Only the first few and latest few chapters of each title are free; older
  chapters return an error from the viewer endpoint (the grab then fails,
  which is fine — this source exists for brand-new chapters).
- The API bans datacenter IPs. It must run from a residential IP (e.g. the
  home server); it will not work from most cloud hosts.
"""

import secrets
import time
from urllib.parse import parse_qs, urlencode

import httpx

from ..util import RateLimiter, normalize_title, parse_chapter_number, rl_request
from ._mangaplus_proto import ProtobufDecodeError, decode_response
from .base import DirectSource, SourceChapter, SourceSeries

API_URL = "https://jumpg-webapi.tokyo-cdn.com/api"
KEY_FRAGMENT = "#mangarr_key="
VIEWER_TOKEN_HEADER = "Plus-Vw-Token"
# language codes in the title list; English is 0 or unset
_ENGLISH = (0, None)

_limiter = RateLimiter(rate=2, per_seconds=1)
_image_limiter = RateLimiter(rate=5, per_seconds=1)
_CATALOG_TTL = 3600


class MangaPlusError(RuntimeError):
    pass


def _xor_decrypt(data: bytes, hex_key: str) -> bytes:
    key = bytes.fromhex(hex_key)
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


class MangaPlusSource(DirectSource):
    name = "mangaplus"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # The public web app supplies an opaque per-browser session identifier.
        self._session_token = secrets.token_hex(8)
        self._client = client or httpx.AsyncClient(
            headers={
                "Accept": "application/x-protobuf",
                "User-Agent": "Mangarr/1.0",
                "Session-Token": self._session_token,
            },
            timeout=60,
            trust_env=False,
            follow_redirects=True,
        )
        self._catalog: list[SourceSeries] = []
        self._catalog_at = 0.0

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await rl_request(
            self._client, "GET", f"{API_URL}{path}", limiter=_limiter,
            params=params, headers={"Session-Token": self._session_token},
        )
        resp.raise_for_status()
        try:
            body = decode_response(resp.content)
        except ProtobufDecodeError as exc:
            raise MangaPlusError("Invalid MangaPlus API response") from exc
        if "error" in body:
            raise MangaPlusError(body["error"] or "MangaPlus API error")
        return body.get("success") or {}

    async def _load_catalog(self) -> list[SourceSeries]:
        if self._catalog and time.time() - self._catalog_at < _CATALOG_TTL:
            return self._catalog
        data = await self._get("/title_list/allV2")
        view = data.get("allTitlesViewV2") or {}
        catalog: list[SourceSeries] = []
        seen: set[str] = set()
        for group in view.get("AllTitlesGroup") or []:
            for title in group.get("titles") or []:
                if title.get("language") not in _ENGLISH:
                    continue
                title_id = title.get("titleId")
                name = title.get("name")
                if title_id is None or not name or str(title_id) in seen:
                    continue
                seen.add(str(title_id))
                catalog.append(
                    SourceSeries(
                        source_name=self.name,
                        external_id=str(title_id),
                        title=name,
                        url=f"https://mangaplus.shueisha.co.jp/titles/{title_id}",
                    )
                )
        self._catalog = catalog
        self._catalog_at = time.time()
        return catalog

    async def search_series(self, query: str) -> list[SourceSeries]:
        nq = normalize_title(query)
        if not nq:
            return []
        catalog = await self._load_catalog()
        scored: list[tuple[int, SourceSeries]] = []
        for series in catalog:
            nt = normalize_title(series.title)
            if nt == nq:
                score = 3
            elif nt.startswith(nq) or nq.startswith(nt):
                score = 2
            elif nq in nt or nt in nq:
                score = 1
            else:
                continue
            scored.append((score, series))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [s for _, s in scored]

    @staticmethod
    def _chapters_of(group: dict) -> list[dict]:
        out = []
        for key in ("firstChapterList", "midChapterList", "lastChapterList"):
            out.extend(group.get(key) or [])
        return out

    async def list_chapters(self, external_id: str) -> list[SourceChapter]:
        data = await self._get("/title_detailV3", params={"title_id": external_id})
        view = data.get("titleDetailView") or {}
        chapters: dict[float, SourceChapter] = {}
        for group in view.get("chapterListGroup") or []:
            for ch in self._chapters_of(group):
                chapter_id = ch.get("chapterId")
                if chapter_id is None:
                    continue
                # name is like "#12"; "ex"/one-shots have no number → 0
                raw = (ch.get("name") or "").lstrip("#")
                number = parse_chapter_number(raw)
                if number is None:
                    number = 0.0
                if number not in chapters:
                    chapters[number] = SourceChapter(
                        source_name=self.name,
                        external_id=str(chapter_id),
                        number=number,
                        title=ch.get("subTitle") or "",
                        url=f"https://mangaplus.shueisha.co.jp/viewer/{chapter_id}",
                    )
        return sorted(chapters.values(), key=lambda c: c.number)

    async def get_pages(self, chapter_external_id: str) -> list[str]:
        data = await self._get(
            "/manga_viewer_v3",
            params={
                "chapter_id": chapter_external_id,
                "split": "yes",
                "img_quality": "high",
            },
        )
        viewer = data.get("mangaViewer") or {}
        viewer_token = viewer.get("vwToken") or ""
        urls = []
        for page in viewer.get("pages") or []:
            manga_page = page.get("mangaPage") if isinstance(page, dict) else None
            if not manga_page:
                continue  # banners / ads have no mangaPage
            image_url = manga_page.get("imageUrl")
            if not image_url:
                continue
            key = manga_page.get("encryptionKey")
            fragment = {}
            if key:
                fragment["mangarr_key"] = key
            if viewer_token:
                fragment["mangarr_vw_token"] = viewer_token
            urls.append(f"{image_url}#{urlencode(fragment)}" if fragment else image_url)
        return urls

    async def download_page(self, client: httpx.AsyncClient, url: str) -> bytes:
        # the per-page XOR key is smuggled in the URL fragment; strip it, fetch
        # (rate-limited + back-off), then decrypt
        real_url, _, raw_fragment = url.partition("#")
        fragment = parse_qs(raw_fragment)
        key = (fragment.get("mangarr_key") or [""])[0]
        viewer_token = (fragment.get("mangarr_vw_token") or [""])[0]
        headers = {VIEWER_TOKEN_HEADER: viewer_token} if viewer_token else None
        resp = await rl_request(
            client, "GET", real_url, limiter=_image_limiter, headers=headers
        )
        resp.raise_for_status()
        return _xor_decrypt(resp.content, key) if key else resp.content


source = MangaPlusSource()
