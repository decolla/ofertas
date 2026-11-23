from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from pathlib import Path

from ofertas.config import ConfigError, load_config
from ofertas.http import PoliteClient
from ofertas.logs import setup_logging
from ofertas.notify import Telegram, brl
from ofertas.runner import ConsoleNotifier, RunReport, run
from ofertas.storage import Storage

log = logging.getLogger("ofertas")


def _client(cfg) -> PoliteClient:
    return PoliteClient(
        user_agent=cfg.user_agent,
        timeout=cfg.get("http.timeout_seconds", 20),
        retries=cfg.get("http.retries", 3),
        backoff_base=cfg.get("http.backoff_base_seconds", 2),
        min_interval=cfg.get("http.min_interval_per_host_seconds", 5),
    )


def _telegram(cfg) -> Telegram:
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        raise ConfigError("TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID precisam estar no .env")
    return Telegram(cfg.telegram_token, cfg.telegram_chat_id)


def _print_report(report: RunReport) -> None:
    print("\n=== Coleta ===")
    for name, n in report.collected.items():
        print(f"  {name}: {n} ofertas")
    for name, err in report.failed.items():
        print(f"  {name}: FALHOU — {err}")
    print(f"\n=== Avaliação ({len(report.evaluated)} ofertas) ===")
    for off, d in sorted(report.evaluated, key=lambda p: not p[1].ok):
        mark = "✅" if d.ok else "❌"
        r = off.raw
        print(f"{mark} [{r.source}] {r.title[:60]} — {brl(r.price)} @ {r.store}")
        print(f"     {'; '.join(d.reasons)}")
    print(f"\n=== {len(report.sent)} mensagem(ns) {'simulada(s)' if report.sent else ''} ===")


def cmd_run(cfg, args) -> int:
    lock_path = cfg.path("lock_file")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("already_running")
            return 0
        notifier = ConsoleNotifier() if args.dry_run else _telegram(cfg)
        storage = Storage(cfg.path("database"))
        try:
            with _client(cfg) as client:
                report = run(cfg, client, storage, notifier, dry_run=args.dry_run,
                             only_source=args.source, max_notifications=args.limit)
        finally:
            storage.close()
        if args.dry_run:
            _print_report(report)
        return 0


def cmd_test_telegram(cfg, args) -> int:
    tg = _telegram(cfg)
    tg.send_text("🤖 Teste do monitor de ofertas: tudo certo por aqui!",
                 ("Abrir Telegram", "https://telegram.org"))
    print("Mensagem de teste enviada.")
    return 0


def cmd_init_db(cfg, args) -> int:
    Storage(cfg.path("database")).close()
    print(f"Banco pronto em {cfg.path('database')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ofertas", description="Monitor de ofertas → Telegram")
    p.add_argument("--config", type=Path, default=None, help="caminho do config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="coleta, avalia e notifica")
    r.add_argument("--dry-run", action="store_true", help="imprime no terminal, não envia nem grava")
    r.add_argument("--source", help="roda só esta fonte")
    r.add_argument("--limit", type=int, help="máximo de notificações nesta execução")
    sub.add_parser("test-telegram", help="envia uma mensagem de teste")
    sub.add_parser("init-db", help="cria/migra o banco")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2
    dry = getattr(args, "dry_run", False)
    setup_logging(cfg.path("log_file"), quiet=dry)
    handlers = {"run": cmd_run, "test-telegram": cmd_test_telegram, "init-db": cmd_init_db}
    try:
        return handlers[args.cmd](cfg, args)
    except ConfigError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2
