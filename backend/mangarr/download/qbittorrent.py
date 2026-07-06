"""qBittorrent Web API v2 client (login, add magnet, poll status)."""

from dataclasses import dataclass

import httpx

from .. import USER_AGENT


class QbtError(RuntimeError):
    pass


@dataclass
class QbtTorrent:
    hash: str
    name: str
    progress: float  # 0..1
    state: str
    content_path: str
    category: str

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.state in (
            "uploading",
            "stalledUP",
            "pausedUP",
            "stoppedUP",
            "queuedUP",
            "forcedUP",
        )


class QbtClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Referer": self.base_url},
            timeout=30,
        )
        self._logged_in = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        resp = await self._client.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        # qBittorrent 4.x answers 200 "Ok."/"Fails."; 5.x answers 204 on
        # success and 401 on bad credentials.
        if resp.status_code >= 300 or resp.text.strip() == "Fails.":
            raise QbtError(f"qBittorrent login failed: HTTP {resp.status_code} {resp.text[:100]}")
        self._logged_in = True

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        resp = await self._client.request(method, f"{self.base_url}/api/v2{path}", **kwargs)
        if resp.status_code == 403:
            await self._login()
            resp = await self._client.request(method, f"{self.base_url}/api/v2{path}", **kwargs)
        resp.raise_for_status()
        return resp

    async def version(self) -> str:
        resp = await self._request("GET", "/app/version")
        return resp.text.strip()

    async def default_save_path(self) -> str:
        """qBittorrent's configured default download directory."""
        try:
            resp = await self._request("GET", "/app/preferences")
            return (resp.json() or {}).get("save_path", "") or ""
        except (httpx.HTTPError, ValueError):
            return ""

    async def ensure_category(self, name: str, save_path: str | None = None) -> None:
        """Create the category (idempotent) so torrents land in its subfolder."""
        data = {"category": name}
        if save_path:
            data["savePath"] = save_path
        resp = await self._client.request(
            "POST", f"{self.base_url}/api/v2/torrents/createCategory", data=data
        )
        if resp.status_code == 403:
            await self._login()
            resp = await self._client.request(
                "POST", f"{self.base_url}/api/v2/torrents/createCategory", data=data
            )
        # 409/Conflict means it already exists — set its path instead
        if resp.status_code == 409 and save_path:
            await self._request("POST", "/torrents/editCategory", data=data)

    async def add_magnet(self, magnet: str, category: str, save_path: str | None = None) -> None:
        data = {"urls": magnet, "category": category}
        if save_path:
            data["savepath"] = save_path
            data["autoTMM"] = "false"
        resp = await self._request("POST", "/torrents/add", data=data)
        if resp.text.strip() == "Fails.":
            raise QbtError("qBittorrent rejected the torrent")

    async def list_torrents(self, category: str) -> list[QbtTorrent]:
        resp = await self._request("GET", "/torrents/info", params={"category": category})
        return [
            QbtTorrent(
                hash=t.get("hash", ""),
                name=t.get("name", ""),
                progress=float(t.get("progress", 0)),
                state=t.get("state", ""),
                content_path=t.get("content_path", ""),
                category=t.get("category", ""),
            )
            for t in resp.json()
        ]

    async def get_torrent(self, torrent_hash: str) -> QbtTorrent | None:
        resp = await self._request("GET", "/torrents/info", params={"hashes": torrent_hash})
        torrents = resp.json()
        if not torrents:
            return None
        t = torrents[0]
        return QbtTorrent(
            hash=t.get("hash", ""),
            name=t.get("name", ""),
            progress=float(t.get("progress", 0)),
            state=t.get("state", ""),
            content_path=t.get("content_path", ""),
            category=t.get("category", ""),
        )


async def test_connection(base_url: str, username: str, password: str) -> str:
    """Returns qBittorrent version or raises QbtError."""
    client = QbtClient(base_url, username, password)
    try:
        return await client.version()
    except httpx.HTTPError as exc:
        raise QbtError(f"Cannot reach qBittorrent at {base_url}: {exc}") from exc
    finally:
        await client.close()
