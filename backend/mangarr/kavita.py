"""Kavita library-scan notifications.

Kavita only notices new CBZ files when it scans, and its own folder-watching
is optional and coarse. After mangarr imports chapters it therefore asks
Kavita to scan the library those files landed in, so downloads show up in the
reader without waiting for Kavita's nightly pass.

A *series* scan (``POST /api/Series/scan``) is preferred: it walks one series
folder instead of the whole library. It needs Kavita's own series id, which
only exists once Kavita has seen the series at least once — so a series added
by mangarr for the first time falls back to a library scan, which is what
makes Kavita discover the new folder in the first place.

Scans are debounced: importing thirty chapters in a row must queue one scan,
not thirty. Delivery is fire-and-forget — an unreachable Kavita must never
block or fail a download.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import httpx

from . import USER_AGENT
from .util import normalize_title

log = logging.getLogger(__name__)

PLUGIN_NAME = "mangarr"
TIMEOUT = 30.0

# Chapters of one series import one after another; each would otherwise fire
# its own scan. Collect a burst and scan once when it goes quiet.
DEBOUNCE_SECONDS = 15.0


class KavitaError(RuntimeError):
    pass


@dataclass
class KavitaLibrary:
    id: int
    name: str
    folders: list[str] = field(default_factory=list)


class KavitaClient:
    """Kavita API v1 client, authenticated with an account's API key.

    The API key is exchanged for a short-lived JWT via the Plugin endpoint;
    every client instance authenticates once and reuses the token.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        self._token = ""
        self.version = ""

    async def close(self) -> None:
        await self._client.aclose()

    async def _authenticate(self) -> None:
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/Plugin/authenticate",
                params={"apiKey": self.api_key, "pluginName": PLUGIN_NAME},
            )
        except httpx.HTTPError as exc:
            raise KavitaError(f"Cannot reach Kavita at {self.base_url}: {exc}") from exc
        if resp.status_code in (400, 401):
            raise KavitaError("Kavita rejected the API key")
        if resp.status_code >= 300:
            raise KavitaError(
                f"Kavita authentication failed: HTTP {resp.status_code} {resp.text[:100]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            # a reverse proxy or the wrong host answering with an HTML page
            raise KavitaError(
                f"{self.base_url} did not return a Kavita API response"
            ) from exc
        self._token = data.get("token") or ""
        self.version = data.get("kavitaVersion") or ""
        if not self._token:
            raise KavitaError("Kavita returned no authentication token")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._token:
            await self._authenticate()
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._client.request(
                method, f"{self.base_url}{path}", headers=headers, **kwargs
            )
            if resp.status_code == 401:  # token expired mid-run
                await self._authenticate()
                headers = {"Authorization": f"Bearer {self._token}"}
                resp = await self._client.request(
                    method, f"{self.base_url}{path}", headers=headers, **kwargs
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise KavitaError(f"Kavita request {method} {path} failed: {exc}") from exc
        return resp

    async def libraries(self) -> list[KavitaLibrary]:
        resp = await self._request("GET", "/api/Library/libraries")
        try:
            rows = resp.json()
        except ValueError as exc:
            raise KavitaError("Kavita returned an unreadable library list") from exc
        return [
            KavitaLibrary(
                id=int(row["id"]),
                name=str(row.get("name", "")),
                folders=[str(f) for f in (row.get("folders") or [])],
            )
            for row in rows
            if row.get("id") is not None
        ]

    async def scan_library(self, library_id: int, force: bool = False) -> None:
        await self._request(
            "POST", "/api/Library/scan",
            params={"libraryId": library_id, "force": str(force).lower()},
        )

    async def scan_series(
        self, library_id: int, series_id: int, force: bool = False
    ) -> None:
        await self._request(
            "POST", "/api/Series/scan",
            json={"libraryId": library_id, "seriesId": series_id, "forceUpdate": force},
        )

    async def find_series_id(self, library_id: int, titles: list[str]) -> int | None:
        """Kavita's series id for the first title that matches unambiguously.

        Kavita's search is fuzzy, so results are re-checked against the
        requested title: a near-miss must not send a scan to the wrong series.
        A title matching more than one series in the library is treated as no
        match — scanning the wrong one would leave the new chapters
        undiscovered, while giving up here falls back to a library scan that
        finds them.
        """
        for title in titles:
            if not title.strip():
                continue
            wanted = normalize_title(title)
            if not wanted:
                continue
            try:
                resp = await self._request(
                    "GET", "/api/Search/search",
                    params={"queryString": title, "includeChapterAndFiles": "false"},
                )
                results = (resp.json() or {}).get("series") or []
            except (KavitaError, ValueError):
                return None
            matches: set[int] = set()
            for row in results:
                if int(row.get("libraryId", 0)) != library_id:
                    continue
                names = (
                    row.get("name"), row.get("originalName"),
                    row.get("localizedName"), row.get("sortName"),
                )
                if any(normalize_title(str(n)) == wanted for n in names if n):
                    matches.add(int(row["seriesId"]))
            if len(matches) == 1:
                return matches.pop()
            if len(matches) > 1:
                log.info(
                    "%r matches %d Kavita series in library %d; scanning the "
                    "library instead of guessing", title, len(matches), library_id
                )
                return None
        return None


async def test_connection(base_url: str, api_key: str) -> tuple[str, list[KavitaLibrary]]:
    """Returns (Kavita version, libraries) or raises KavitaError."""
    client = KavitaClient(base_url, api_key)
    try:
        libraries = await client.libraries()
        return client.version, libraries
    finally:
        await client.close()


# ------------------------------------------------------------ library mapping

def _path_parts(path: str) -> list[str]:
    """Case-folded path components, for either separator.

    mangarr and Kavita usually run in different containers, so the same share
    is mounted at different absolute paths — only the trailing components are
    comparable.
    """
    cleaned = path.strip().replace("\\", "/")
    parts = PurePosixPath(cleaned).parts if "/" in cleaned else PureWindowsPath(cleaned).parts
    return [p.lower() for p in parts if p not in ("/", "\\", "")]


def match_library(
    libraries: list[KavitaLibrary], root_path: str
) -> KavitaLibrary | None:
    """Guess which Kavita library holds `root_path`.

    mangarr and Kavita usually mount the same share at different absolute
    paths, so the leading components rarely agree — the library folder's own
    name is the signal. A candidate must share at least its last component
    (``/data/manga`` matches ``/mnt/media/manga``); among candidates that do,
    the one sharing the most trailing components wins, so an exact path always
    beats a same-name folder elsewhere. Users whose paths share nothing can map
    root folders explicitly in Settings.
    """
    root = _path_parts(root_path)
    if not root:
        return None
    best: tuple[int, KavitaLibrary] | None = None
    for library in libraries:
        for folder in library.folders:
            parts = _path_parts(folder)
            if not parts:
                continue
            shared = 0
            for mine, theirs in zip(reversed(root), reversed(parts)):
                if mine != theirs:
                    break
                shared += 1
            if shared == 0:
                continue
            if best is None or shared > best[0]:
                best = (shared, library)
    return best[1] if best else None


def parse_library_map(raw: str) -> dict[int, int]:
    """Explicit root-folder-id → Kavita-library-id overrides, stored as JSON.

    Unparseable content maps nothing rather than raising: a corrupted setting
    must degrade to auto-detection, not break every import.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("kavita_library_map is not valid JSON; ignoring it")
        return {}
    if not isinstance(data, dict):
        return {}
    mapping: dict[int, int] = {}
    for key, value in data.items():
        try:
            library_id = int(value)
        except (TypeError, ValueError):
            continue
        try:
            root_id = int(key)
        except (TypeError, ValueError):
            continue
        if library_id > 0:
            mapping[root_id] = library_id
    return mapping


async def resolve_library_id(
    client: KavitaClient, values: dict[str, str],
    root_folder_id: int | None, root_path: str,
    libraries: list[KavitaLibrary] | None = None,
) -> int | None:
    """The Kavita library for a mangarr root folder: explicit mapping first,
    then a path-based guess. None means "don't scan" — better than scanning an
    unrelated library."""
    mapping = parse_library_map(values.get("kavita_library_map", ""))
    if root_folder_id is not None and root_folder_id in mapping:
        return mapping[root_folder_id]
    if libraries is None:
        libraries = await client.libraries()
    match = match_library(libraries, root_path)
    if match is None:
        log.warning(
            "no Kavita library matches root folder %s; map it in Settings → Kavita",
            root_path,
        )
        return None
    return match.id


# ------------------------------------------------------------------ scanning

@dataclass(frozen=True)
class ScanRequest:
    root_folder_id: int | None
    root_path: str
    titles: tuple[str, ...]  # series title plus alternates/folder name


_pending: set[ScanRequest] = set()
_flush_task: asyncio.Task | None = None
_flush_values: dict[str, str] = {}


def is_configured(values: dict[str, str]) -> bool:
    return (
        values.get("kavita_enabled") == "true"
        and bool(values.get("kavita_url", "").strip())
        and bool(values.get("kavita_api_key", "").strip())
    )


async def run_scans(values: dict[str, str], requests: set[ScanRequest]) -> None:
    """Scan once per distinct target for a batch of imports."""
    client = KavitaClient(values["kavita_url"].strip(), values["kavita_api_key"].strip())
    prefer_series = values.get("kavita_scan_mode", "series") == "series"
    try:
        libraries = await client.libraries()
        # a library scan covers every series in it, so collect those first and
        # skip the per-series scans they already subsume
        library_scans: set[int] = set()
        series_scans: set[tuple[int, int]] = set()
        for request in requests:
            library_id = await resolve_library_id(
                client, values, request.root_folder_id, request.root_path, libraries
            )
            if library_id is None:
                continue
            series_id = None
            if prefer_series:
                series_id = await client.find_series_id(library_id, list(request.titles))
            if series_id is None:
                # unknown to Kavita (a series mangarr just created) or an
                # ambiguous title — only a library scan is guaranteed to find it
                library_scans.add(library_id)
            else:
                series_scans.add((library_id, series_id))
        for library_id in library_scans:
            await client.scan_library(library_id)
            log.info("requested Kavita scan of library %d", library_id)
        for library_id, series_id in series_scans:
            if library_id in library_scans:
                continue
            await client.scan_series(library_id, series_id)
            log.info(
                "requested Kavita scan of series %d in library %d", series_id, library_id
            )
    finally:
        await client.close()


async def _flush() -> None:
    global _flush_task
    try:
        # imports that land while a scan is in flight are picked up by the next
        # pass rather than waiting for some later import to restart the timer
        while True:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            requests, values = set(_pending), dict(_flush_values)
            _pending.clear()
            if not requests:
                return
            try:
                await run_scans(values, requests)
            except KavitaError as exc:
                log.warning("Kavita scan request failed: %s", exc)
            except Exception:  # a broken notify must not kill the import path
                log.exception("Kavita scan request failed unexpectedly")
    finally:
        _flush_task = None


def notify_import(
    values: dict[str, str], root_folder_id: int | None, root_path: str,
    titles: list[str],
) -> None:
    """Schedule a debounced Kavita scan for freshly imported files."""
    global _flush_task, _flush_values
    if not is_configured(values):
        return
    if not root_path.strip():
        return
    _pending.add(ScanRequest(
        root_folder_id=root_folder_id,
        root_path=root_path,
        titles=tuple(t for t in titles if t and t.strip()),
    ))
    _flush_values = dict(values)
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.get_running_loop().create_task(_flush())


def validate_settings(values: dict[str, str]) -> None:
    """Raises ValueError for Kavita settings that would silently never work."""
    url = values.get("kavita_url", "").strip()
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("kavita_url must be a valid http:// or https:// URL")
    if values.get("kavita_enabled") == "true":
        if not url:
            raise ValueError("kavita_url is required when the Kavita connection is enabled")
        if not values.get("kavita_api_key", "").strip():
            raise ValueError(
                "kavita_api_key is required when the Kavita connection is enabled"
            )
    mode = values.get("kavita_scan_mode")
    if mode is not None and mode not in ("series", "library"):
        raise ValueError("kavita_scan_mode must be 'series' or 'library'")
    raw = values.get("kavita_library_map", "")
    if raw and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError("kavita_library_map must be valid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("kavita_library_map must be a JSON object")
