"""User term lists kept in `settings` (JSON): /quero (wishlist) and /silenciar.

A term matches an offer when every word of the term is in the offer title,
ignoring accents, case, word order and stopwords: "air fryer" matches
"Fritadeira Air Fryer Mondial 4L".
"""

from __future__ import annotations

import json

from ofertas.normalize import normalize_title

WISHLIST = "wishlist"
MUTED = "muted"
MAX_TERMS = 50
MAX_TERM_LEN = 60


def get_terms(db, key: str) -> list[str]:
    raw = db.get_setting(key)
    try:
        terms = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    return [t for t in terms if isinstance(t, str)]


def _save(db, key: str, terms: list[str]) -> None:
    db.set_setting(key, json.dumps(terms, ensure_ascii=False))


def clean_term(text: str) -> str:
    return " ".join(text.split())[:MAX_TERM_LEN]


def add_term(db, key: str, text: str) -> tuple[bool, str]:
    """(added, message). Rejects empty, duplicate and over-limit terms."""
    term = clean_term(text)
    if not normalize_title(term):
        return False, "Termo vazio ou só com palavras comuns."
    terms = get_terms(db, key)
    if any(normalize_title(t) == normalize_title(term) for t in terms):
        return False, f"“{term}” já está na lista."
    if len(terms) >= MAX_TERMS:
        return False, f"A lista já tem {MAX_TERMS} termos; remova algum antes."
    _save(db, key, [*terms, term])
    return True, term


def remove_term(db, key: str, text: str) -> str | None:
    """Removes by position (1-based) or by term. Returns the removed term."""
    terms = get_terms(db, key)
    text = clean_term(text)
    idx = None
    if text.isdigit() and 1 <= int(text) <= len(terms):
        idx = int(text) - 1
    else:
        norm = normalize_title(text)
        idx = next((i for i, t in enumerate(terms) if normalize_title(t) == norm), None)
    if idx is None:
        return None
    removed = terms.pop(idx)
    _save(db, key, terms)
    return removed


def matches(term: str, title: str) -> bool:
    words = set(normalize_title(term).split())
    return bool(words) and words <= set(normalize_title(title).split())


def first_match(terms: list[str], title: str) -> str | None:
    return next((t for t in terms if matches(t, title)), None)
