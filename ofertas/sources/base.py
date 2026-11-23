from __future__ import annotations

from abc import ABC, abstractmethod

from ofertas.http import PoliteClient
from ofertas.models import RawOffer


class Source(ABC):
    """A pluggable offer source. Subclasses implement parse(); fetch() is generic."""

    name: str = ""

    def __init__(self, client: PoliteClient, urls: list[str]):
        self.client = client
        self.urls = urls

    def fetch(self) -> list[RawOffer]:
        seen: dict[str, RawOffer] = {}
        for url in self.urls:
            resp = self.client.get(url)
            resp.raise_for_status()
            for offer in self.parse(resp.text):
                seen.setdefault(offer.source_id, offer)
        return list(seen.values())

    @staticmethod
    @abstractmethod
    def parse(html: str) -> list[RawOffer]: ...
