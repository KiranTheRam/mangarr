"""Fetch torrent metadata and hash the original bencoded info dictionary."""
import base64
import binascii
import hashlib
import re

import httpx

MAX_TORRENT_SIZE = 10_000_000
_BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")


def magnet_btih_hex(magnet: str) -> str:
    """The magnet's info-hash in the lowercase hex form qBittorrent reports.
    Base32 hashes (older magnet links) are converted — matching against
    qBittorrent by their raw form would never succeed."""
    m = _BTIH_RE.search(magnet)
    if not m:
        return ""
    raw = m.group(1)
    if len(raw) == 40:
        return raw.lower()
    try:
        return base64.b32decode(raw).hex()
    except (binascii.Error, ValueError):
        return ""


def torrent_info_hash(data: bytes) -> str:
    from ..torrent_selection import bdecode

    decoded = bdecode(data)
    if not isinstance(decoded, dict) or not isinstance(decoded.get(b"info"), dict):
        raise ValueError("Missing torrent info dictionary")
    info_dict = decoded[b"info"]
    if info_dict.get(b"meta version") == 2:
        # Tracking and cancellation use v1 btih hashes. A v2-only torrent has
        # no SHA-1 identity, even though its info dictionary can be SHA-1 hashed.
        # Hybrid torrents retain their v1 pieces and can use that identity.
        pieces = info_dict.get(b"pieces")
        if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
            raise ValueError("BitTorrent v2-only metadata is unsupported; choose a v1 or hybrid torrent")

    def parse(pos: int, depth: int = 0):
        if depth > 64 or pos >= len(data):
            raise ValueError("Invalid torrent metadata")
        token = data[pos:pos + 1]
        if token == b'i':
            end = data.index(b'e', pos + 1)
            int(data[pos + 1:end])
            return end + 1, None
        if token in (b'd', b'l'):
            cursor = pos + 1
            while cursor < len(data) and data[cursor:cursor + 1] != b'e':
                cursor, _ = parse(cursor, depth + 1)
                if token == b'd':
                    cursor, _ = parse(cursor, depth + 1)
            if cursor >= len(data):
                raise ValueError("Invalid torrent metadata")
            return cursor + 1, None
        if not token.isdigit():
            raise ValueError("Invalid torrent metadata")
        colon = data.index(b':', pos)
        length = int(data[pos:colon])
        end = colon + 1 + length
        if length < 0 or end > len(data):
            raise ValueError("Invalid torrent metadata")
        return end, data[colon + 1:end]

    if not data.startswith(b'd'):
        raise ValueError("Expected a torrent dictionary")
    pos, info = 1, None
    while pos < len(data) and data[pos:pos + 1] != b'e':
        pos, key = parse(pos)
        start = pos
        pos, _ = parse(pos)
        if key == b'info':
            if info is not None or data[start:start + 1] != b'd':
                raise ValueError("Invalid torrent info dictionary")
            info = data[start:pos]
    if pos != len(data) - 1 or data[pos:] != b'e' or info is None:
        raise ValueError("Missing torrent info dictionary")
    return hashlib.sha1(info).hexdigest()


async def fetch_torrent(url: str) -> tuple[str, str, bytes | None]:
    # Follow HTTP redirects explicitly: Prowlarr can redirect to a magnet URI.
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for _ in range(6):
            if url.startswith("magnet:"):
                info_hash = magnet_btih_hex(url)
                if not info_hash:
                    raise ValueError("Invalid magnet redirect")
                return url, info_hash, None
            if not url.startswith(("http://", "https://")):
                raise ValueError("Invalid torrent download URL")
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_TORRENT_SIZE:
                        raise ValueError("Torrent metadata is too large")
                content = bytes(data)
                return url, torrent_info_hash(content), content
    raise ValueError("Too many torrent redirects")
