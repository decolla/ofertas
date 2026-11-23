"""Pure helpers: BRL price parsing, URL canonicalisation, product keys."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gclid|fbclid|ref|ref_|tag|aff.*|afiliado|partner.*|pid|mkt_.*|"
    r"source|campaign|matt_.*|_ga|srsltid)$",
    re.I,
)

# Words that carry no identity for a product listing.
_STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "com", "para", "pra", "em", "a", "o",
    "as", "os", "no", "na", "por", "the", "and", "with", "for", "-",
    "oferta", "promocao", "cupom", "frete", "gratis", "novo", "nova", "original",
}


def parse_brl(text: str | float | int | None) -> float | None:
    """'R$ 1.149,00' -> 1149.0 ; '899' -> 899.0 ; None/'' -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return round(float(text), 2)
    s = re.sub(r"[^\d,.]", "", text)
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):
        s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def normalize_title(title: str) -> str:
    s = strip_accents(title).lower()
    s = s.replace("&", " ").replace('"', " polegadas ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _STOPWORDS]
    return " ".join(tokens)


def product_key(title: str) -> str:
    """Order-insensitive key so 'SSD Kingston 1TB NV3' == 'Kingston NV3 SSD 1TB'."""
    tokens = sorted(set(normalize_title(title).split()))
    return " ".join(tokens)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower() or "https", parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


def normalize_store(store: str | None) -> str | None:
    if not store:
        return None
    return strip_accents(store).lower().replace("!", "").replace(" ", "").strip()
