#!/usr/bin/env bash
# Instala os units do systemd (modo usuário) apontando para este projeto.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$DEST"
for unit in ofertas.service ofertas.timer ofertas-bot.service; do
  sed "s|@ROOT@|$ROOT|g" "$ROOT/deploy/$unit" > "$DEST/$unit"
  echo "instalado: $DEST/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now ofertas.timer ofertas-bot.service
systemctl --user list-timers ofertas.timer --no-pager
echo
echo "Para continuar rodando com você deslogado (opcional):"
echo "  sudo loginctl enable-linger $USER"
