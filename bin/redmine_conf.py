"""Единый конфиг redmine-* инструментов: BASE URL и activity_id.

Источник истины это ~/.config/redmine-tui.conf (KEY=VALUE, тот же файл можно `source` в bash).
Модуль НАМЕРЕННО лёгкий и без побочных эффектов (не лезет в secret-tool / тему / termios),
поэтому его безопасно импортировать даже из standalone-скриптов (redmine-list и т.п.).
Если файла нет, берутся дефолты ниже.
"""
import os

_DEFAULTS = {"BASE": "https://redmine.example.com", "ACTIVITY_ID": "35", "TRANSPARENT": "1"}

def _load():
    cfg = dict(_DEFAULTS)
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "redmine-tui.conf")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg

_C = _load()
BASE = _C["BASE"].rstrip("/")
try:
    ACTIVITY_ID = int(_C.get("ACTIVITY_ID", 35))
except (TypeError, ValueError):
    ACTIVITY_ID = 35
# Прозрачный фон панелей (как у foot): фон-панель рендерится дефолтным фоном терминала. 0/false/no/off отключают.
TRANSPARENT = str(_C.get("TRANSPARENT", "1")).strip().lower() not in ("0", "false", "no", "off", "")
# Источники для redmine-list (меню Super+I): query-строки Redmine, разделитель "||".
# Реальные значения в ~/.config/redmine-tui.conf (В КАВЫЧКАХ: содержат & и ||, файл также `source`-ится из bash).
LIST_SOURCES = [s.strip() for s in _C.get("LIST_SOURCES", "").split("||") if s.strip()]
