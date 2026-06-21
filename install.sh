#!/usr/bin/env bash
# Установка redmine-tools.
#   • симлинки bin/* → ~/.local/bin   (репо становится единственным источником правды)
#   • посев конфигов из config/*.example в ~/.config (существующие НЕ трогает)
# Идемпотентно. Симлинки совместимы с import-трюком скриптов (realpath(__file__) → bin/ репо).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
mkdir -p "$BIN" "$CFG/redmine-notify"

echo "→ Симлинки в $BIN"
for f in "$REPO"/bin/*; do
    name="$(basename "$f")"; target="$BIN/$name"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv "$target" "$target.bak"; echo "  бэкап: $name → $name.bak"
    fi
    ln -sfn "$f" "$target"; echo "  $name"
done

echo "→ Конфиги (существующие не перезаписываю)"
seed() { if [ -f "$2" ]; then echo "  есть:    $2"; else cp "$1" "$2"; echo "  создан:  $2  ← отредактируйте"; fi; }
seed "$REPO/config/redmine-tui.conf.example"      "$CFG/redmine-tui.conf"
seed "$REPO/config/redmine-notify.config.example" "$CFG/redmine-notify/config"
seed "$REPO/config/redmine-shift-ban.txt.example" "$CFG/redmine-shift-ban.txt"

cat <<EOF

Готово. Дальше:
  1) Впишите BASE / LIST_SOURCES в $CFG/redmine-tui.conf
  2) API-ключ:  secret-tool store --label='Redmine API' service redmine-notify
  3) Демон уведомлений (опц.):
       cp "$REPO/systemd/redmine-notify.service" ~/.config/systemd/user/
       systemctl --user enable --now redmine-notify
EOF
