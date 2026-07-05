"""Nyaa.si torrent indexer via its RSS feed (Literature - English-translated)."""

import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import TorrentIndexer, TorrentRelease

BASE_URL = "https://nyaa.si"
NYAA_NS = "https://nyaa.si/xmlns/nyaa"

_limiter = RateLimiter(rate=1, per_seconds=2)

_SIZE_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def parse_size(text: str) -> int:
    parts = text.strip().split()
    if len(parts) != 2:
        return 0
    try:
        value = float(parts[0])
    except ValueError:
        return 0
    return int(value * _SIZE_UNITS.get(parts[1].upper(), 1))


def parse_rss(xml_text: str) -> list[TorrentRelease]:
    root = ET.fromstring(xml_text)
    releases = []
    for item in root.iter("item"):
        def text(tag: str, ns: str | None = None) -> str:
            el = item.find(f"{{{ns}}}{tag}" if ns else tag)
            return el.text or "" if el is not None else ""

        info_hash = text("infoHash", NYAA_NS)
        title = text("title")
        if not info_hash or not title:
            continue
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}"
        releases.append(
            TorrentRelease(
                source_name="nyaa",
                title=title,
                magnet=magnet,
                url=text("guid"),
                size_bytes=parse_size(text("size", NYAA_NS)),
                seeders=int(text("seeders", NYAA_NS) or 0),
                leechers=int(text("leechers", NYAA_NS) or 0),
            )
        )
    return releases


class NyaaIndexer(TorrentIndexer):
    name = "nyaa"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
        )

    async def search(self, query: str) -> list[TorrentRelease]:
        resp = await rl_request(
            self._client, "GET", BASE_URL, limiter=_limiter,
            # c=3_1 → Literature - English-translated; f=0 → no filter
            params={"page": "rss", "q": query, "c": "3_1", "f": "0"},
        )
        resp.raise_for_status()
        releases = parse_rss(resp.text)
        return sorted(releases, key=lambda r: r.seeders, reverse=True)


indexer = NyaaIndexer()
