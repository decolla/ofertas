"""Telegram Bot API over plain HTTP. Messages in pt-BR, HTML parse mode."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

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
    if decision.wish:
        lines.insert(0, f"💛 <b>Da sua lista:</b> {e(decision.wish)}")
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
    if decision.brand:
        lines.append(f"🏷️ Marca favorita: {e(decision.brand)}")
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
    if decision.unjudged:
        lines.append("")
        lines.append("⚠️ <i>Sem avaliação do Claude (limite de uso da assinatura): passou pelo "
                     "nosso histórico de preços, confira antes de comprar.</i>")
    if decision.insufficient_history:
        lines.append("")
        lines.append("⚠️ <i>Histórico insuficiente: baseado no desconto declarado (e na comunidade, se houver).</i>")
    lines.append("")
    lines.append(f"<i>via {e(r.source.capitalize())}</i>")
    return "\n".join(lines)


CAPTION_LIMIT = 1024  # Telegram counts the caption after HTML entities are parsed
OPEN_LABEL = "🛒 Abrir oferta"
ENDED_LABEL = "❌ Encerrada (abrir mesmo assim)"
ENDED_PREFIX = "❌ <b>Oferta encerrada</b> (segundo a fonte)\n\n"


@dataclass
class SentMessage:
    message_id: int
    text: str  # kept so the message can be edited when the deal ends
    photo: bool


def _visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _keyboard(button: tuple[str, str]) -> dict:
    return {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}


def feedback_row(offer_id: int, vote: int | None = None) -> list[dict]:
    """👍/👎 buttons; the chosen one is marked. callback_data: fb:<id>:<1|-1>."""
    return [
        {"text": ("✅ " if vote == 1 else "") + "👍 Boa", "callback_data": f"fb:{offer_id}:1"},
        {"text": ("✅ " if vote == -1 else "") + "👎 Não curti", "callback_data": f"fb:{offer_id}:-1"},
    ]


def offer_keyboard(url: str, offer_id: int, label: str = OPEN_LABEL, vote: int | None = None) -> dict:
    return {"inline_keyboard": [[{"text": label, "url": url}], feedback_row(offer_id, vote)]}


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

    def send_offer(self, offer: StoredOffer, decision: Decision) -> SentMessage:
        text = format_offer(offer, decision)
        markup = offer_keyboard(offer.raw.url, offer.id)
        image = offer.raw.image
        if image and image.startswith("https://") and _visible_len(text) <= CAPTION_LIMIT:
            try:
                mid = self._call("sendPhoto", {
                    "chat_id": self.chat_id, "photo": image, "caption": text,
                    "parse_mode": "HTML", "reply_markup": markup,
                })["message_id"]
                return SentMessage(mid, text, photo=True)
            except (RuntimeError, httpx.HTTPError) as exc:
                # Telegram could not fetch/accept the image: the offer still goes out.
                log.warning("photo_failed", extra={"offer_id": offer.id, "error": str(exc)})
        mid = self._call("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": markup,
        })["message_id"]
        return SentMessage(mid, text, photo=False)

    def close_offer(self, message_id: int, text: str, photo: bool, url: str, offer_id: int) -> None:
        """Marks an already-sent offer as ended: prefix + button label.
        A caption that would overflow keeps its text and only changes the button."""
        markup = offer_keyboard(url, offer_id, label=ENDED_LABEL)
        new = ENDED_PREFIX + text
        base = {"chat_id": self.chat_id, "message_id": message_id, "reply_markup": markup}
        if photo and _visible_len(new) > CAPTION_LIMIT:
            self._call("editMessageReplyMarkup", base)
        elif photo:
            self._call("editMessageCaption", {**base, "caption": new, "parse_mode": "HTML"})
        else:
            self._call("editMessageText", {**base, "text": new, "parse_mode": "HTML",
                                           "disable_web_page_preview": True})

    def close(self) -> None:
        self._client.close()
