"""Request signing for the MangaFire JSON API.

MangaFire rejects API calls without a ``vrf`` query parameter (HTTP 403,
"Missing token.").  The site's web client computes that token with an
obfuscated, VM-based script bundled into its frontend; the token is a
deterministic function of the request path and query, but the cipher's keys
live inside that script and change whenever the site ships a new build.

Rather than porting the cipher (and breaking on every rebuild), Mangarr loads
the site's current signing module into an embedded V8 isolate and asks it for
tokens.  The isolate has no network, filesystem, or timer access — the script
can only compute — and every call runs under a time and memory cap.  The
module is rediscovered periodically and whenever the API rejects a token.
"""

import asyncio
import json
import logging
import re
import time
from urllib.parse import urljoin

import httpx
from py_mini_racer import MiniRacer

log = logging.getLogger(__name__)

TOKEN_FUNCTION = "getProtectionToken"
# rebuilds rotate the signing keys; re-read the frontend at least this often
CACHE_SECONDS = 6 * 3600
MAX_MODULES = 25
CALL_TIMEOUT_SECONDS = 5
MAX_MEMORY_BYTES = 128 * 1024 * 1024

_SCRIPT_SRC = re.compile(r"<script\b[^>]*\bsrc=[\"']([^\"']+\.js[^\"']*)[\"']", re.I)
_RELATIVE_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["'](\.{1,2}/[^"']+\.js)["']"""
)
_TRAILING_EXPORT = re.compile(r"export\s*\{[^}]*\}\s*;?\s*$")

# The signing module expects a browser: it keys its bytecode on
# navigator.appCodeName and encodes with TextEncoder/atob/btoa.  Timers are
# no-ops so its periodic anti-debugging checks never run.
_BROWSER_SHIM = r"""
var window = globalThis, self = globalThis;
var location = {href: 'https://mangafire.to/', hostname: 'mangafire.to',
  host: 'mangafire.to', origin: 'https://mangafire.to', protocol: 'https:',
  pathname: '/', search: '', hash: ''};
var document = {domain: 'mangafire.to', location: location, referrer: '',
  cookie: '', createElement: function () { return {}; },
  querySelector: function () { return null; }, addEventListener: function () {}};
var navigator = {userAgent: 'Mozilla/5.0', appCodeName: 'Mozilla',
  appName: 'Netscape', webdriver: false, language: 'en-US',
  languages: ['en-US'], platform: 'MacIntel'};
var setTimeout = function () { return 0; }, setInterval = function () { return 0; };
var clearTimeout = function () {}, clearInterval = function () {};
function TextEncoder() {}
TextEncoder.prototype.encode = function (str) {
  var u = unescape(encodeURIComponent(String(str === undefined ? '' : str)));
  var out = new Uint8Array(u.length);
  for (var i = 0; i < u.length; i++) out[i] = u.charCodeAt(i);
  return out;
};
function TextDecoder() {}
TextDecoder.prototype.decode = function (buf) {
  if (!buf) return '';
  var b = buf instanceof Uint8Array ? buf : new Uint8Array(buf.buffer || buf), s = '';
  for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return decodeURIComponent(escape(s));
};
var _B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function btoa(s) {
  s = String(s);
  var out = '', i = 0;
  while (i < s.length) {
    var a = s.charCodeAt(i++), b = s.charCodeAt(i++), c = s.charCodeAt(i++);
    var n = (a << 16) | ((b || 0) << 8) | (c || 0);
    out += _B64[(n >> 18) & 63] + _B64[(n >> 12) & 63] +
      (isNaN(b) ? '=' : _B64[(n >> 6) & 63]) + (isNaN(c) ? '=' : _B64[n & 63]);
  }
  return out;
}
function atob(s) {
  s = String(s).replace(/[^A-Za-z0-9+/]/g, '');
  var out = '', bits = 0, value = 0;
  for (var i = 0; i < s.length; i++) {
    value = (value << 6) | _B64.indexOf(s[i]);
    bits += 6;
    if (bits >= 8) { bits -= 8; out += String.fromCharCode((value >> bits) & 255); }
  }
  return out;
}
"""


async def _run(context: MiniRacer, code: str):
    return await asyncio.wait_for(
        context.eval_cancelable(code), timeout=CALL_TIMEOUT_SECONDS
    )


class MangaFireTokenError(RuntimeError):
    """The site's request-signing script could not be found or run."""


class MangaFireSigner:
    """Produces ``vrf`` tokens using the site's own signing module."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._context: MiniRacer | None = None
        self._module_url = ""
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Forget the loaded module (e.g. after the API rejected a token)."""
        self._context = None
        self._loaded_at = 0.0

    async def sign(
        self, client: httpx.AsyncClient, path: str, params: dict | None = None
    ) -> str:
        context = await self._ensure_loaded(client)
        expr = f"{TOKEN_FUNCTION}({json.dumps(path)}, {json.dumps(dict(params or {}))})"
        try:
            token = await _run(context, expr)
        except Exception as exc:
            self.invalidate()
            raise MangaFireTokenError(f"MangaFire signing failed: {exc}") from exc
        if not isinstance(token, str) or not token:
            self.invalidate()
            raise MangaFireTokenError("MangaFire signing returned no token")
        return token

    async def _ensure_loaded(self, client: httpx.AsyncClient) -> MiniRacer:
        async with self._lock:
            fresh = time.monotonic() - self._loaded_at < CACHE_SECONDS
            if self._context is None or not fresh:
                self._context, self._module_url = await self._load(client)
                self._loaded_at = time.monotonic()
                log.info("loaded MangaFire signing module %s", self._module_url)
            return self._context

    async def _load(self, client: httpx.AsyncClient) -> tuple[MiniRacer, str]:
        try:
            url, source = await self._find_module(client)
        except httpx.HTTPError as exc:
            raise MangaFireTokenError(
                f"could not fetch MangaFire's frontend: {exc}"
            ) from exc
        context = MiniRacer()
        context.set_hard_memory_limit(MAX_MEMORY_BYTES)
        try:
            module = _TRAILING_EXPORT.sub("", source.strip())
            # end on a primitive: the module's last statement may be an object
            await _run(context, f"{_BROWSER_SHIM}\n{module}\n;true")
            available = await _run(context, f"typeof globalThis.{TOKEN_FUNCTION}")
        except Exception as exc:
            raise MangaFireTokenError(
                f"MangaFire signing module failed to load: {exc}"
            ) from exc
        if available != "function":
            raise MangaFireTokenError(
                f"MangaFire signing module does not define {TOKEN_FUNCTION}"
            )
        return context, url

    async def _find_module(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """Walk the homepage's scripts and their relative imports until one
        defines the token function."""
        response = await client.get(self._base_url, headers={"Accept": "text/html"})
        response.raise_for_status()
        queue = [urljoin(self._base_url, src) for src in _SCRIPT_SRC.findall(response.text)]
        seen: set[str] = set()
        while queue and len(seen) < MAX_MODULES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            script = await client.get(url, headers={"Accept": "*/*"})
            script.raise_for_status()
            if TOKEN_FUNCTION in script.text:
                return url, script.text
            queue.extend(
                urljoin(url, rel) for rel in _RELATIVE_IMPORT.findall(script.text)
            )
        raise MangaFireTokenError(
            f"no MangaFire script defines {TOKEN_FUNCTION} "
            f"(checked {len(seen)} modules)"
        )
