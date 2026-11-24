"""Telegram command bot (long polling). Runs as its own long-lived process.

Only the configured private chat is obeyed. State lives in the `settings`
table, which the runner reads on every cycle:
  paused      "1" stops notifications (collection continues: history grows)
  categories  comma-separated enabled categories (overrides config.yaml)
  min_score   minimum Claude score (overrides criteria.min_score)
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from ofertas.categories import CATEGORY_LABELS
from ofertas.config import Config
from ofertas.notify import Telegram
from ofertas.storage import Storage, utcnow
from ofertas.triage import CATEGORIES

log = logging.getLogger(__name__)


COMMANDS = [
    ("status", "Situação do monitor"),
    ("pausar", "Pausar os avisos"),
    ("retomar", "Retomar os avisos"),
    ("categorias", "Escolher categorias"),
    ("minimo", "Ver ou mudar a nota mínima (ex.: /minimo 75)"),
    ("ajuda", "Ajuda"),
]

HELP = (
    "🍌 <b>Monitor de ofertas</b>\n\n"
    "/status — situação do monitor\n"
    "/pausar — para de enviar avisos (a coleta continua)\n"
    "/retomar — volta a enviar\n"
    "/categorias — liga/desliga categorias\n"
    "/minimo — mostra a nota mínima do Claude; /minimo 75 muda para 75\n"
    "/ajuda — esta mensagem"
)


def enabled_categories(cfg: Config, db: Storage) -> list[str]:
    raw = db.get_setting("categories")
    if raw is not None:
        return [c for c in raw.split(",") if c]
    return list(cfg.get("categories.enabled") or CATEGORIES)


def min_score(cfg: Config, db: Storage) -> int:
    raw = db.get_setting("min_score")
    return int(raw) if raw is not None else int(cfg.get("criteria.min_score", 80))


def categories_keyboard(enabled: list[str]) -> dict:
    buttons = [
        {"text": f"{'✅' if c in enabled else '⬜'} {CATEGORY_LABELS[c]}", "callback_data": f"cat:{c}"}
        for c in CATEGORIES
    ]
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def status_text(cfg: Config, db: Storage) -> str:
    today = date.today()
    used = db.claude_calls(today)
    limit = cfg.get("claude.daily_call_limit", 100)
    since = (utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
    sent = db.conn.execute("SELECT COUNT(*) FROM sent WHERE sent_at >= ?", (since,)).fetchone()[0]
    lines = [
        "📊 <b>Status</b>",
        f"Avisos: {'⏸️ pausados' if db.get_setting('paused') == '1' else '▶️ ativos'}",
        f"Nota mínima: {min_score(cfg, db)}",
        f"Categorias: {', '.join(CATEGORY_LABELS[c] for c in enabled_categories(cfg, db)) or 'nenhuma'}",
        f"Claude hoje: {used}/{limit} chamadas",
        f"Enviadas nas últimas 24h: {sent}",
        "",
        "<b>Fontes</b>",
    ]
    rows = db.conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    for r in rows:
        mark = "✅" if r["consecutive_failures"] == 0 else f"⚠️ {r['consecutive_failures']} falha(s)"
        lines.append(f"{r['source']}: {mark}")
    if not rows:
        lines.append("ainda sem execuções")
    return "\n".join(lines)


def handle_update(update: dict, cfg: Config, db: Storage, tg: Telegram) -> None:
    """Pure-ish dispatcher: all side effects go through db and tg (testable)."""
    allowed = str(cfg.telegram_chat_id)
    if "callback_query" in update:
        cq = update["callback_query"]
        chat = str(cq.get("message", {}).get("chat", {}).get("id"))
        if chat != allowed:
            log.warning("ignored_chat", extra={"chat": chat})
            return
        data = cq.get("data") or ""
        if data.startswith("cat:") and data[4:] in CATEGORIES:
            cats = enabled_categories(cfg, db)
            c = data[4:]
            cats = [x for x in cats if x != c] if c in cats else [*cats, c]
            db.set_setting("categories", ",".join(x for x in CATEGORIES if x in cats))
            tg.call("editMessageReplyMarkup", {
                "chat_id": allowed, "message_id": cq["message"]["message_id"],
                "reply_markup": categories_keyboard(enabled_categories(cfg, db)),
            })
            tg.call("answerCallbackQuery", {"callback_query_id": cq["id"],
                                            "text": f"{CATEGORY_LABELS[c]}: {'ligada' if c in cats else 'desligada'}"})
        return

    msg = update.get("message") or {}
    chat = str(msg.get("chat", {}).get("id"))
    text = (msg.get("text") or "").strip()
    if chat != allowed:
        log.warning("ignored_chat", extra={"chat": chat})
        return
    if not text.startswith("/"):
        tg.send_text("Use /ajuda para ver os comandos.")
        return
    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()

    if cmd in ("/start", "/ajuda", "/help"):
        tg.send_text(HELP)
    elif cmd == "/pausar":
        db.set_setting("paused", "1")
        tg.send_text("⏸️ Avisos pausados. A coleta continua, para o histórico seguir crescendo. /retomar para voltar.")
    elif cmd == "/retomar":
        db.set_setting("paused", "0")
        tg.send_text("▶️ Avisos retomados.")
    elif cmd == "/categorias":
        tg.call("sendMessage", {
            "chat_id": allowed, "text": "Toque para ligar/desligar:",
            "reply_markup": categories_keyboard(enabled_categories(cfg, db)),
        })
    elif cmd == "/minimo":
        if not args:
            tg.send_text(f"Nota mínima atual: <b>{min_score(cfg, db)}</b>. Para mudar: /minimo 75")
        elif args[0].isdigit() and 0 <= int(args[0]) <= 100:
            db.set_setting("min_score", args[0])
            tg.send_text(f"✅ Nota mínima agora é <b>{int(args[0])}</b>.")
        else:
            tg.send_text("Use um número de 0 a 100, ex.: /minimo 75")
    elif cmd == "/status":
        tg.send_text(status_text(cfg, db))
    else:
        tg.send_text("Comando desconhecido. /ajuda")


def run_bot(cfg: Config, db: Storage, tg: Telegram, once: bool = False) -> None:
    tg.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
    offset = int(db.get_setting("tg_offset", "0"))
    log.info("bot_started", extra={"offset": offset})
    while True:
        try:
            updates = tg.call("getUpdates", {
                "offset": offset, "timeout": 50,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=60)
        except Exception as exc:
            log.warning("get_updates_failed", extra={"error": str(exc)})
            time.sleep(10)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                handle_update(upd, cfg, db, tg)
            except Exception:
                log.exception("update_failed", extra={"update_id": upd["update_id"]})
            db.set_setting("tg_offset", str(offset))
        if once:
            return
