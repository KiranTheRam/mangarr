"""Bound failed sign-in attempts and queued password checks per process."""

from collections import OrderedDict
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address
from math import ceil
from threading import Lock
from time import monotonic

ATTEMPTS = 5
WINDOW_SECONDS = 60
MAX_PEERS = 1024
MAX_PENDING = 8


@dataclass
class AttemptWindow:
    expires_at: float
    attempts: int = 0


def _peer_key(host: str | None) -> bytes | None:
    if host is None:
        return None
    try:
        address = ip_address(host)
    except ValueError:
        return None
    if isinstance(address, IPv6Address):
        mapped = address.ipv4_mapped
        return mapped.packed if mapped else address.packed[:8]
    return address.packed


class SignInThrottle:
    def __init__(self) -> None:
        self._windows: OrderedDict[bytes | None, AttemptWindow] = OrderedDict()
        self._salt: bytes | None = None
        self._pending = 0
        self._lock = Lock()

    def reserve(self, host: str | None, salt: bytes) -> AttemptWindow | int:
        """Reserve an attempt before verification or return Retry-After seconds."""
        peer = _peer_key(host)
        with self._lock:
            now = monotonic()
            if self._salt != salt:
                self._windows.clear()
                self._salt = salt
            while self._windows:
                oldest = next(iter(self._windows.values()))
                if oldest.expires_at > now:
                    break
                self._windows.popitem(last=False)

            window = self._windows.get(peer)
            if window is not None and window.attempts >= ATTEMPTS:
                return ceil(window.expires_at - now)
            if window is None and len(self._windows) >= MAX_PEERS:
                oldest = next(iter(self._windows.values()))
                return ceil(oldest.expires_at - now)
            if self._pending >= MAX_PENDING:
                return 1
            if window is None:
                window = self._windows[peer] = AttemptWindow(now + WINDOW_SECONDS)
            window.attempts += 1
            self._pending += 1
            return window

    def finish(self, host: str | None, window: AttemptWindow, *, success: bool) -> None:
        peer = _peer_key(host)
        with self._lock:
            self._pending -= 1
            if success and self._windows.get(peer) is window:
                del self._windows[peer]
