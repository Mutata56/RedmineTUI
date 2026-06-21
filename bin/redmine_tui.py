"""Общий движок терминальных TUI для Redmine (SUPER+I просмотр и разбор смены).

Содержит:
  • загрузку темы caelestia (~/.local/state/caelestia/scheme.json) в ANSI truecolor;
  • чтение клавиш в raw-режиме (стрелки/Enter/Esc/q);
  • REST-хелперы (ключ из secret-tool), кэш статусов;
  • подсветку SQL (pygments) под тему;
  • render_issue(): красивая ASCII-карточка заявки с полной историей.

Импортируется из redmine-view и redmine-shift-fix (оба добавляют ~/.local/bin в sys.path).
"""
import os, sys, re, json, time, subprocess, datetime, atexit, signal, textwrap, unicodedata, bisect, hashlib
import termios, tty, select, threading, http.client
import urllib.request, urllib.error, urllib.parse

# ── ширина символа в клетках терминала (эмодзи/CJK = 2) ──────────────────────
def _cwidth(ch):
    if ch < " ":
        return 0
    if unicodedata.combining(ch):
        return 0
    o = ord(ch)
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if (0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF      # эмодзи вне CJK-таблицы EAW
            or o in (0x231A, 0x231B, 0x2B50, 0x2B55)):
        return 2
    return 1

def dwidth(s):
    """Видимая ширина строки (без ANSI) в клетках терминала."""
    return sum(_cwidth(c) for c in s)

try:                              # единый конфиг (BASE, activity_id), см. ~/.config/redmine-tui.conf
    import redmine_conf
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import redmine_conf
BASE = redmine_conf.BASE
ACTIVITY_ID = redmine_conf.ACTIVITY_ID
TRANSPARENT = getattr(redmine_conf, "TRANSPARENT", True)   # фон-панель = дефолтный фон терминала (прозрачно)

# ── тема caelestia в ANSI ────────────────────────────────────────────────────
def _hex_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

def fghex(h):
    r, g, b = _hex_rgb(h)
    return f"\033[38;2;{r};{g};{b}m"

def bghex(h):
    r, g, b = _hex_rgb(h)
    return f"\033[48;2;{r};{g};{b}m"

