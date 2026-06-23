"""Proxy manager — residential proxy rotation for anti-detection."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Proxy:
    """A proxy server configuration."""

    host: str
    port: int
    username: str = ""
    password: str = ""
    protocol: str = "http"

    def to_playwright_proxy(self) -> dict:
        """Convert to Playwright proxy format."""
        proxy = {
            "server": f"{self.protocol}://{self.host}:{self.port}",
        }
        if self.username:
            proxy["username"] = self.username
            proxy["password"] = self.password
        return proxy


class ProxyManager:
    """Manage a pool of proxies with rotation."""

    def __init__(self, proxy_list: list[str] | None = None, seed: int | None = None):
        self._proxies: list[Proxy] = []
        self._rng = random.Random(seed)
        self._current_index = 0

        if proxy_list:
            for p in proxy_list:
                proxy = self._parse_proxy(p)
                if proxy:
                    self._proxies.append(proxy)

    def _parse_proxy(self, proxy_str: str) -> Proxy | None:
        """Parse a proxy string like 'http://user:pass@host:port' or 'host:port'."""
        try:
            if "://" in proxy_str:
                protocol, rest = proxy_str.split("://", 1)
            else:
                protocol = "http"
                rest = proxy_str

            if "@" in rest:
                creds, hostport = rest.rsplit("@", 1)
                username, password = creds.split(":", 1)
            else:
                hostport = rest
                username, password = "", ""

            host, port = hostport.rsplit(":", 1)
            return Proxy(host=host, port=int(port), username=username, password=password, protocol=protocol)
        except Exception:
            return None

    def get_proxy(self) -> Proxy | None:
        """Get the next proxy in rotation."""
        if not self._proxies:
            return None
        proxy = self._proxies[self._current_index]
        self._current_index = (self._current_index + 1) % len(self._proxies)
        return proxy

    def get_random_proxy(self) -> Proxy | None:
        """Get a random proxy from the pool."""
        if not self._proxies:
            return None
        return self._rng.choice(self._proxies)

    def add_proxy(self, proxy_str: str) -> None:
        """Add a proxy to the pool."""
        proxy = self._parse_proxy(proxy_str)
        if proxy:
            self._proxies.append(proxy)

    @property
    def count(self) -> int:
        return len(self._proxies)
