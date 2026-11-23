"""Telegram Bot API over plain HTTP. Messages in pt-BR, HTML parse mode."""

from __future__ import annotations

import html
import logging

import httpx

from ofertas.models import Decision, StoredOffer

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


CATEGORY_EMOJI = {
    "hardware": "🖥️", "eletronicos": "📱", "casa": "🏠", "consumo": "🛒",
    "cozinha": "🍳", "jogos_tabuleiro": "🎲", "outros": "📦",
}


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
    lines.append(f"🏪 {e(r.store or 'Loja não informada')}")
    if r.coupon:
        lines.append(f"🎟️ Cupom: <code>{e(r.coupon)}</code>")
    if r.free_shipping:
        lines.append("🚚 Frete grátis")
    if r.votes is not None or r.temperature is not None:
        lines.append(f"👍 {r.votes or 0} likes · 🔥 {r.temperature or 0:.0f}")
    if v:
        lines.append("")
        lines.append(f"⭐ Nota <b>{v.score}</b>/100 — {e(v.reason)}")
        if v.flags:
            lines.append(f"🚩 {e(', '.join(f.replace('_', ' ') for f in v.flags))}")
    if decision.insufficient_history:
        lines.append("")
        lines.append("⚠️ <i>Histórico insuficiente: baseado no desconto declarado e na comunidade.</i>")
    lines.append("")
    lines.append(f"<i>via {e(r.source.capitalize())}</i>")
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str, chat_id: str, transport: httpx.BaseTransport | None = None):
        self.chat_id = chat_id
        self._token = token
        self._client = httpx.Client(timeout=20, transport=transport)

    def _call(self, method: str, payload: dict) -> dict:
        resp = self._client.post(API.format(token=self._token, method=method), json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} falhou: {data.get('description')}")
        return data["result"]

    def send_text(self, text: str, button: tuple[str, str] | None = None) -> int:
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if button:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": button[0], "url": button[1]}]]
            }
        return self._call("sendMessage", payload)["message_id"]

    def send_offer(self, offer: StoredOffer, decision: Decision) -> int:
        return self.send_text(format_offer(offer, decision), ("🛒 Abrir oferta", offer.raw.url))

    def close(self) -> None:
        self._client.close()
