"""Source registry. To add a source: create a module with a Source subclass,
register it here and add a block under `sources:` in config.yaml."""

from ofertas.sources.base import Source
from ofertas.sources.gatry import GatrySource
from ofertas.sources.promobit import PromobitSource

REGISTRY: dict[str, type[Source]] = {
    PromobitSource.name: PromobitSource,
    GatrySource.name: GatrySource,
}

__all__ = ["REGISTRY", "Source"]
