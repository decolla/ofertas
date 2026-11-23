"""Polite HTTP client: honest UA, robots.txt, per-host rate limit, retry+backoff.

Never bypasses anti-bot protection: a 403/429 or a challenge page raises
BlockedError and the caller skips the source.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

_CHALLENGE_MARKERS = (
    "<title>Just a moment...</title>",
    "cf-challenge",
    "challenges.cloudflare.com",
    "g-recaptcha",
    "hcaptcha.com",
    "Attention Required! | Cloudflare",
)


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """The site refused us (robots, 403/429, captcha). Do not retry, do not bypass."""


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20,
        retries: int = 3,
        backoff_base: float = 2,
        min_interval: float = 5,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.user_agent = user_agent
        self.retries = retries
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self._sleep = sleep
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "pt-BR,pt;q=0.9",
            },
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- robots.txt -------------------------------------------------------
    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        try:
            resp = self._request(f"{origin}/robots.txt", check_robots=False)
        except BlockedError:
            raise
        except FetchError:
            log.warning("robots_unavailable", extra={"origin": origin})
            return None  # unreachable robots.txt: allowed per RFC 9309 (5xx aside)
        if resp.status_code >= 400:
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    # -- requests ---------------------------------------------------------
    def get(self, url: str) -> httpx.Response:
        return self._request(url, check_robots=True)

    def _throttle(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _request(self, url: str, check_robots: bool) -> httpx.Response:
        if check_robots and not self.allowed(url):
            raise BlockedError(f"robots.txt não permite {url}")
        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if resp.status_code in (403, 429) or _is_challenge(resp):
                    raise BlockedError(f"{url} bloqueou a coleta (HTTP {resp.status_code})")
                if resp.status_code < 500:
                    return resp
                last_exc = FetchError(f"HTTP {resp.status_code} em {url}")
            if attempt < self.retries:
                delay = self.backoff_base ** (attempt + 1)
                log.info("retry", extra={"url": url, "attempt": attempt + 1, "delay": delay})
                self._sleep(delay)
        raise FetchError(f"falha ao buscar {url}: {last_exc}") from last_exc


def _is_challenge(resp: httpx.Response) -> bool:
    if "text/html" not in resp.headers.get("content-type", ""):
        return False
    head = resp.text[:5000]
    return any(marker in head for marker in _CHALLENGE_MARKERS)
