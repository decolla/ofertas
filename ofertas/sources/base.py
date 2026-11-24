from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from ofertas.http import BlockedError, PoliteClient
from ofertas.models import RawOffer

log = logging.getLogger(__name__)

ROTATION_SLOT_SECONDS = 20 * 60  # one timer tick


def rotation(urls: list[str], per_run: int, now: float | None = None) -> list[str]:
    """The slice of `urls` for this run: round-robin by time slot, no state.

    With 30 category pages and 6 per run, every page is read every 5 runs
    (~100 min), instead of hammering the site with 30 requests every run.
    """
    if not urls or per_run <= 0:
        return []
    if per_run >= len(urls):
        return list(urls)
    slot = int((now if now is not None else time.time()) // ROTATION_SLOT_SECONDS)
    start = (slot * per_run) % len(urls)
    return [urls[(start + i) % len(urls)] for i in range(per_run)]


class Source(ABC):
    """A pluggable offer source. Subclasses implement parse(); fetch() is generic.

    `urls` are read every run; `rotate_urls` are spread over runs,
    `rotate_per_run` at a time.
    """

    name: str = ""

    def __init__(self, client: PoliteClient, urls: list[str],
                 rotate_urls: list[str] | None = None, rotate_per_run: int = 0):
        self.client = client
        self.urls = urls
        self.rotate_urls = rotate_urls or []
        self.rotate_per_run = rotate_per_run

    def pages(self) -> list[str]:
        return list(self.urls) + rotation(self.rotate_urls, self.rotate_per_run)

    def fetch(self) -> list[RawOffer]:
        seen: dict[str, RawOffer] = {}
        errors: list[Exception] = []
        pages = self.pages()
        for url in pages:
            try:
                resp = self.client.get(url)
                resp.raise_for_status()
                offers = self.parse(resp.text)
            except BlockedError:
                raise  # the site said no: stop asking it anything this run
            except Exception as exc:
                # One bad page (404 category, layout change on one listing)
                # doesn't lose the others; all pages failing fails the source.
                log.warning("page_failed", extra={"source": self.name, "url": url, "error": str(exc)})
                errors.append(exc)
                continue
            for offer in offers:
                seen.setdefault(offer.source_id, offer)
        if errors and len(errors) == len(pages):
            raise errors[0]
        return list(seen.values())

    @staticmethod
    @abstractmethod
    def parse(html: str) -> list[RawOffer]: ...