def load_theme():
    """Палитра текущей схемы caelestia. Падает в нейтральный дефолт, если файла нет."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(state, "caelestia", "scheme.json")
    c, dark = {}, True
    try:
        with open(path, encoding="utf-8") as _f:
            s = json.load(_f)
        c = s.get("colours", {})
        dark = s.get("mode", "dark") != "light"
    except Exception:
        pass
    def col(k, default):
        v = c.get(k)
        return "#" + v if v else default
    return {
        "dark":      dark,
        "fg":        col("onSurface", "#d0d0d0" if dark else "#202020"),
        "accent":    col("primary",   "#8fc7e3"),
        "secondary": col("secondary", "#b8c8d0"),
        "tertiary":  col("tertiary",  "#d8c0ee"),
        "muted":     col("outline",   "#8b9295"),
        "error":     col("error",     "#ff7b6f"),
        "ok":        col("term2",     "#7fc77f"),
        "warn":      col("term3",     "#e0b060"),
        "line":      col("outlineVariant", col("outline", "#555a5c")),
        # панельные цвета (дизайн как у dbpick)
        "panel":     col("surfaceContainer",     "#1b1d1d" if dark else "#eef1f2"),
        "panel_hi":  col("surfaceContainerHigh", "#232626" if dark else "#e6eaeb"),
        "on_accent": col("onPrimary",            "#10242c" if dark else "#0a1a20"),
    }

T = load_theme()
RESET, BOLD, DIM, ITAL = "\033[0m", "\033[1m", "\033[2m", "\033[3m"
A    = fghex(T["accent"])
FG   = fghex(T["fg"])
SEC  = fghex(T["secondary"])
TER  = fghex(T["tertiary"])
MUT  = fghex(T["muted"])
ERR  = fghex(T["error"])
OK   = fghex(T["ok"])
WARN = fghex(T["warn"])
LINE = fghex(T["line"])

def _scheme_path():
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "caelestia", "scheme.json")

def _scheme_mtime():
    try:
        return os.path.getmtime(_scheme_path())
    except Exception:
        return 0.0

_THEME_MTIME = _scheme_mtime()

def reload_theme():
    """Перечитать схему caelestia и обновить все ANSI-цвета + подсветку (тема живёт со сменой фона)."""
    global T, A, FG, SEC, TER, MUT, ERR, OK, WARN, LINE, _HL, _THEME_MTIME
    T = load_theme()
    A, FG, SEC, TER = fghex(T["accent"]), fghex(T["fg"]), fghex(T["secondary"]), fghex(T["tertiary"])
    MUT, ERR, OK, WARN, LINE = fghex(T["muted"]), fghex(T["error"]), fghex(T["ok"]), fghex(T["warn"]), fghex(T["line"])
    _HL = _make_hl()
    _THEME_MTIME = _scheme_mtime()

def theme_changed():
    return _scheme_mtime() != _THEME_MTIME

def cols():
    return shutil_cols()

import shutil
def shutil_cols():
    return shutil.get_terminal_size((100, 40)).columns

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
def vlen(s):
    return dwidth(ANSI_RE.sub("", s))

# ── чтение клавиш (raw) ──────────────────────────────────────────────────────
_FD = sys.stdin.fileno() if sys.stdin.isatty() else None
_ORIG = termios.tcgetattr(_FD) if _FD is not None else None

def restore_term():
    if _FD is not None and _ORIG is not None:
        try:
            if "foot" in os.environ.get("TERM", ""):     # снять трекинг мыши (raw_off/atexit/$EDITOR)
                sys.stdout.write("\033[?1006l\033[?1000l"); sys.stdout.flush()
            termios.tcsetattr(_FD, termios.TCSADRAIN, _ORIG)
        except Exception:
            pass

atexit.register(restore_term)
def _sig_restore(signum, frame):
    try:                                    # при SIGTERM/SIGHUP: снять мышь/sync, вернуть курсор и осн. экран
        sys.stdout.write("\033[?1006l\033[?1000l\033[?2026l\033[?25h\033[?1049l")
        sys.stdout.flush()
    except Exception:
        pass
    restore_term()
    os._exit(0)
for _s in (signal.SIGTERM, signal.SIGHUP):
    try:
        signal.signal(_s, _sig_restore)
    except Exception:
        pass

def raw_on():
    """cbreak на весь сеанс (без эха/построчного буфера, но \\n в \\r\\n и Ctrl+C продолжают работать).
    ВАЖНО: TCSANOW, не TCSAFLUSH, иначе сбрасывается уже введённый байт (двойной нажим!)."""
    if _FD is not None:
        try:
            tty.setcbreak(_FD, termios.TCSANOW)
        except Exception:
            pass

def raw_off():
    restore_term()

# ЙЦУКЕН на QWERTY: хоткеи работают и в русской раскладке (ф как a, с как c, м как v, й как q)
_RU2EN = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбюё"
    "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
    "QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>~")

def read_key(timeout=None, translate=True):
    """Клавиша или None по таймауту. Требует cbreak. Читает полный UTF-8 символ
    и нормализует русскую раскладку в латиницу (иначе кириллица-хоткеи не ловились).
    translate=False: вернуть исходный символ (для ввода текста, напр. поиска)."""
    if _FD is None:
        return "ENTER"
    raw_on()
    if timeout is not None:
        rdy, _, _ = select.select([_FD], [], [], timeout)
        if not rdy:
            return None
    try:
        b = os.read(_FD, 1)
    except OSError:                          # hangup на Linux: чтение pty даёт EIO, не b""
        return "EOF"
    if not b:                               # EOF, терминал закрыт, не падаем на b[0]
        return "EOF"
    if b == b"\x1b":
        rdy, _, _ = select.select([_FD], [], [], 0.06)
        if not rdy:
            return "ESC"
        try:
            c1 = os.read(_FD, 1)
        except OSError:
            return "ESC"
        if c1 == b"O":                          # SS3 (стрелки в app-cursor mode): ESC O A..D
            c2 = os.read(_FD, 1).decode("latin1", "ignore")
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT",
                    "H": "HOME", "F": "END"}.get(c2, "ESC")
        if c1 != b"[":
            return "ESC"
        params = b""                            # CSI: ESC [ <params> <final 0x40..0x7e>
        while True:                             # читаем ВСЮ последовательность целиком
            ch = os.read(_FD, 1)
            if not ch:
                return "ESC"
            if 0x40 <= ch[0] <= 0x7e:
                final = ch.decode("latin1", "ignore")
                break
            params += ch
            if len(params) > 32:                # защита от мусора
                return "ESC"
        p = params.decode("latin1", "ignore")
        if p[:1] == "<" and final in ("M", "m"):    # SGR-мышь: ESC[<btn;col;row M|m
            try:
                bn, x, y = (int(v) for v in p[1:].split(";"))
            except Exception:
                return "ESC"
            return ("MOUSE", bn, x, y, final == "M")   # press=True для 'M', release для 'm'
        if final == "~":                            # ESC[n~ : PgUp5 PgDn6 Home1/7 End4/8
            return {"5": "PGUP", "6": "PGDN", "1": "HOME", "7": "HOME",
                    "4": "END", "8": "END"}.get(p.split(";")[0], "ESC")
        # стрелки/Home/End: финальный байт A/B/C/D/H/F. С модификаторами (ESC[1;5C) params
        # игнорируем, но они уже вычитаны из буфера, так что фантомных нажатий не будет.
        return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT",
                "H": "HOME", "F": "END"}.get(final, "ESC")
    if b in (b"\r", b"\n"):
        return "ENTER"
    if b == b"\x03":
        raise KeyboardInterrupt
    o = b[0]                                     # дочитать хвост UTF-8 (кириллица = 2 байта)
    if o >= 0xF0:
        b += os.read(_FD, 3)
    elif o >= 0xE0:
        b += os.read(_FD, 2)
    elif o >= 0xC0:
        b += os.read(_FD, 1)
    ch = b.decode("utf-8", "ignore")
    return ch.translate(_RU2EN) if (ch and translate) else ch

CLEAR = "\033[2J\033[3J\033[H"
SYNC_BEG = "\033[?2026h"          # синхронизированный вывод (foot): кадр рисуется атомарно, без мерцания
SYNC_END = "\033[?2026l"
def clear():
    sys.stdout.write(CLEAR); sys.stdout.flush()

# ── мышь (foot, SGR-1006): клики/отпускания/колесо ───────────────────────────
HAVE_MOUSE = (_FD is not None and "foot" in os.environ.get("TERM", ""))
MOUSE_ON  = "\033[?1000h\033[?1006h"      # 1000=клики, 1006=SGR-кодировка координат
MOUSE_OFF = "\033[?1006l\033[?1000l"
def mouse_on():
    """Включить трекинг мыши. Вызывать ОДИН раз при входе/возврате в TUI (не в read_key!)."""
    if HAVE_MOUSE:
        sys.stdout.write(MOUSE_ON); sys.stdout.flush()
def mouse_off():
    """Выключить трекинг перед $EDITOR / галереей / cooked-вводом / выходом (иначе ломается
    выделение текста и сыплется мусор ESC[<…M). Уже встроено в restore_term()."""
    if HAVE_MOUSE:
        sys.stdout.write(MOUSE_OFF); sys.stdout.flush()

# ── REST ─────────────────────────────────────────────────────────────────────
def get_key():
    try:                              # secret-tool может отсутствовать, тогда не валим импорт модуля
        return subprocess.run(["secret-tool", "lookup", "service", "redmine-notify"],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""

_KEY = None
def key():
    """API-ключ с ЛЕНИВОЙ инициализацией: secret-tool дёргается при первом обращении, не при импорте."""
    global _KEY
    if _KEY is None:
        _KEY = get_key()
    return _KEY

def __getattr__(name):                # обратная совместимость: R.KEY отдаёт ленивый ключ
    if name == "KEY":
        return key()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

class RedmineClient:
    """REST-клиент Redmine с keep-alive: на каждый поток своё TCP/TLS-соединение,
    при ошибке соединения делается один переподключ. Ключ берётся лениво через key()."""

    def __init__(self):
        self._tls = threading.local()      # своё соединение на поток

    def _conn(self):
        c = getattr(self._tls, "c", None)
        if c is None:
            c = http.client.HTTPSConnection(urllib.parse.urlsplit(BASE).netloc, timeout=30)
            self._tls.c = c
        return c

    def api(self, method, path, payload=None):
        """REST-запрос. Возвращает (status, body). Соединение переиспользуется в рамках
        потока, в худшем случае это то же новое соединение на запрос, что и у urllib."""
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"X-Redmine-API-Key": key(), "Content-Type": "application/json", "Accept": "application/json"}
        last = "?"
        for _attempt in (1, 2):
            c = self._conn()
            try:
                c.request(method, path, body=data, headers=headers)
                r = c.getresponse()
                body = r.read().decode("utf-8", "replace")   # дочитать обязательно, иначе соединение не переиспользовать
                return r.status, body
            except Exception as e:
                last = str(e)
                try:
                    c.close()
                except Exception:
                    pass
                self._tls.c = None                            # сбросить и пойти на вторую попытку с новым соединением
        return 0, last

    def get_issue(self, issue_id):
        code, body = self.api("GET", f"/issues/{issue_id}.json?include=journals,attachments,relations")
        if code != 200:
            return None
        try:
            return json.loads(body)["issue"]
        except Exception:
            return None


_client = RedmineClient()
# Старые имена сохраняем: движок и фронтенды зовут api()/get_issue() напрямую.
api = _client.api
get_issue = _client.get_issue


class StatusCache:
    """{id: name} статусов заявок. Суточный дисковый кэш плюс мемоизация в памяти
    по mtime, чтобы не читать json на каждый кадр рендера."""
    PATH = os.path.expanduser("~/.cache/redmine-menu/statuses.json")

    def __init__(self):
        self._memo = {"mt": -1.0, "data": None}

    def get(self):
        try:
            mt = os.path.getmtime(self.PATH)
            if time.time() - mt < 86400:
                if self._memo["mt"] == mt and self._memo["data"] is not None:
                    return self._memo["data"]
                with open(self.PATH) as _f:
                    d = {int(k): v for k, v in json.load(_f).items()}
                self._memo.update(mt=mt, data=d)
                return d
        except Exception:
            pass
        code, body = api("GET", "/issue_statuses.json")
        m = {}
        if code == 200:
            try:
                m = {s["id"]: s["name"] for s in json.loads(body)["issue_statuses"]}
                os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
                tmp = self.PATH + ".tmp"
                with open(tmp, "w") as _f:
                    json.dump({str(k): v for k, v in m.items()}, _f)
                os.replace(tmp, self.PATH)                      # атомарно: оборванная запись не оставит битый кэш
                self._memo.update(mt=os.path.getmtime(self.PATH), data=m)
            except Exception:
                pass
        return m


class UserCache:
    """{id(str): «Имя Фамилия»} всех пользователей, суточный кэш. Пустой словарь,
    если /users.json запрещён (403): тогда в этой сессии сервер больше не дёргаем."""
    PATH = os.path.expanduser("~/.cache/redmine-menu/users.json")

    def __init__(self):
        self._memo = {"mt": -1.0, "data": None}   # user_name() зовётся на каждую запись истории
        self._failed = False

    def get(self):
        try:
            mt = os.path.getmtime(self.PATH)
            if time.time() - mt < 86400:
                if self._memo["mt"] == mt and self._memo["data"] is not None:
                    return self._memo["data"]
                with open(self.PATH) as _f:
                    d = json.load(_f)
                self._memo.update(mt=mt, data=d)
                return d
        except Exception:
            pass
        if self._failed:                 # уже получили 403 в этой сессии, не долбим сервер
            return {}
        code, body = api("GET", "/users.json?limit=100&offset=0")   # первая страница, заодно узнаём total_count
        if code != 200:
            self._failed = True
            return {}
        try:
            d = json.loads(body)
        except Exception:
            return {}
        pages = [d.get("users", [])]
        offs = list(range(100, d.get("total_count", 0), 100))        # остальные страницы тянем параллельно
        if offs:
            import concurrent.futures
            def _pg(o):
                c, b = api("GET", f"/users.json?limit=100&offset={o}")
                try:
                    return json.loads(b).get("users", []) if c == 200 else []
                except Exception:
                    return []
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(offs))) as ex:
                pages += list(ex.map(_pg, offs))
        m = {}
        for us in pages:
            for u in us:
                nm = (f"{u.get('firstname','')} {u.get('lastname','')}".strip()
                      or u.get("login") or str(u.get("id")))
                m[str(u["id"])] = nm
        if m:
            try:
                os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
                tmp = self.PATH + ".tmp"
                with open(tmp, "w") as _f:
                    json.dump(m, _f)
                os.replace(tmp, self.PATH)                      # атомарно: оборванная запись не оставит битый кэш
                self._memo.update(mt=os.path.getmtime(self.PATH), data=m)
            except Exception:
                pass
        return m

    def name(self, uid, involved=None):
        """Имя по id: сперва из «причастных» к заявке (без запросов), затем из общего
        кэша всех юзеров, иначе «#id». involved это {id(str): name}."""
        if uid in (None, "", "0", 0):
            return "—"
        uid = str(uid)
        if involved and involved.get(uid):
            return involved[uid]
        return self.get().get(uid) or f"#{uid}"


_status_cache = StatusCache()
_user_cache = UserCache()
# Старые имена сохраняем для рендера и фронтендов.
status_map = _status_cache.get
users_map = _user_cache.get
user_name = _user_cache.name

# ── подсветка SQL (pygments, под тему) ───────────────────────────────────────
def _make_hl():
    try:
        from pygments import highlight as _hl
        from pygments.lexers import SqlLexer
        from pygments.formatters import TerminalTrueColorFormatter
        from pygments.style import Style
        from pygments.token import Keyword, Name, String, Comment, Token
        acc, ter, mut, fg = T["accent"], T["tertiary"], T["muted"], T["fg"]
        class _S(Style):
            # минимализм под тему: всё обычный текст темы; выделяем только
            # ключевые слова (акцент, жирный), строки (третичный), комментарии (приглушённо)
            styles = {
                Token:        fg,
                Keyword:      f"bold {acc}",
                Name.Builtin: f"bold {acc}",
                String:       ter,
                Comment:      f"italic {mut}",
            }
        fmt, lex = TerminalTrueColorFormatter(style=_S), SqlLexer()
        return lambda block: _hl(block, lex, fmt).rstrip("\n")
    except Exception:
        return None

_HL = _make_hl()
_KW = r"SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|GROUP BY|ORDER BY|HAVING|CREATE|ALTER|DROP|EXEC|MERGE|TRUNCATE|UNION"
_KW_RE = re.compile(_KW, re.I)
_SELFROM = re.compile(r"\bSELECT\b.*\bFROM\b", re.I)

_RE_INLINE_IMG = re.compile(r'!([^!\n]{1,200})!')
_RE_INLINE_LINK = re.compile(r'"([^"\n]+)":(\S+)')
_RE_INLINE_BOLD = re.compile(r'(?<![\w*])\*([^*\n]{1,80})\*(?![\w*])')
_RE_INLINE_ITAL = re.compile(r'(?<![\w_])_([^_\n]{1,80})_(?![\w_])')
_RE_INLINE_CODE = re.compile(r'(?<![\w@])@([^@\n]{1,80})@(?![\w@])')
_RE_INLINE_UNDER = re.compile(r'(?<![\w+])\+([^+\n]{1,80})\+(?![\w+])')
_RE_COLLAPSE = re.compile(r'^\s*\{\{collapse(?:\((.*?)\))?\s*(.*)$', re.I)
_RE_CODE_OPEN = re.compile(r'^\s*(<pre>|```\w*|~~~|bc\.\s?)(.*)$', re.I)
_RE_HD = re.compile(r'^h([1-6])\.\s+(.*)$')
_RE_BQ = re.compile(r'^bq\.\s+(.*)$')
_RE_HR = re.compile(r'^\s*(-{3,}|_{3,}|={3,})\s*$')
_RE_LIST = re.compile(r'^(\s*)([*#]+)\s+(.*)$')
_RE_SQL_TAG = re.compile(r'</?code\b[^>]*>', re.I)
_RE_FENCE_PRE = re.compile(r'(<pre>\n?)(.*?)(\n?</pre>)', re.S | re.I)
_RE_FENCE_TICK = re.compile(r'(```\w*\n)(.*?)(\n```)', re.S)
_RE_ANSI_SPLIT = re.compile(r'(\033\[[0-9;]*m)')
_RE_CTRL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
_RE_STRIP_PRE = re.compile(r'</?pre>|```\w*')

def _looks_sql(line):
    return bool(_SELFROM.search(line)) or len(_KW_RE.findall(line)) >= 2

def _block_looks_sql(text):
    """Похож ли весь блок на SQL, для многострочных запросов (SELECT и FROM на РАЗНЫХ строках:
    построчная эвристика их не ловит). Склеиваем в одну строку и ищем SELECT…FROM / ≥2 ключевых."""
    flat = " ".join(text.split())
    return bool(_SELFROM.search(flat)) or len(_KW_RE.findall(flat)) >= 2

def _hl_fallback(block):
    # без pygments просто жирним ключевые слова
    return re.sub(_KW, lambda m: f"{BOLD}{A}{m.group(0)}{RESET}", block, flags=re.I)

def highlight_sql(text):
    """Подсвечивает SQL-блоки/строки в свободном тексте; прозу не трогает."""
    if not text:
        return ""
    hl = _HL or _hl_fallback
    # сначала явная разметка Redmine: <pre>…</pre> и ```…```
    def _fence(m):
        return m.group(1) + hl(m.group(2)) + m.group(3)
    text = _RE_FENCE_PRE.sub(_fence, text)
    text = _RE_FENCE_TICK.sub(_fence, text)
    # затем эвристика по строкам: смежные SQL-строки в один блок
    out, buf = [], []
    def flush():
        if buf:
            out.append(hl("\n".join(buf)))
            buf.clear()
    for ln in text.split("\n"):
        if "\033" in ln:            # строка уже подсвечена (fence или повторный вызов), не трогаем,
            flush()                 # иначе pygments токенизирует сами ESC-байты и выходит мусор «\x1b\x1b[39m»
            out.append(ln)
        elif _looks_sql(ln):
            buf.append(ln)
        else:
            flush()
            out.append(ln)
    flush()
    return "\n".join(out)

# ── текстовые помощники ──────────────────────────────────────────────────────
def clean(s):
    return _RE_CTRL.sub("", s or "")

def strip_redmine(s):
    # лёгкая чистка вики-разметки/html для прозы (код-блоки уже подсвечены отдельно)
    s = clean(s)
    s = _RE_STRIP_PRE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s

def wrap_prose(text, width, indent=""):
    import textwrap
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        # строку с подсветкой (есть ANSI) не переносим, пусть терминал сам
        if "\033[" in para:
            out.append(indent + para)
            continue
        for ln in textwrap.wrap(para, max(20, width - len(indent))) or [""]:
            out.append(indent + ln)
    return out

def rel_date(iso):
    if not iso:
        return ""
    try:
        t = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return iso
    # ISO в UTC переводим в локальное «дд.мм.гг чч:мм»
    t = t.replace(tzinfo=datetime.timezone.utc).astimezone()
    return t.strftime("%d.%m.%y %H:%M")

def _due_label(d):
    """('дд.мм.гг (+пометка)', цвет) для срока. d='YYYY-MM-DD'. Просрочен красим в ERR, скоро в WARN."""
    try:
        due = datetime.datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return d, FG
    delta = (due - datetime.date.today()).days
    s = due.strftime("%d.%m.%y")
    if delta < 0:
        return f"{s}  (просрочен на {-delta} дн.)", ERR
    if delta == 0:
        return f"{s}  (сегодня)", WARN
    if delta == 1:
        return f"{s}  (завтра)", WARN
    if delta <= 3:
        return f"{s}  (через {delta} дн.)", WARN
    return s, FG

def render_hint(pairs):
    """Строка-подсказка для футеров: ключи accent+bold, подписи MUT, разделитель ' · '.
    pairs = [(key, label), …]. Единый стиль для всех TUI."""
    sep = f"{MUT} · {RESET}"
    return sep.join(f"{A}{BOLD}{k}{RESET} {MUT}{lbl}{RESET}" for k, lbl in pairs)

def ansi_wrap(line, width):
    """Переносит одну (возможно цветную) строку по ширине, сохраняя SGR-коды."""
    if width < 8:
        width = 8
    out, cur, vis, active = [], "", 0, ""
    for p in _RE_ANSI_SPLIT.split(line):
        if not p:
            continue
        if p.startswith("\033["):
            cur += p
            active = "" if p in ("\033[0m", RESET) else active + p
            continue
        for ch in p:
            w = _cwidth(ch)
            if vis + w > width:
                out.append(cur + RESET)
                cur, vis = active + ch, w
            else:
                cur += ch
                vis += w
    out.append(cur + (RESET if active else ""))
    return out

def pager(lines, hint="←/→ страницы · ↑/↓ строки · Enter — далее · q — выход", rerender=None, title="Redmine"):
    """Постраничный просмотр в общей панели-«скелете» (как список или карточка): листание страниц стрелками влево-вправо,
    снизу «страница N/M». Возвращает клавишу выхода. rerender() отдаёт свежие строки при смене темы."""
    if _FD is None:               # не tty (тест), просто печатаем всё
        print("\n".join(lines))
        return "ENTER"
    cur = lines
    def build(ls):
        w = min(cols() - 4, 130)
        out = []
        for ln in ls:
            out += [ln] if vlen(ln) <= w else ansi_wrap(ln, w)
        return out
    vis = build(cur)
    pg = 0
    while True:
        c, r = shutil.get_terminal_size((100, 40))
        CW = min(c - 4, 130)
        body = max(4, (r - 4) - 2)                                      # тело панели (минус разделитель+футер)
        pages = max(1, -(-len(vis) // body))                           # ceil
        pg = max(0, min(pg, pages - 1))
        chunk = vis[pg * body:pg * body + body]
        inner = [ansi_to_cells(ln, CW, T["fg"], T["panel"]) for ln in chunk]
        while len(inner) < body:
            inner.append(crow(CW, T["fg"], T["panel"]))
        sep = crow(CW, T["line"], T["panel"]); cstamp(sep, 0, "─" * CW, T["line"], T["panel"]); inner.append(sep)
        inner.append(ansi_to_cells(f"  {MUT}страница {pg + 1}/{pages}   ·   {hint}{RESET}", CW, T["muted"], T["panel"]))
        panel_screen(title, inner, CW)
        k = read_key(timeout=5)
        if k is None:                          # тик: сменилась ли тема?
            if theme_changed():
                reload_theme()
                if rerender:
                    cur = rerender()
                vis = build(cur)
            continue
        if isinstance(k, tuple):               # мышь
            if k[1] & 64:                      # колесо листает страницы
                pg = min(pages - 1, pg + 1) if (k[1] & 1) else max(0, pg - 1)
            continue
        if k in ("RIGHT", "DOWN", "j", "PGDN", " "):
            pg = min(pages - 1, pg + 1)
        elif k in ("LEFT", "UP", "k", "PGUP"):
            pg = max(0, pg - 1)
        elif k in ("g", "HOME"):
            pg = 0
        elif k in ("G", "END"):
            pg = pages - 1
        elif k in ("ENTER", "ESC", "EOF", "q", "Q", "r", "R", "v", "V", "c", "C", "a", "A",
                   "p", "P", "s", "S", "t", "T", "o", "O") or (k and k.isdigit()):
            return k

# ── клеточный буфер + центрированная панель (дизайн как у dbpick) ────────────--
class _Cell:
    __slots__ = ("ch", "fg", "bg", "bold")
    def __init__(self, ch, fg, bg, bold):
        self.ch, self.fg, self.bg, self.bold = ch, fg, bg, bold

def crow(w, fg, bg):
    return [_Cell(" ", fg, bg, False) for _ in range(w)]

def cstamp(row, x, text, fg, bg, bold=False):
    p = x
    for ch in text:
        if 0 <= p < len(row):
            row[p] = _Cell(ch, fg, bg, bold)
        w = _cwidth(ch)
        if w == 2 and 0 <= p + 1 < len(row):
            row[p + 1] = _Cell("", fg, bg, bold)   # клетка-продолжение широкого символа
        p += w if w else 1

def cfill(row, x0, x1, bg):
    for p in range(max(0, x0), min(len(row), x1)):
        row[p].bg = bg

def _csgr(fg, bg, bold):
    r, g, b = _hex_rgb(fg)
    head = f"\033[{'1' if bold else '22'};38;2;{r};{g};{b}"   # 1=жирный, 22=снять жирность явно, иначе жирность течёт по строке
    if TRANSPARENT and (bg == T["panel"] or bg == T["panel_hi"]):   # фон-панель ставим в 49 (дефолт терминала)
        return head + ";49m"                                       # так видно прозрачность foot, как в консоли
    rr, gg, bb = _hex_rgb(bg)
    return head + f";48;2;{rr};{gg};{bb}m"

def crender(row):
    out, pf, pb, pbd = [], None, None, None
    for c in row:
        if (c.fg, c.bg, c.bold) != (pf, pb, pbd):
            out.append(_csgr(c.fg, c.bg, c.bold)); pf, pb, pbd = c.fg, c.bg, c.bold
        out.append(c.ch)
    out.append(RESET)
    return "".join(out)

def ansi_to_cells(s, width, default_fg, bg, bold=False):
    """ANSI-строку (fg/bg/bold, truecolor) в ряд из `width` клеток. Так любой готовый цветной
    текст (вкл. фон-полоски) кладётся в общий клеточный «скелет» (panel_screen)."""
    cells, fg, bgc, bd = [], default_fg, bg, bold
    for part in _RE_ANSI_SPLIT.split(s or ""):
        if not part:
            continue
        if part.startswith("\033["):
            codes = part[2:-1].split(";")
            k = 0
            while k < len(codes):
                c = codes[k].lstrip("0") or "0"           # pygments шлёт «01»/«00»/«39», снимаем ведущие нули,
                nxt = (codes[k + 1].lstrip("0") or "0") if k + 1 < len(codes) else ""  # иначе bold/reset терялись
                if c == "0":
                    fg, bgc, bd = default_fg, bg, bold
                elif c == "1":
                    bd = True
                elif c == "22":
                    bd = False
                elif c == "39":
                    fg = default_fg
                elif c == "49":
                    bgc = bg
                elif c == "38" and k + 4 < len(codes) and nxt == "2":
                    fg = "#%02x%02x%02x" % (int(codes[k + 2]), int(codes[k + 3]), int(codes[k + 4]))
                    k += 4
                elif c == "48" and k + 4 < len(codes) and nxt == "2":
                    bgc = "#%02x%02x%02x" % (int(codes[k + 2]), int(codes[k + 3]), int(codes[k + 4]))
                    k += 4
                k += 1                                # 3/4/24/23 игнорируем (нет в клеточной модели)
            continue
        for ch in part:
            w = _cwidth(ch)
            if len(cells) + (2 if w == 2 else 1) > width:   # широкий символ целиком не влезает, не рвём его пополам
                break
            cells.append(_Cell(ch, fg, bgc, bd))
            if w == 2:
                cells.append(_Cell("", fg, bgc, bd))
    while len(cells) < width:
        cells.append(_Cell(" ", default_fg, bg, False))
    return cells[:width]

def panel_screen(title, inner, cw):
    """Кадрирует список cell-строк (ширины cw) в центрированную панель с фоном."""
    c, r = shutil.get_terminal_size((100, 40))
    border, panel = T["line"], T["panel"]
    box_w, box_h = cw + 2, len(inner) + 2
    out = [SYNC_BEG + "\033[2J\033[H"]
    if c < box_w or r < box_h:
        out.append(f"\033[2;3HОкно маловато — растяни терминал (нужно ~{box_w}x{box_h}).")
        sys.stdout.write("".join(out) + SYNC_END); sys.stdout.flush(); return
    ox, oy = max(0, (c - box_w) // 2), max(0, (r - box_h) // 2)

    def bar(lch, rch):
        row = crow(box_w, border, panel)
        row[0] = _Cell(lch, border, panel, False)
        for i in range(1, box_w - 1):
            row[i] = _Cell("─", border, panel, False)
        row[box_w - 1] = _Cell(rch, border, panel, False)
        return row
    top = bar("╭", "╮"); cstamp(top, 3, f" {title} ", T["accent"], panel, True)
    bottom = bar("╰", "╯")

    def place(y, row):
        out.append(f"\033[{y};{ox + 1}H"); out.append(crender(row))
    place(oy + 1, top)
    for i, rw in enumerate(inner):
        full = crow(box_w, border, panel)
        full[0] = _Cell("│", border, panel, False)
        for j, cell in enumerate(rw[:cw]):
            full[1 + j] = cell
        full[box_w - 1] = _Cell("│", border, panel, False)
        place(oy + 2 + i, full)
    place(oy + 2 + len(inner), bottom)
    sys.stdout.write("".join(out) + SYNC_END); sys.stdout.flush()

def _fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return ""
    for u in ("Б", "КБ", "МБ"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "Б" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ГБ"

# ── картинки-вложения в sixel через chafa (ПЕРСИСТЕНТНЫЙ кэш ~/.cache) ─────────
import glob as _glob
_IMG_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "redmine-img")
_SIXEL_DIR = os.path.join(_IMG_DIR, "sixel")
os.makedirs(_IMG_DIR, exist_ok=True)
os.makedirs(_SIXEL_DIR, exist_ok=True)
for _f in _glob.glob(os.path.join(_IMG_DIR, "*")):     # удаляем файлы старше ~36 часов
    try:
        if _f.startswith(_SIXEL_DIR):
            continue
        if time.time() - os.path.getmtime(_f) > 36 * 3600:
            os.remove(_f)
    except Exception:
        pass
for _f in _glob.glob(os.path.join(_SIXEL_DIR, "*")):  # sixel-кэш: TTL 1.5 дня (36ч)
    try:
        if time.time() - os.path.getmtime(_f) > 36 * 3600:
            os.remove(_f)
    except Exception:
        pass

HAVE_CHAFA = shutil.which("chafa") is not None
SIXEL_OK = HAVE_CHAFA and "foot" in os.environ.get("TERM", "")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg")

def is_image(a):
    ct = (a.get("content_type") or "").lower()
    fn = (a.get("filename") or "").lower()
    return ct.startswith("image/") or fn.endswith(IMG_EXT)

def download_att(a):
    url = a.get("content_url")
    if not url:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", a.get("filename") or "img")
    path = os.path.join(_IMG_DIR, f"{a.get('id')}_{safe}")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        req = urllib.request.Request(url, headers={"X-Redmine-API-Key": key()})
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception:
        return None

def image_sixel(path, cols, rows):
    """Конвертация изображения в sixel через chafa с disk-кэшем (TTL 36ч)."""
    try:
        h = hashlib.sha256(path.encode()).hexdigest()[:16]
        cache = os.path.join(_SIXEL_DIR, f"{h}_{cols}x{rows}.sixel")
        if os.path.exists(cache) and os.path.getsize(cache) > 0:
            with open(cache, "rb") as f:
                return f.read()
        r = subprocess.run(["chafa", "-f", "sixel", "--size", f"{cols}x{rows}", "--animate=off", path],
                           capture_output=True, timeout=25)
        if r.stdout:
            try:
                with open(cache, "wb") as f:
                    f.write(r.stdout)
            except Exception:
                pass
        return r.stdout
    except Exception:
        return b""

def status_color(name):
    n = (name or "").lower()
    if "закры" in n or "решен" in n:
        return OK
    if "ожида" in n:
        return WARN
    if "работ" in n or "процесс" in n or "нов" in n:
        return A
    return FG

# ── рендер карточки заявки ───────────────────────────────────────────────────
def _inline(s):
    """Инлайн-разметка Textile/Redmine: !картинка! *жирный* _курсив_ @код@ "ссылка":url."""
    s = clean(s)
    s = _RE_INLINE_IMG.sub(lambda m: f'{MUT}🖼{RESET}', s)
    s = _RE_INLINE_LINK.sub(lambda m: f'\033[4m{A}{m.group(1)}{RESET} {MUT}{m.group(2)}{RESET}', s)
    s = _RE_INLINE_BOLD.sub(lambda m: f'{BOLD}{m.group(1)}{RESET}', s)
    s = _RE_INLINE_ITAL.sub(lambda m: f'{ITAL}{m.group(1)}{RESET}', s)
    s = _RE_INLINE_CODE.sub(lambda m: f'{TER}{m.group(1)}{RESET}', s)
    s = _RE_INLINE_UNDER.sub(lambda m: f'\033[4m{m.group(1)}{RESET}', s)
    return s

def _emit_code(out, indent, title, block, collapse, width=80):
    """Свёрнутый (⊕ title (N стр.)) или развёрнутый (▾ + подсветка SQL) код-блок."""
    body = [b.expandtabs(4) for b in block]   # табы в пробелы: иначе таб=0 клеток в рендере и колонки съезжают
    n = len([b for b in body if b.strip()]) or len(body)
    if collapse:
        out.append(f"{indent}{A}⊕ {clean(title)}{RESET} {MUT}({n} стр.) · c — развернуть{RESET}")
    else:
        out.append(f"{indent}{A}{BOLD}▾ {clean(title)}{RESET}")
        gutter = f"{indent}{A}▏{RESET} "
        cw = max(20, width - len(indent) - 2)
        joined = "\n".join(body)                  # код-блок заведомо код, если это SQL, красим целиком
        hl = _HL or _hl_fallback                  # (иначе многострочный SELECT…FROM подсветится частично)
        rendered = hl(joined) if _block_looks_sql(joined) else highlight_sql(joined)
        for cl in rendered.split("\n"):
            wrapped = ansi_wrap(cl, cw) if vlen(cl) > cw else [cl]
            for wln in wrapped:
                out.append(gutter + wln)

def render_markup(text, width, indent="  ", collapse=True):
    """Textile/Redmine-разметка в список ANSI-строк. collapse=True: {{collapse}}, код-блоки и
    опознанный SQL сворачиваются в «⊕ title (N стр.)» (разворот клавишей c)."""
    out = []
    if not text:
        return out
    lines = clean(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    iw = max(20, width - len(indent))
    i, ln_n = 0, len(lines)
    while i < ln_n:
        ln = lines[i]
        mco = _RE_COLLAPSE.match(ln)
        if mco:                                      # {{collapse(Title) … }}
            title = (mco.group(1) or "свёрнутый блок").strip() or "свёрнутый блок"
            block, rest = [], mco.group(2)
            if rest.strip() and "}}" not in rest:
                block.append(rest)
            i += 1
            while i < ln_n and "}}" not in lines[i]:
                block.append(lines[i]); i += 1
            if i < ln_n:
                tail = lines[i].split("}}")[0]
                if tail.strip():
                    block.append(tail)
                i += 1
            block = [_RE_SQL_TAG.sub("", _RE_STRIP_PRE.sub("", b)) for b in block]  # снять <pre>/<code class=sql>
            while block and not block[0].strip():
                block.pop(0)
            while block and not block[-1].strip():
                block.pop()
            _emit_code(out, indent, title, block, collapse, iw)
            continue
        mopen = _RE_CODE_OPEN.match(ln)
        if mopen:                                    # код-блок <pre>/```/bc.
            opener, rest, block = mopen.group(1).lower(), mopen.group(2), []
            if opener.startswith("bc."):
                if rest.strip():
                    block.append(rest)
                i += 1
                while i < ln_n and lines[i].strip() != "":
                    block.append(lines[i]); i += 1
            else:
                close = "</pre>" if "pre" in opener else ("```" if "`" in opener else "~~~")
                if rest.strip():
                    block.append(rest)
                i += 1
                while i < ln_n and close not in lines[i].lower():
                    block.append(lines[i]); i += 1
                i += 1
            block = [_RE_SQL_TAG.sub("", b) for b in block]   # убрать <code class=sql>
            while block and not block[0].strip():
                block.pop(0)
            while block and not block[-1].strip():
                block.pop()
            _emit_code(out, indent, "код", block, collapse, iw)
            continue
        mh = _RE_HD.match(ln)
        if mh:                                       # заголовок
            lvl, txt = int(mh.group(1)), _inline(mh.group(2).strip())
            out.append("")
            if lvl <= 2:
                out.append(f"{indent}{A}{BOLD}▌ {txt}{RESET}")
                out.append(f"{indent}{A}{'─' * min(iw, max(6, vlen(txt) + 2))}{RESET}")
            else:
                out.append(f"{indent}{A}{BOLD}▸ {txt}{RESET}")
            i += 1; continue
        mq = _RE_BQ.match(ln)
        if mq:                                       # цитата
            for piece in (textwrap.wrap(clean(mq.group(1)), iw - 2) or [""]):
                out.append(f"{indent}{MUT}┃ {ITAL}{_inline(piece)}{RESET}")
            i += 1; continue
        if _RE_HR.match(ln):
            out.append(f"{indent}{MUT}{'─' * iw}{RESET}"); i += 1; continue
        ml = _RE_LIST.match(ln)
        if ml:                                       # список
            depth = len(ml.group(2)); pad = "  " * (depth - 1)
            bullet = "•" if "*" in ml.group(2) else "‣"
            wrapped = textwrap.wrap(clean(ml.group(3)), max(10, iw - 2 - len(pad))) or [""]
            out.append(f"{indent}{pad}{A}{bullet}{RESET} {_inline(wrapped[0])}")
            for piece in wrapped[1:]:
                out.append(f"{indent}{pad}  {_inline(piece)}")
            i += 1; continue
        if ln.strip() == "":
            out.append(""); i += 1; continue
        if _looks_sql(ln):                           # неявный SQL прячем в свёрнутый блок
            block = []
            while i < ln_n and lines[i].strip() and _looks_sql(lines[i]):
                block.append(lines[i]); i += 1
            _emit_code(out, indent, "SQL-запрос", block, collapse, iw)
            continue
        for piece in (textwrap.wrap(clean(ln), iw) or [""]):   # проза + инлайн-разметка
            out.append(f"{indent}{_inline(piece)}")
        i += 1
    return out

def _cf(issue):
    return {c.get("name", ""): (c.get("value") or "") for c in issue.get("custom_fields", [])}

def _cf_by_id(issue):
    return {str(c.get("id")): c.get("name", str(c.get("id"))) for c in issue.get("custom_fields", [])}

def _involved(issue):
    """{id(str): name} причастных к заявке (автор, исполнитель, авторы истории), без запросов."""
    m = {}
    for who in (issue.get("author"), issue.get("assigned_to")):
        if who and who.get("id"):
            m[str(who["id"])] = who.get("name")
    for j in issue.get("journals", []) or []:
        u = j.get("user") or {}
        if u.get("id"):
            m[str(u["id"])] = u.get("name")
    return m

def _humanize_detail(d, cf_names, st, unames=None):
    prop, name = d.get("property"), str(d.get("name"))
    ov, nv = d.get("old_value"), d.get("new_value")
    if prop == "attr" and name == "status_id":
        a = st.get(int(ov), ov) if ov else "—"
        b = st.get(int(nv), nv) if nv else "—"
        return f"статус: {a} → {b}"
    if prop == "attr" and name == "assigned_to_id":
        return f"исполнитель: {user_name(ov, unames)} → {user_name(nv, unames)}"
    if prop == "attr" and name == "done_ratio":
        return f"готовность: {ov or 0}% → {nv or 0}%"
    if prop == "attr" and name == "subject":
        return "тема изменена"
    if prop == "cf":
        label = cf_names.get(name, f"поле {name}")
        a = (ov or "∅").strip() or "∅"
        b = (nv or "∅").strip() or "∅"
        return f"{label}: {a} → {b}"
    if prop == "attachment":
        return f"вложение: {nv or ov}"
    return f"{name}: {ov} → {nv}"

def _attach_map(issue):
    """Возвращает ({journal_id:[att_id...]}, [initial_atts]). Привязывает вложение к записи истории
    по детали property=attachment, иначе по близкому created_on (картинки в комментах без детали)."""
    atts = issue.get("attachments", []) or []
    journals = issue.get("journals", []) or []
    def ts(s):
        try:
            return datetime.datetime.strptime(s or "", "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
    j_of = {}
    for j in journals:
        for d in j.get("details", []):
            if d.get("property") == "attachment" and d.get("new_value"):
                j_of[str(d.get("name"))] = j.get("id")
    jts = [(ts(j.get("created_on")), j.get("id")) for j in journals]
    jts_sorted = sorted([(jt, jid) for jt, jid in jts if jt is not None], key=lambda x: x[0])
    ts_only = [t for t, _ in jts_sorted]
    for a in atts:
        aid = str(a.get("id"))
        if aid in j_of:
            continue
        at = ts(a.get("created_on"))
        if at is None:
            continue
        pos = bisect.bisect_left(ts_only, at)
        best, bd = None, None
        for idx in (pos - 1, pos):
            if 0 <= idx < len(jts_sorted):
                jt, jid = jts_sorted[idx]
                d = abs((jt - at).total_seconds())
                if d <= 120 and (bd is None or d < bd):
                    best, bd = jid, d
        if best is not None:
            j_of[aid] = best
    per = {}
    for aid, jid in j_of.items():
        per.setdefault(jid, []).append(aid)
    for jid in per:
        per[jid].sort(key=lambda x: int(x))
    initial = [a for a in atts if str(a.get("id")) not in j_of]
    return per, initial

def render_issue_items(issue, st=None, width=None, collapse=True):
    """Список элементов карточки: str (строка) или ("img", attachment, caption_lines)."""
    if st is None:
        st = status_map()
    w = min((width or cols()) - 1, 108)
    cf = _cf(issue)
    cf_names = _cf_by_id(issue)
    unames = _involved(issue)
    L = []

    def rule(ch="─"):
        L.append(LINE + ch * w + RESET)
    def label_val(lbl, val, vcolor=FG):
        if val is None or str(val).strip() == "":
            return
        L.append(f"  {MUT}{lbl:<22}{RESET}{vcolor}{val}{RESET}")

    iid = issue.get("id")
    subj = clean(issue.get("subject", ""))
    tracker = (issue.get("tracker") or {}).get("name", "")
    rule("━")
    if issue.get("is_private"):                      # приватная заявка, полоса в цвет темы
        bar = " ПРИВАТНАЯ ЗАЯВКА · видна только сотрудникам"
        bw = max(0, w - 2)
        L.append("  " + BOLD + fghex(T["on_accent"]) + bghex(T["accent"]) + bar[:bw].ljust(bw) + RESET)
    L.append(f"  {A}{BOLD}{tracker} #{iid}{RESET}")
    L.append(f"  {BOLD}{FG}{subj}{RESET}")
    author = (issue.get("author") or {}).get("name", "—")
    L.append(f"  {MUT}добавил(а) {RESET}{SEC}{author}{RESET}{MUT}  ·  создана {rel_date(issue.get('created_on'))}"
             f"  ·  обновлена {rel_date(issue.get('updated_on'))}{RESET}")
    rule("━")

    # атрибуты
    stt = (issue.get("status") or {}).get("name", "")
    label_val("Статус:", stt, status_color(stt))
    due = issue.get("due_date")
    if due:
        dtxt, dcol = _due_label(due)
        label_val("Срок:", dtxt, dcol)
    label_val("Приоритет:", (issue.get("priority") or {}).get("name"))
    label_val("Назначена:", (issue.get("assigned_to") or {}).get("name"), SEC)
    label_val("Проект:", (issue.get("project") or {}).get("name"), TER)
    label_val("Я-трекер:", cf.get("Я-трекер"), A)
    label_val("Поток:", cf.get("Поток"))
    label_val("Причина обращения:", cf.get("Причина обращения"), TER)
    label_val("Способ решения:", cf.get("Способ решения"), TER)
    label_val("SLA реакция:", cf.get("SLA реакция"))
    label_val("SLA решение:", cf.get("SLA решение"))
    label_val("Время проблемы:", cf.get("Время возникновения проблемы"))
    sh = issue.get("spent_hours") or 0
    label_val("Трудозатраты:", (f"{sh:.2f} ч" if sh else "—"), OK if sh else ERR)
    label_val("Готовность:", f"{issue.get('done_ratio', 0)}%")
    # прочие непустые кастомные поля, которые не показали выше
    shown = {"Я-трекер", "Поток", "Причина обращения", "Способ решения", "SLA реакция",
             "SLA решение", "Время возникновения проблемы"}
    for k, v in cf.items():
        if k and k not in shown and str(v).strip() and str(v) not in ("0",):
            label_val(k + ":", v)

    # вложения: индекс по id + привязка к записям истории (по детали ИЛИ по времени)
    att = issue.get("attachments", []) or []
    att_by_id = {str(a.get("id")): a for a in att}
    per_journal, initial_atts = _attach_map(issue)
    det_names = {str(d.get("name")): d.get("new_value")          # имена даже для удалённых вложений
                 for j in issue.get("journals", []) for d in j.get("details", [])
                 if d.get("property") == "attachment" and d.get("new_value")}

    img_no = [0]
    def att_line(att_id, filename, indent):
        a = att_by_id.get(str(att_id))
        nm = clean(filename or (a.get("filename") if a else "") or det_names.get(str(att_id), "") or "файл")
        size = f"  {MUT}({_fmt_size(a['filesize'])}){RESET}" if (a and a.get("filesize")) else ""
        if a and is_image(a):
            img_no[0] += 1
            caption = [f"{indent}{TER}📎 [{img_no[0]}] {nm}{RESET}{size}  {MUT}({img_no[0]} — открыть){RESET}"]
            if a.get("content_url"):
                caption.append(f"{indent}   {MUT}{a['content_url']}{RESET}")
            L.append(("img", a, caption))          # картинка открывается в галерее по цифре
        else:
            caption = [f"{indent}{TER}📎 {nm}{RESET}{size}"]
            if a and a.get("content_url"):
                caption.append(f"{indent}   {MUT}{a['content_url']}{RESET}")
            L.extend(caption)

    # описание (Textile/Redmine-разметка)
    desc_raw = issue.get("description", "") or ""
    L.append("")
    L.append(f"  {A}{BOLD}Описание{RESET}")
    rule()
    if desc_raw.strip():
        L += render_markup(desc_raw, w, indent="  ", collapse=collapse)
    else:
        L.append(f"  {MUT}(пусто){RESET}")
    if initial_atts:                                   # вложения при создании
        L.append("")
        L.append(f"  {A}{BOLD}Вложения{RESET} {MUT}({len(initial_atts)}){RESET}")
        for a in initial_atts:
            att_line(a.get("id"), a.get("filename", ""), "  ")

    # история: записи с заметкой, изменениями или вложениями; комментарии нумеруются
    journals = [j for j in issue.get("journals", [])
                if (j.get("notes") or j.get("details") or per_journal.get(j.get("id")))]
    L.append("")
    L.append(f"  {A}{BOLD}История{RESET} {MUT}({len(journals)}){RESET}")
    rule()
    if not journals:
        L.append(f"  {MUT}(пусто){RESET}")
    cnum = 0
    for j in journals:
        who = (j.get("user") or {}).get("name", "—")
        when = rel_date(j.get("created_on"))
        is_priv = bool(j.get("private_notes"))
        priv = f"  {WARN}{BOLD} внутренний{RESET}" if is_priv else ""
        num = ""
        if (j.get("notes") or "").strip():             # номер ставим только у комментариев с текстом
            cnum += 1
            num = f"{A}{BOLD}#{cnum}{RESET} "
        L.append("")
        L.append(f"  {num}{TER}▸ {who}{RESET}{MUT}  ·  {when}{RESET}{priv}")
        for d in j.get("details", []):                 # изменения (вложения идут отдельно ниже)
            if d.get("property") == "attachment":
                if not d.get("new_value"):
                    L.append(f"      {MUT}🗑 удалил вложение: {clean(d.get('old_value') or '')}{RESET}")
            else:
                L.append(f"      {MUT}• {_humanize_detail(d, cf_names, st, unames)}{RESET}")
        notes = j.get("notes", "") or ""
        if notes.strip():
            note_lines = render_markup(notes, w, indent="      ", collapse=collapse)
            if is_priv:                                # приватные помечаем цветным gutter ▎ на каждой строке
                note_lines = [re.sub(r"^      ", f"    {WARN}▎{RESET} ", x, count=1) for x in note_lines]
            L += note_lines
        for aid in per_journal.get(j.get("id"), []):   # вложения этой записи (картинки коммента)
            att_line(aid, "", "      ")
    return L

def render_issue(issue, st=None, width=None, collapse=True):
    """ANSI-строка карточки (картинки идут как подпись 📎); для pager/shift-fix."""
    out = []
    for it in render_issue_items(issue, st, width, collapse):
        if isinstance(it, tuple):
            out += it[2]
        else:
            out.append(it)
    return "\n".join(out)

def print_issue(issue):
    """Печатает карточку В ПОТОК (обычный экран): текст + инлайн sixel-картинки вложений."""
    items = render_issue_items(issue)
    if SIXEL_OK:                                   # скачать ВСЕ картинки параллельно заранее
        imgs = [it[1] for it in items if isinstance(it, tuple) and it[0] == "img"]
        if imgs:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(download_att, imgs))
    c, _ = shutil.get_terminal_size((100, 40))
    iw = min(c - 2, 100)
    for it in items:
        if isinstance(it, tuple) and it[0] == "img":
            _, att, caption = it
            for cl in caption:
                print(cl)
            if SIXEL_OK:
                p = download_att(att)              # уже в кэше, отдаём мгновенно
                if p:
                    sx = image_sixel(p, min(iw, 72), 20)
                    if sx:
                        sys.stdout.buffer.write(sx)
                        sys.stdout.buffer.flush()
                        print()
        else:
            print(it)

def gallery(imgs, start=0):
    """Полноэкранная «модалка» картинок: одна во весь экран, листание стрелками влево-вправо, Esc или q закрывают."""
    if _FD is None or not imgs:
        return
    mouse_off()                       # поверх sixel клики не парсим; колесо вернётся как стрелки
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:   # прогреть кэш
        list(ex.map(download_att, imgs))
    sx_cache = {}                     # (path, cols, rows) в sixel-байты (чтобы не гонять chafa на каждое нажатие)
    if SIXEL_OK and len(imgs) > 1:    # фоном параллельно конвертим sixel заранее, тогда листание без лагов
        _c0, _r0 = shutil.get_terminal_size((100, 40))
        _dims = (_c0 - 2, _r0 - 4)
        def _warm():
            def _conv(a):
                p = download_att(a)
                ck = (p, _dims[0], _dims[1])
                if p and ck not in sx_cache:
                    try:
                        sx_cache[ck] = image_sixel(p, _dims[0], _dims[1])
                    except Exception:
                        pass
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as wex:
                list(wex.map(_conv, imgs))
        threading.Thread(target=_warm, daemon=True).start()
    cur = max(0, min(start, len(imgs) - 1))
    while True:
        c, r = shutil.get_terminal_size((100, 40))
        clear()
        a = imgs[cur]
        sys.stdout.write(f"  {A}{BOLD}🖼  {clean(a.get('filename',''))}{RESET}"
                         f"  {MUT}[{cur+1}/{len(imgs)}] ({_fmt_size(a.get('filesize',0))}){RESET}\n\n")
        p = download_att(a)
        shown = False
        if p and SIXEL_OK:
            ckey = (p, c - 2, r - 4)
            sx = sx_cache.get(ckey)
            if sx is None:
                sx = image_sixel(p, c - 2, r - 4)    # во весь экран
                sx_cache[ckey] = sx
            if sx:
                sys.stdout.buffer.write(sx); sys.stdout.buffer.flush(); shown = True
        if not shown:
            sys.stdout.write(f"  {ERR}не удалось показать картинку{RESET}\n"
                             f"  {MUT}{a.get('content_url','')}{RESET}\n")
        sys.stdout.write(f"\n{MUT}  ←/→ листать ({cur+1}/{len(imgs)}) · Esc/q — закрыть{RESET}")
        sys.stdout.flush()
        k = read_key()
        if isinstance(k, tuple):                  # мышь (если вдруг прилетела): колесо листает, клик закрывает
            if k[1] & 64:
                cur = (cur + 1) % len(imgs) if (k[1] & 1) else (cur - 1) % len(imgs)
            elif not k[4]:
                return
            continue
        if k in ("RIGHT", "DOWN", "j", "PGDN", " "):
            cur = (cur + 1) % len(imgs)
        elif k in ("LEFT", "UP", "k", "PGUP"):
            cur = (cur - 1) % len(imgs)
        elif k in ("ESC", "q", "Q", "ENTER", "EOF"):
            return

def add_comment(issue_id, text, private=False):
    """Добавить комментарий (journal note). private=True делает внутреннюю (приватную) запись,
    клиент её не видит и не получает уведомление. Возвращает (ok, msg)."""
    issue = {"notes": text}
    if private:
        issue["private_notes"] = True
    code, body = api("PUT", f"/issues/{issue_id}.json", {"issue": issue})
    return code == 204, ("OK" if code == 204 else f"HTTP {code} {body.strip()[:200]}")

def _clip_text():
    """Текст из буфера обмена (только text/plain, не картинку). '' если нет/ошибка."""
    try:
        r = subprocess.run(["wl-paste", "-t", "text/plain", "--no-newline"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""

def _external_edit(initial=""):
    """Открыть $EDITOR на временном файле с initial; вернуть итоговый текст (initial при ошибке)."""
    if _FD is None:
        return initial
    import tempfile
    editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
              or shutil.which("nano") or shutil.which("vim") or "vi")
    fd, path = tempfile.mkstemp(prefix="rmcomment-", suffix=".md")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(initial or "")
        restore_term()                                   # отдать терминал редактору
        try:
            subprocess.call([editor, path])
        except Exception:
            pass
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return initial
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

def compose_comment(issue, prefill=""):
    """Сплит-редактор комментария: слева сырой текст (Textile/Redmine-разметка), справа живой
    предпросмотр (как отрендерится в карточке: заголовки, *жирный*, списки, подсветка SQL …).
    Возвращает текст или None при отмене. Ctrl+D готово, Esc отмена, Ctrl+E внешний $EDITOR, стрелки/Home/End/PgUp/PgDn."""
    if _FD is None:
        return None
    lines = (prefill or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines:
        lines = [""]
    cy, cx, vtop = len(lines) - 1, len(lines[-1]), 0
    iid, subj = issue.get("id"), clean(issue.get("subject", ""))
    pcache = {"key": None, "rows": []}                   # превью пересобираем только при ИЗМЕНЕНИИ текста
    raw_on(); mouse_off()                                # cbreak + без мыши на время ввода

    def draw():
        nonlocal vtop
        c, r = shutil.get_terminal_size((100, 40))
        CW = min(c - 4, 160)
        body = max(6, (r - 4) - 2)                       # минус разделитель + футер
        lw = max(20, (CW - 3) * 45 // 100)               # ~45% ширины под редактор, остальное под превью
        rw = CW - lw - 3
        if cy < vtop:                                    # вертикальный скролл: курсор всегда виден
            vtop = cy
        elif cy >= vtop + body:
            vtop = cy - body + 1
        hoff = cx - lw + 1 if cx >= lw else 0            # горизонтальный сдвиг строки курсора
        joined = "\n".join(lines)
        if pcache["key"] != (joined, rw):                # пересборка превью только когда текст изменился
            pv = (render_markup(joined, rw, indent="", collapse=False)
                  if joined.strip() else [f"{MUT}(пусто — здесь будет предпросмотр){RESET}"])
            pcache.update(key=(joined, rw), rows=pv)
        preview = pcache["rows"]
        inner = []
        for i in range(body):
            row = crow(CW, T["fg"], T["panel"])
            li = vtop + i
            if li < len(lines):                          # левая колонка: сырой текст
                off = hoff if li == cy else 0
                for j, cell in enumerate(ansi_to_cells(lines[li][off:off + lw], lw, T["fg"], T["panel"])):
                    row[j] = cell
            row[lw] = _Cell("│", T["line"], T["panel"], False)
            pv_line = preview[i] if i < len(preview) else ""   # правая колонка: предпросмотр
            for j, cell in enumerate(ansi_to_cells(pv_line, rw, T["fg"], T["panel"])):
                row[lw + 2 + j] = cell
            inner.append(row)
        ci, cc = cy - vtop, min(lw - 1, cx - hoff)       # курсор это клетка с акцентным фоном
        if 0 <= ci < body and 0 <= cc < lw:
            ch = lines[cy][cx] if cx < len(lines[cy]) else " "
            inner[ci][cc] = _Cell(ch if ch.strip() else " ", T["on_accent"], T["accent"], True)
        sep = crow(CW, T["line"], T["panel"]); cstamp(sep, 0, "─" * CW, T["line"], T["panel"]); inner.append(sep)
        over = f"   {WARN}▾ превью +{len(preview) - body} стр.{RESET}" if len(preview) > body else ""
        hint = render_hint([("Ctrl+D", "отправить"), ("Esc", "отмена"), ("Ctrl+P", "SQL из буфера"),
                            ("Ctrl+E", "внешний ред."), ("↑↓←→ Home/End", "курсор")])
        inner.append(ansi_to_cells("  " + hint + over, CW, T["muted"], T["panel"]))
        panel_screen(f"Комментарий к #{iid}: {subj[:46]}   —   слева пишешь · справа предпросмотр", inner, CW)

    while True:
        if not select.select([_FD], [], [], 0)[0]:       # рисуем, когда ввод «отдышался» (вставка не лагает)
            draw()
        k = read_key(translate=False)                    # без ЙЦУКЕН-нормализации, печатаем как набрано
        if k in ("EOF", "ESC"):
            return None
        if isinstance(k, tuple):                         # мышь игнорируем в редакторе
            continue
        if k == "\x04":                                  # Ctrl+D, завершить ввод
            return "\n".join(lines).strip("\n")
        if k == "\x05":                                  # Ctrl+E, внешний $EDITOR (тяжёлая правка)
            new = _external_edit("\n".join(lines))
            lines = (new.replace("\r\n", "\n").replace("\r", "\n").split("\n")) if new is not None else lines
            if not lines:
                lines = [""]
            cy = min(cy, len(lines) - 1); cx = min(cx, len(lines[cy])); pcache["key"] = None
            raw_on(); mouse_off()
            continue
        if k == "\x10":                                  # Ctrl+P, вставить SQL-блок из буфера в позицию курсора
            clip = _clip_text().strip("\n")
            bl = ('<pre><code class="sql">\n' + clip + '\n</code></pre>').split("\n")
            head, tail = lines[cy][:cx], lines[cy][cx:]
            if len(bl) == 1:
                lines[cy] = head + bl[0] + tail; cx = len(head) + len(bl[0])
            else:
                lines[cy] = head + bl[0]
                for off, nl in enumerate(bl[1:-1] + [bl[-1] + tail], 1):
                    lines.insert(cy + off, nl)
                cy += len(bl) - 1; cx = len(bl[-1])
            pcache["key"] = None
            continue
        if k == "LEFT":
            if cx > 0: cx -= 1
            elif cy > 0: cy -= 1; cx = len(lines[cy])
        elif k == "RIGHT":
            if cx < len(lines[cy]): cx += 1
            elif cy < len(lines) - 1: cy += 1; cx = 0
        elif k == "UP":
            if cy > 0: cy -= 1; cx = min(cx, len(lines[cy]))
        elif k == "DOWN":
            if cy < len(lines) - 1: cy += 1; cx = min(cx, len(lines[cy]))
        elif k == "PGUP":
            cy = max(0, cy - 10); cx = min(cx, len(lines[cy]))
        elif k == "PGDN":
            cy = min(len(lines) - 1, cy + 10); cx = min(cx, len(lines[cy]))
        elif k == "HOME":
            cx = 0
        elif k == "END":
            cx = len(lines[cy])
        elif k == "ENTER":                               # перенос строки
            tail = lines[cy][cx:]; lines[cy] = lines[cy][:cx]
            lines.insert(cy + 1, tail); cy += 1; cx = 0
        elif k in ("\x7f", "\x08"):                      # Backspace
            if cx > 0:
                lines[cy] = lines[cy][:cx - 1] + lines[cy][cx:]; cx -= 1
            elif cy > 0:
                pcx = len(lines[cy - 1]); lines[cy - 1] += lines[cy]
                del lines[cy]; cy -= 1; cx = pcx
        elif isinstance(k, str) and (k == "\t" or (len(k) >= 1 and k >= " ")):   # печать / вставка
            lines[cy] = lines[cy][:cx] + k + lines[cy][cx:]; cx += len(k)
        # прочие управляющие игнорируем

def add_comment_ui(issue, paste_sql=False):
    """Комментарий через сплит-редактор с живым предпросмотром. paste_sql=True префиллит SQL из буфера."""
    if _FD is None:
        return False
    iid = issue.get("id")
    prefill = ""
    if paste_sql:
        clip = _clip_text().strip("\n")
        prefill = '<pre><code class="sql">\n' + (clip or "") + '\n</code></pre>\n\n'
    text = compose_comment(issue, prefill)
    if not text or not text.strip():
        raw_on()
        return False
    text = text.strip()
    clear(); raw_off()                                   # cooked для подтверждения
    print(f"  {A}{BOLD}Будет записано в #{iid}:{RESET}\n")
    for l in text.split("\n"):
        print(f"    {l}")
    print(f"\n  {MUT}Enter — записать (обычный) · {RESET}{ERR}i — внутренний (приватный){RESET}"
          f"{MUT} · n — отмена{RESET}")
    try:                                                 # ввод нормализуем (рус. раскладка в лат.)
        c = input("  > ").strip().translate(_RU2EN).lower()
    except (EOFError, KeyboardInterrupt):
        c = "n"
    if c.startswith("n"):                                # n / т(рус) = отмена
        raw_on()
        return False
    private = c.startswith("i")                          # i / ш(рус) = внутренний
    ok, msg = add_comment(iid, text, private=private)
    tag = f" {ERR}(внутренний){RESET}" if private else ""
    print(f"\n  {OK}✓ комментарий{tag}{OK} записан{RESET}" if ok else f"\n  {ERR}✗ ошибка: {msg}{RESET}")
    time.sleep(1.0 if ok else 2.5)
    raw_on()
    return ok

# ── смена статуса / трудозатраты (запись только после явного подтверждения) ────
def _yes(prompt):
    """Cooked-подтверждение: True только при явном «да» (y/да/д/yes/ok); рус. раскладка ок."""
    try:
        c = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return c in ("y", "yes", "yep", "ok", "да", "д", "ага") or c.translate(_RU2EN).startswith("y")

def mins_to_hm(m):
    return f"{m // 60}:{m % 60:02d}"

# частые статусы для быстрого меню (id из /issue_statuses.json)
STATUS_QUICK = [4, 3, 2, 49, 1, 8, 22, 5, 6]
# 4 Ожидание ответа от клиента · 3 Решена · 2 В работе · 49 Новая инфо · 1 Новая
# 8 Отложена · 22 Приостановлено · 5 Закрыта · 6 Отменена

def set_status(issue_id, status_id):
    """Сменить статус заявки. Возвращает (ok, msg)."""
    code, body = api("PUT", f"/issues/{issue_id}.json", {"issue": {"status_id": int(status_id)}})
    return code == 204, ("OK" if code == 204 else f"HTTP {code} {body.strip()[:200]}")

def add_time(issue_id, mins, comments=""):
    """Списать трудозатраты (activity из конфига). Возвращает (ok, msg)."""
    te = {"issue_id": int(issue_id), "hours": mins_to_hm(int(mins)),
          "activity_id": ACTIVITY_ID, "comments": comments or ""}
    code, body = api("POST", "/time_entries.json", {"time_entry": te})
    return code == 201, ("OK" if code == 201 else f"HTTP {code} {body.strip()[:200]}")

def change_status_ui(issue):
    """Меню смены статуса (быстрый список частых статусов). Пишем только после явного «да»."""
    if _FD is None:
        return False
    iid = issue.get("id")
    cur = issue.get("status") or {}
    cur_id = cur.get("id")
    sm = status_map()
    opts = [(sid, sm.get(sid, str(sid))) for sid in STATUS_QUICK if sid in sm]
    raw_off(); clear()
    print(f"  {A}{BOLD}Смена статуса #{iid}{RESET}")
    print(f"  {MUT}{clean(issue.get('subject',''))[:70]}{RESET}")
    print(f"  {MUT}текущий: {RESET}{status_color(cur.get('name'))}{cur.get('name','—')}{RESET}\n")
    for n, (sid, nm) in enumerate(opts, 1):
        mark = f"  {MUT}← текущий{RESET}" if sid == cur_id else ""
        print(f"    {A}{n}{RESET}. {status_color(nm)}{nm}{RESET}{mark}")
    print(f"    {DIM}Enter — отмена{RESET}")
    try:
        sel = input("\n  статус № → ").strip()
    except (EOFError, KeyboardInterrupt):
        sel = ""
    if not (sel.isdigit() and 1 <= int(sel) <= len(opts)):
        raw_on(); return False
    sid, nm = opts[int(sel) - 1]
    if sid == cur_id:
        print(f"  {MUT}заявка уже в этом статусе{RESET}"); time.sleep(1.0); raw_on(); return False
    if not _yes(f"\n  {WARN}Перевести #{iid} → «{nm}»?{RESET} [да / Enter — нет]: "):
        raw_on(); return False
    ok, msg = set_status(iid, sid)
    print(f"\n  {OK}✓ статус → {nm}{RESET}" if ok else f"\n  {ERR}✗ ошибка: {msg}{RESET}")
    time.sleep(1.0 if ok else 2.5)
    raw_on()
    return ok

def add_time_ui(issue):
    """Меню списания трудозатрат: пресеты 5/10/15/20/30/60 мин или своё. Пишем после «да»."""
    if _FD is None:
        return False
    iid = issue.get("id")
    spent = issue.get("spent_hours") or 0
    presets = [("1", 5), ("2", 10), ("3", 15), ("4", 20), ("5", 30), ("6", 60)]
    pm = dict(presets)
    raw_off(); clear()
    print(f"  {A}{BOLD}Трудозатраты #{iid}{RESET}")
    print(f"  {MUT}{clean(issue.get('subject',''))[:70]}{RESET}")
    print(f"  {MUT}уже списано: {RESET}{OK if spent else MUT}{spent:.2f} ч{RESET}\n")
    print("    " + "   ".join(f"{A}{k}{RESET}) {m}м" for k, m in presets))
    print(f"    {A}c{RESET}) своё (минуты или H:MM)    {DIM}Enter — отмена{RESET}")
    try:
        sel = input("\n  время → ").strip().translate(_RU2EN).lower()
    except (EOFError, KeyboardInterrupt):
        sel = ""
    mins = None
    if sel in pm:
        mins = pm[sel]
    elif sel == "c":
        try:
            raw = input("  минуты или H:MM → ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        if ":" in raw:
            try:
                h, mm = raw.split(":", 1)
                mins = int(h) * 60 + int(mm)
            except Exception:
                mins = None
        elif raw.isdigit():
            mins = int(raw)
    if not mins or mins <= 0:
        raw_on(); return False
    if not _yes(f"\n  Списать {mins_to_hm(mins)} ({mins} мин) в #{iid}? [да / Enter — нет]: "):
        raw_on(); return False
    ok, msg = add_time(iid, mins)
    print(f"\n  {OK}✓ списано {mins_to_hm(mins)}{RESET}" if ok else f"\n  {ERR}✗ ошибка: {msg}{RESET}")
    time.sleep(1.0 if ok else 2.5)
    raw_on()
    return ok

# ── черновик правок: набрать значения без отправки, затем отправить одним заходом ─
def pick_comment(issue, paste_sql=False):
    """Сочинить комментарий для черновика, БЕЗ отправки. Возвращает (text, private) или None."""
    if _FD is None:
        return None
    prefill = ""
    if paste_sql:
        clip = _clip_text().strip("\n")
        prefill = '<pre><code class="sql">\n' + (clip or "") + '\n</code></pre>\n\n'
    text = compose_comment(issue, prefill)
    if not text or not text.strip():
        raw_on()
        return None
    text = text.strip()
    clear(); raw_off()
    print(f"  {A}{BOLD}Комментарий в черновик #{issue.get('id')}:{RESET}\n")
    for l in text.split("\n"):
        print(f"    {l}")
    print(f"\n  {MUT}Enter — обычный · {RESET}{ERR}i — внутренний (приватный){RESET}{MUT} · n — отмена{RESET}")
    try:
        c = input("  > ").strip().translate(_RU2EN).lower()
    except (EOFError, KeyboardInterrupt):
        c = "n"
    raw_on()
    if c.startswith("n"):
        return None
    return text, c.startswith("i")

def pick_status(issue):
    """Выбрать статус для черновика, БЕЗ отправки. Возвращает (status_id, name) или None."""
    if _FD is None:
        return None
    cur = issue.get("status") or {}
    cur_id = cur.get("id")
    sm = status_map()
    opts = [(sid, sm.get(sid, str(sid))) for sid in STATUS_QUICK if sid in sm]
    raw_off(); clear()
    print(f"  {A}{BOLD}Статус в черновик #{issue.get('id')}{RESET}")
    print(f"  {MUT}{clean(issue.get('subject',''))[:70]}{RESET}")
    print(f"  {MUT}текущий: {RESET}{status_color(cur.get('name'))}{cur.get('name','—')}{RESET}\n")
    for n, (sid, nm) in enumerate(opts, 1):
        mark = f"  {MUT}← текущий{RESET}" if sid == cur_id else ""
        print(f"    {A}{n}{RESET}. {status_color(nm)}{nm}{RESET}{mark}")
    print(f"    {DIM}Enter — отмена{RESET}")
    try:
        sel = input("\n  статус № → ").strip()
    except (EOFError, KeyboardInterrupt):
        sel = ""
    raw_on()
    if not (sel.isdigit() and 1 <= int(sel) <= len(opts)):
        return None
    sid, nm = opts[int(sel) - 1]
    return (sid, nm) if sid != cur_id else None

def pick_time(issue):
    """Выбрать трудозатраты (минуты) для черновика, БЕЗ отправки. Возвращает mins или None."""
    if _FD is None:
        return None
    presets = [("1", 5), ("2", 10), ("3", 15), ("4", 20), ("5", 30), ("6", 60)]
    pm = dict(presets)
    spent = issue.get("spent_hours") or 0
    raw_off(); clear()
    print(f"  {A}{BOLD}Трудозатраты в черновик #{issue.get('id')}{RESET}")
    print(f"  {MUT}{clean(issue.get('subject',''))[:70]}{RESET}")
    print(f"  {MUT}уже списано: {RESET}{OK if spent else MUT}{spent:.2f} ч{RESET}\n")
    print("    " + "   ".join(f"{A}{k}{RESET}) {m}м" for k, m in presets))
    print(f"    {A}c{RESET}) своё (минуты или H:MM)    {DIM}Enter — отмена{RESET}")
    try:
        sel = input("\n  время → ").strip().translate(_RU2EN).lower()
    except (EOFError, KeyboardInterrupt):
        sel = ""
    mins = None
    if sel in pm:
        mins = pm[sel]
    elif sel == "c":
        try:
            raw = input("  минуты или H:MM → ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        if ":" in raw:
            try:
                h, mm = raw.split(":", 1)
                mins = int(h) * 60 + int(mm)
            except Exception:
                mins = None
        elif raw.isdigit():
            mins = int(raw)
    raw_on()
    return mins if (mins and mins > 0) else None

def submit_draft(issue_id, draft):
    """Отправить черновик: комментарий и статус уходят ОДНИМ PUT /issues/ID.json,
    трудозатраты отдельным POST /time_entries.json (общего эндпоинта у Redmine нет).
    Возвращает {'issue': (ok, msg) | None, 'time': (ok, msg) | None}; None означает,
    что этого в черновике не было. Вызывающий чистит в черновике только то, что ушло."""
    res = {"issue": None, "time": None}
    patch = {}
    if draft.get("note"):
        patch["notes"] = draft["note"]
        if draft.get("private"):
            patch["private_notes"] = True
    if draft.get("status_id"):
        patch["status_id"] = int(draft["status_id"])
    if patch:
        code, body = api("PUT", f"/issues/{issue_id}.json", {"issue": patch})
        res["issue"] = (code == 204, "OK" if code == 204 else f"HTTP {code} {body.strip()[:150]}")
    if draft.get("mins"):
        te = {"issue_id": int(issue_id), "hours": mins_to_hm(int(draft["mins"])),
              "activity_id": ACTIVITY_ID, "comments": ""}
        code, body = api("POST", "/time_entries.json", {"time_entry": te})
        res["time"] = (code == 201, "OK" if code == 201 else f"HTTP {code} {body.strip()[:150]}")
    return res
