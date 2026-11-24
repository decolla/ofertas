"""Telegram Bot API over plain HTTP. Messages in pt-BR, HTML parse mode."""

from __future__ import annotations

import html
import logging

import httpx

from ofertas.categories import CATEGORY_LABELS
from ofertas.models import Decision, StoredOffer

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _emoji(label: str) -> str:
    return label.split(" ", 1)[0]


CATEGORY_EMOJI = {c: _emoji(label) for c, label in CATEGORY_LABELS.items()}


def format_offer(offer: StoredOffer, decision: Decision) -> str:
    r = offer.raw
    v = decision.verdict
    e = html.escape
    if v:
        lines = [f"🍌 {CATEGORY_EMOJI.get(v.category, '📦')} <b>{e(v.name_norm)}</b>",
                 f"<i>{e(r.title)}</i>", ""]
    else:
        lines = [f"🍌 <b>{e(r.title)}</b>", ""]
    price_line = f"💰 <b>{brl(r.price)}</b>"
    if r.list_price:
        price_line += f"  <s>{brl(r.list_price)}</s>"
    if r.declared_discount:
        price_line += f"  (−{r.declared_discount:.0%} declarado)"
    lines.append(price_line)
    st = decision.stats
    if st and not decision.insufficient_history and st.median:
        if st.is_new_low:
            lines.append(f"🏆 Menor preço já registrado (antes {brl(st.minimum)})")
        if (st.discount_vs_median or 0) > 0:
            lines.append(f"📉 {st.discount_vs_median:.0%} abaixo da mediana de {brl(st.median)} "
                         f"({st.points} observações)")
    lines.append(f"🏪 {e(r.store or 'Loja não informada')}")
    if r.coupon:
        lines.append(f"🎟️ Cupom: <code>{e(r.coupon)}</code>")
    if r.free_shipping:
        lines.append("🚚 Frete grátis")
    if r.votes is not None or r.temperature is not None:
        lines.append(f"👍 {r.votes or 0} likes · 🔥 {r.temperature or 0:.0f}")
    if v:
        lines.append("")
        lines.append(f"⭐ Nota <b>{v.score}</b>/100")
        lines.append(f"🤖 <i>Claude: {e(v.reason[:200])}</i>")
        if v.flags:
            lines.append(f"🚩 {e(', '.join(f.replace('_', ' ') for f in v.flags))}")
    if decision.variants:
        lines.append("")
        shown = ", ".join(e(t[:60]) for t in decision.variants[:3])
        more = f" e mais {len(decision.variants) - 3}" if len(decision.variants) > 3 else ""
        lines.append(f"🎨 Mesmo preço em outras variações: {shown}{more}")
    if decision.insufficient_history:
        lines.append("")
        lines.append("⚠️ <i>Histórico insuficiente: baseado no desconto declarado (e na comunidade, se houver).</i>")
    lines.append("")
    lines.append(f"<i>via {e(r.source.capitalize())}</i>")
    return "\n".join(lines)


CAPTION_LIMIT = 1024  # Telegram counts the caption after HTML entities are parsed


def _visible_len(text: str) -> int:
    import re

    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _keyboard(button: tuple[str, str]) -> dict:
    return {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}


class Telegram:
    def __init__(self, token: str, chat_id: str, transport: httpx.BaseTransport | None = None):
        self.chat_id = chat_id
        self._token = token
        self._client = httpx.Client(timeout=20, transport=transport)

    def call(self, method: str, payload: dict, timeout: float | None = None):
        resp = self._client.post(
            API.format(token=self._token, method=method), json=payload,
            timeout=timeout or self._client.timeout,
        )
        data = resp.json()
        if not data.get("ok"):
            # Never include the URL (it carries the token) in errors/logs.
            raise RuntimeError(f"Telegram {method} falhou: {data.get('description')}")
        return data["result"]

    _call = call

    def send_text(self, text: str, button: tuple[str, str] | None = None) -> int:
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if button:
            payload["reply_markup"] = _keyboard(button)
        return self._call("sendMessage", payload)["message_id"]

    def send_offer(self, offer: StoredOffer, decision: Decision) -> int:
        text = format_offer(offer, decision)
        button = ("🛒 Abrir oferta", offer.raw.url)
        image = offer.raw.image
        if image and image.startswith("https://") and _visible_len(text) <= CAPTION_LIMIT:
            try:
                return self._call("sendPhoto", {
                    "chat_id": self.chat_id, "photo": image, "caption": text,
                    "parse_mode": "HTML", "reply_markup": _keyboard(button),
                })["message_id"]
            except (RuntimeError, httpx.HTTPError) as exc:
                # Telegram could not fetch/accept the image: the offer still goes out.
                log.warning("photo_failed", extra={"offer_id": offer.id, "error": str(exc)})
        return self.send_text(text, button)

    def close(self) -> None:
        self._client.close()
