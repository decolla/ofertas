"""Gatry: the public /promocoes page is server-rendered as a plain <ul> list.

Each item looks like:
  <li><a href="/promocoes/<uuid>/<slug>">Title</a> — R$ 899,00 · Amazon</li>
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ofertas.models import RawOffer
from ofertas.normalize import parse_brl
from ofertas.sources.base import Source

BASE = "https://gatry.com"
_HREF = re.compile(r"^/promocoes/([0-9a-f-]{36})(?:/|$)")
_TAIL = re.compile(r"R\$\s*([\d.,]+)(?:\s*·\s*(.+))?")


class _ListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, str]] = []  # (href, title, tail)
        self._in_li = False
        self._in_a = False
        self._href = ""
        self._title: list[str] = []
        self._tail: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self._in_li, self._href, self._title, self._tail = True, "", [], []
        elif tag == "a" and self._in_li:
            self._in_a = True
            self._href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_a = False
        elif tag == "li" and self._in_li:
            self._in_li = False
            self.items.append((self._href, "".join(self._title), "".join(self._tail)))

    def handle_data(self, data):
        if self._in_a:
            self._title.append(data)
        elif self._in_li:
            self._tail.append(data)


class GatrySource(Source):
    name = "gatry"

    @staticmethod
    def parse(html: str) -> list[RawOffer]:
        p = _ListParser()
        p.feed(html)
        offers: list[RawOffer] = []
        for href, title, tail in p.items:
            m = _HREF.match(href)
            t = _TAIL.search(tail)
            if not m or not t or not title.strip():
                continue
            price = parse_brl(t.group(1))
            if not price:
                continue
            offers.append(
                RawOffer(
                    source="gatry",
                    source_id=m.group(1),
                    title=" ".join(title.split()),
                    price=price,
                    url=BASE + href,
                    store=(t.group(2) or "").strip() or None,
                )
            )
        if not offers and "<li>" in html:
            raise ValueError("Gatry: nenhuma oferta reconhecida (layout mudou?)")
        return offers
