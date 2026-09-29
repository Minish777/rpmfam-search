#!/usr/bin/env python3
"""rpmfam-search — поиск по списку зарегистрированных фамилий RPM North.

Данные читаются из Google Docs через публичный экспорт в txt и кэшируются
локально. Утилита ничего не изменяет в документе и никуда не отправляет данные.

Проект написан с помощью ИИ и может содержать ошибки.
"""

from __future__ import annotations

import argparse
import base64
from html import unescape

import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import rpname

APP = "rpmfam-search"

# Документ не выбирается по умолчанию: на первом запуске утилита сама
# спрашивает. Этот ID — только предложение в списке при настройке.
__version__ = "1.3.2"

# репозиторий, откуда берём обновления
REPO = "Minish777/rpmfam-search"
# Через API, а не raw.githubusercontent.com: raw отдаётся из CDN и может
# показывать старую версию даже с cache-busting, а API всегда свежий.
API_FILE_URL = (f"https://api.github.com/repos/{REPO}/contents/"
                f"rpmfam_search.py?ref=main")
UPDATE_INTERVAL = 12 * 3600   # как часто проверять обновления
RE_VERSION = re.compile(r'^__version__\s*=\s*["\'](.+?)["\']', re.M)


SUGGESTED_DOC_ID = "1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA"
SUGGESTED_LABEL = "RPM North"


CACHE_TTL = 300          # через сколько секунд кеш считается протухшим
NEW_WINDOW_DAYS = 7      # окно "новых фамилий" по умолчанию

ROLE_HEAD = "Глава"

# Пороги, по которым документ признаётся реестром фамилий.
MIN_SURNAMES = 20
MIN_NOTES = 20
MIN_ROLES = 5

RE_ROLE_LINE = re.compile(
    r"^\s*(Глава|Заместитель|Владелец|Основатель)\s*[-–—]\s*(.+?)\s*$", re.I
)
# тот же шаблон, но построчно — для подсчёта по всему документу
RE_ROLE_ANY = re.compile(
    r"^[ \t]*(?:Глава|Заместитель|Владелец|Основатель)[ \t]*[-–—][ \t]*\S",
    re.I | re.M
)
RE_FIELD = re.compile(
    r"^\s*(?:номер|номре|номер\s)?\s*([а-яa-z ]{3,20}?)\s*[-–—]\s*(.+?)\s*$", re.I
)
RE_MARKER = re.compile(r"^\s*\[([a-z]{1,2})\]", re.I)
RE_REF = re.compile(r"относ\w*\s+к\s+(.+?)\s*$", re.I)
RE_ANYONE = re.compile(r"подойдет\s+любой", re.I)
RE_CAVEAT = re.compile(r"при\s+условии", re.I)
RE_DIGITS = re.compile(r"^\d{4,}$")
RE_PASSPORT_OK = re.compile(r"^RPM-[0-9A-Za-z]{4,}$", re.I)

# Временные пометки по отдельным фамилиям: фамилия -> почему нельзя выдавать.
# Пока фамилия есть в документе, запрет показывается. Уберут фамилию из
# документа — предупреждение исчезнет само, запись можно будет удалить.
# ВАЖНО: сравнение строгое, без нечёткого. «Зитракс» и «Зетрикс» —
# разные фамилии в одну букву, запрет только на «Зетрикс». Поэтому в самой
# пометке проговаривается, что имелась в виду другая фамилия.
BANNED: dict = {
    "Зетрикс": "ЗАБАНЕНО, больше не выдавать. Ответил skyfall_, "
               "команда RPM ROLEPLAY\n"
               "Запрещён именно ЗЕТРИКС (с «е»).\n"
               "ЗИТРАКС (с «и») — выдать можно.",
}


def ban_reason(name: str) -> str | None:
    """Почему фамилию нельзя выдавать, если она запрещена."""
    for banned, reason in BANNED.items():
        if key(banned) == key(name):
            return reason
    return None

RE_HTML = re.compile(r"<\s*(?:!doctype|html|head|body)\b", re.I)
RE_DOC_URL = re.compile(r"/d/([a-zA-Z0-9_-]{20,})")
RE_DOC_ID = re.compile(r"^[a-zA-Z0-9_-]{20,}$")
RE_PAREN = re.compile(r"[([]\s*([^()[\]]+?)\s*[)\]]")
# Google Docs любит превращать строку в пункт списка или сдвигать отступом.
# Такая строка иначе молча теряется, а её поля приписываются к прошлому
# человеку — поэтому снимаем маркер списка перед разбором.
RE_BULLET = re.compile(r"^\s*(?:[-*•·‣⁃–—]+\s*)+")



# ---------------------------------------------------------------- утилиты

def fix_stdio() -> None:
    """На Windows консоль по умолчанию cp1251 — из-за кириллицы был бы краш."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def key(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower().replace("ё", "е")


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def loose(s: str) -> str:
    """Схлопнутая транслитерация — ищет «Amb» по «Амброус» и наоборот."""
    return re.sub(r"[^a-z0-9]", "", "".join(_TRANSLIT.get(c, c) for c in key(s)))


class C:
    on = True

    @classmethod
    def _w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else s

    @classmethod
    def dim(cls, s):
        return cls._w("2", s)

    @classmethod
    def bold(cls, s):
        return cls._w("1", s)

    @classmethod
    def green(cls, s):
        return cls._w("32", s)

    @classmethod
    def yellow(cls, s):
        return cls._w("33", s)

    @classmethod
    def red(cls, s):
        return cls._w("31", s)

    @classmethod
    def cyan(cls, s):
        return cls._w("36", s)

    @classmethod
    def magenta(cls, s):
        return cls._w("35", s)


# ---------------------------------------------------------------- пути

def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def cache_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP)


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def doc_paths(doc_id: str) -> tuple[str, str]:
    """Кеш и история — отдельные на каждый документ, чтобы не смешивались."""
    tag = (doc_id or "default")[:12]
    d = cache_dir()
    return (os.path.join(d, f"doc-{tag}.txt"),
            os.path.join(d, f"surnames-{tag}.tsv"))


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# ---------------------------------------------------------------- конфиг

def load_config() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    _write_private(config_path(),
                   json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def connect_doc(doc_id: str, label: str) -> str | None:
    """Запоминает документ. Возвращает текст ошибки или None."""
    cfg = load_config()
    cfg["doc_id"] = doc_id
    cfg["label"] = label
    try:
        save_config(cfg)
    except OSError as e:
        return f"не смог сохранить настройки: {e.strerror}. " \
               f"Проверь права на {config_dir()}"
    return None


def extract_doc_id(value: str) -> str | None:
    """Принимает полную ссылку, ссылку с /edit?pli=1 или голый ID."""
    v = (value or "").strip().strip("<>\"'")
    m = RE_DOC_URL.search(v)
    if m:
        return m.group(1)
    bare = v.split("?")[0].split("/")[-1]
    return bare if RE_DOC_ID.match(bare) else None


# ---------------------------------------------------------------- загрузка

FETCH_ATTEMPTS = 3
FETCH_TIMEOUT = 10
FETCH_TIMEOUT_SHORT = 3     # проба пока сеть считается лежащей
NET_BREAKER_COOLDOWN = 900   # после неудач не лезем в сеть 15 минут


def netfail_path() -> str:
    return os.path.join(cache_dir(), "netfail")


def net_is_down() -> bool:
    """Недавно была неудача — значит сеть, скорее всего, ещё не ожила.

    Без этого утилита на каждом запуске ждала бы все попытки, а это
    полминуты на команду, когда интернета нет.
    """
    try:
        return time.time() - os.path.getmtime(netfail_path()) < NET_BREAKER_COOLDOWN
    except OSError:
        return False


def mark_net_up() -> None:
    try:
        os.remove(netfail_path())
    except OSError:
        pass


def mark_net_down() -> None:
    try:
        _write_private(netfail_path(), "")
    except OSError:
        pass


def fetch(doc_id: str, path: str, force: bool = False) -> str:
    if not force and os.path.isfile(path):
        if time.time() - os.path.getmtime(path) < CACHE_TTL:
            with open(path, encoding="utf-8-sig") as f:
                return f.read()

    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/1.0"})

    # после недавних неудач ходим один раз и недолго: моргнуть может и так,
    # но ждать полминуты на каждой команде при лежащей сети нельзя
    down = net_is_down()
    attempts = 1 if down else FETCH_ATTEMPTS
    timeout = FETCH_TIMEOUT_SHORT if down else FETCH_TIMEOUT
    last = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8-sig", errors="replace")
            mark_net_up()
            break
        except urllib.error.URLError as e:
            last = e
            if attempt < attempts:
                sys.stderr.write(C.dim("  попытка не удалась, повторяю…\n"))
                time.sleep(0.7)
    else:
        mark_net_down()
        if os.path.isfile(path):
            age = int(time.time() - os.path.getmtime(path))
            sys.stderr.write(C.yellow(
                f"! документ недоступен ({last.reason}), "
                f"кешу {format_age(age)}\n"))
            with open(path, encoding="utf-8-sig") as f:
                return f.read()
        sys.stderr.write(C.red(f"! не удалось загрузить документ: {last.reason}\n"))
        sys.stderr.write(C.dim("  проверить: rpmfam-search --doc <ссылка>\n"))
        raise SystemExit(2)

    try:
        _write_private(path, raw)
    except OSError as e:
        # Каталог кеша может быть недоступен для записи (залоченный профиль,
        # права на домашний каталог, диск только для чтения). Данные уже в
        # памяти — покажем результат, просто не сохранив кеш.
        sys.stderr.write(C.yellow(
            f"! не смог сохранить кеш ({e.strerror}), работаю без него\n"))
    return raw


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 час», «2 часа», «5 часов» — русские числительные."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def format_age(seconds: int) -> str:
    """Человеческий возраст кеша: «5 минут», «2 часа», «3 дня»."""
    if seconds < 90:
        return plural(max(seconds, 1), "секунда", "секунды", "секунд")
    if seconds < 5400:
        return plural(seconds // 60, "минута", "минуты", "минут")
    if seconds < 172800:
        return plural(seconds // 3600, "час", "часа", "часов")
    return plural(seconds // 86400, "день", "дня", "дней")


def fetched_at(path: str) -> str:
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(os.path.getmtime(path)))
    except OSError:
        return "неизвестно"


# ---------------------------------------------------------------- разбор

def parse_person_name(s: str) -> tuple[str, str | None]:
    """'Порше (Crackedon))' и 'Уильямс (DomovenokStopani0' -> (имя, ник).

    В документе встречаются незакрытая и лишняя скобки, поэтому не полагаемся
    на строгий шаблон: закрываем хвост, если скобок не хватает, и берём последнюю
    группу как ник.
    """
    s = norm(s)
    if s.count("(") > s.count(")"):
        s += ")" * (s.count("(") - s.count(")"))

    groups = list(RE_PAREN.finditer(s))
    if groups:
        last = groups[-1]
        nick = norm(last.group(1))
        head = re.sub(r"[([][^()[\]]*[)\]]", " ", s[:last.start()])
        name = norm(head) or norm(s)
        return name, nick or None
    return norm(s.rstrip("([ ")), None


def parse_block(block: str) -> dict:
    people: list[dict] = []
    note: str | None = None
    cur: dict | None = None

    for raw in block.split("\n"):
        line = RE_BULLET.sub("", raw.rstrip())
        if not line.strip():
            continue

        m = RE_ROLE_LINE.match(line)
        if m:
            role = norm(m.group(1)).capitalize()
            role = ROLE_HEAD if role.lower() == "глава" else role
            name, nick = parse_person_name(m.group(2))
            cur = {"role": role, "name": name, "nick": nick,
                   "passport": None, "phone": None}
            people.append(cur)
            continue

        if cur is not None:
            fm = RE_FIELD.match(line)
            if fm:
                what = key(fm.group(1))
                val = norm(fm.group(2))
                if "паспорт" in what:
                    cur["passport"] = val
                elif "телефон" in what:
                    cur["phone"] = val
                continue
            if not RE_MARKER.match(line):
                # неопознанная строка: дальше поля уже не её, иначе они
                # испортят предыдущего человека
                cur = None
                continue

        # примечание на весь блок: «подойдёт любой представитель», «относится к X»
        if note is None and line.strip() and not RE_ROLE_LINE.match(line):
            s = norm(line)
            if RE_ANYONE.search(s) or RE_REF.search(s):
                note = s

    return {"people": people, "note": note}


def _surname_section(text: str) -> tuple[list[str], int]:
    lines = text.split("\n")
    end = len(lines)
    for i, l in enumerate(lines):
        if l.strip().startswith("(список может"):
            end = i
            break
    for i, l in enumerate(lines):
        if l.strip().startswith("(Обновил"):
            end = min(end, i)
            break
    # начало списка — первая строка-буква, до неё заголовок и правила
    start = 0
    for i, l in enumerate(lines[:end]):
        s = norm(l)
        if len(s) == 1 and s.isalpha():
            start = i
            break
    return lines, start, end


def parse_surnames(lines: list[str], start: int, end: int) -> list[dict]:
    """Строка может нести несколько сносок: 'Бронзе[r]*[s]', а из-за склейки
    абзацев бывает 'Дрейхард\\n[bo]Дельта[bp]', где [bo] уже принадлежит
    фамилии с предыдущей строки. Маркер привязывается к ближайшему тексту
    СЛЕВА, иначе — к предыдущей строке, иначе — к последней выведенной."""
    out: list[dict] = []
    pending: str | None = None

    def emit(name: str, marker: str | None, star: bool = False) -> None:
        n = norm(name)
        if not n or n.startswith("(") or len(n) < 2:
            return
        if marker is None:
            out.append({"name": n, "markers": [], "star": False})
            return
        if out and out[-1]["name"] == n and out[-1]["markers"]:
            if marker not in out[-1]["markers"]:
                out[-1]["markers"].append(marker)
            out[-1]["star"] = out[-1]["star"] or star
            return
        out.append({"name": n, "markers": [marker], "star": star})

    for raw in lines[start:end]:
        s = norm(raw)
        if not s:
            continue
        star = "*" in s
        s = norm(s.replace("*", " "))

        if len(s) == 1 and s.isalpha():
            pending = None
            continue

        acc = ""
        for i, tok in enumerate(re.split(r"(\[([a-z]{1,2})\])", s, flags=re.I)):
            if i % 3 == 2:
                continue
            if i % 3 == 1:
                marker = tok[1:-1].lower()
                name = norm(acc)
                if name:
                    if pending and key(pending) != key(name):
                        emit(pending, None)
                    emit(name, marker, star)
                    acc, star = "", False
                elif pending:
                    emit(pending, marker)
                elif out and out[-1]["markers"]:
                    if marker not in out[-1]["markers"]:
                        out[-1]["markers"].append(marker)
                pending = None
                continue
            acc += tok
        acc = norm(acc)
        if acc and not acc.startswith("["):
            if pending and key(pending) != key(acc):
                emit(pending, None)
            pending = acc

    if pending:
        emit(pending, None)
    return out


def parse(text: str) -> dict:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines, start, end = _surname_section(text)
    surnames = parse_surnames(lines, start, end)

    notes: dict[str, dict] = {}
    cur = None
    buf: list[str] = []
    note_start = 0
    for i, l in enumerate(lines):
        if l.strip().startswith("(Обновил"):
            note_start = i + 1
            break
    for l in lines[note_start:]:
        m = RE_MARKER.match(l)
        if m:
            if cur:
                notes[cur] = parse_block("\n".join(buf))
            cur = m.group(1).lower()
            buf = [l[m.end():]]
        elif cur is not None and l.strip():
            buf.append(l)
    if cur:
        notes[cur] = parse_block("\n".join(buf))

    updated_at = None
    updated_by = None
    for i, l in enumerate(lines):
        if "Обновил" in l:
            m = re.search(r"Обновил:\s*(.+?)\s*$",
                          norm(l).replace("(", " ").replace(")", " "))
            if m:
                updated_by = m.group(1)
            for j in range(i - 1, max(-1, i - 4), -1):
                d = re.search(r"(\d{2}\.\d{2}\.\d{4})", lines[j])
                if d:
                    updated_at = d.group(1)
                    break
            break

    return {"surnames": surnames, "notes": notes, "text": text,
            "updated_at": updated_at, "updated_by": updated_by,
            "roles": len(RE_ROLE_ANY.findall(text))}


# ---------------------------------------------------------------- проверки

def registry_problems(db: dict, strict: bool = False) -> list[str]:
    """Почему выгрузка не похожа на реестр фамилий. strict — для --doc."""
    problems = []
    text = db.get("text", "")

    if RE_HTML.search(text[:4000]):
        problems.append("Google вернул HTML-страницу, а не текст документа. "
                        "Похоже, документ закрыт или ссылка неверная")
    if not db["surnames"]:
        problems.append("не найдено ни одной фамилии")
    elif strict and len(db["surnames"]) < MIN_SURNAMES:
        problems.append(f"найдено всего {len(db['surnames'])} фамилий, "
                        f"для реестра ожидается от {MIN_SURNAMES}")
    if not db["roles"]:
        problems.append("не найдено ни строки «Глава -» или «Заместитель -»")
    elif strict and db["roles"] < MIN_ROLES:
        problems.append(f"найдено {db['roles']} представителей, "
                        f"для реестра ожидается от {MIN_ROLES}")
    if strict and len(db["notes"]) < MIN_NOTES:
        problems.append(f"найдено {len(db['notes'])} сносок, "
                        f"для реестра ожидается от {MIN_NOTES}")
    return problems


# ---------------------------------------------------------------- сборка

def entries(db: dict) -> list[dict]:
    out: list[dict] = []
    index: dict[str, dict] = {}
    for s in db["surnames"]:
        n = norm(s["name"])
        if not n:
            continue
        info = index.get(key(n))
        if info is None:
            info = {"name": n, "star": s.get("star", False), "blocks": [],
                    "note": None, "ref": None, "markers": []}
            index[key(n)] = info
            out.append(info)
        info["star"] = info["star"] or s.get("star", False)

        for m in s["markers"]:
            if m in info["markers"]:
                continue
            info["markers"].append(m)
            b = db["notes"].get(m)
            if not b:
                continue
            info["blocks"].append(b)
            if b["note"] and not info["note"]:
                info["note"] = b["note"]
                r = RE_REF.search(b["note"])
                if r:
                    info["ref"] = norm(r.group(1))
                # звёздочку в списке могли забыть поставить, но если в сноске
                # прямо сказано «подойдёт любой представитель» — помечаем
                if RE_ANYONE.search(b["note"]):
                    info["star"] = True
        if not info["blocks"] and s["markers"]:
            info["note"] = info["note"] or "нет данных в документе"
    return out


def person_keys(p: dict) -> list[str]:
    out = []
    if p.get("passport"):
        out.append("p:" + key(p["passport"]).replace(" ", ""))
    if p.get("nick"):
        out.append("n:" + key(p["nick"]))
    return out


def build_links(entries_list) -> dict:
    idx: dict[str, set] = {}
    for e in entries_list:
        for b in e["blocks"]:
            for p in b["people"]:
                for k in person_keys(p):
                    idx.setdefault(k, set()).add(e["name"])
    return idx


def related(links: dict, e: dict) -> list[str]:
    out: set = set()
    for b in e["blocks"]:
        for p in b["people"]:
            for k in person_keys(p):
                out |= links.get(k, set())
    out.discard(e["name"])
    return sorted(out)


def resolve_ref(entries_list, name):
    nk = key(name)
    for e in entries_list:
        if key(e["name"]) == nk:
            return e
    return None


def suspicious_spaced(name: str) -> bool:
    """Фамилия с пробелом внутри — почти всегда опечатка в документе.

    «Флоре с» вместо «Флорес». Двойные фамилии через дефис норма, а вот
    пробел посередине — нет.
    """
    return " " in norm(name) and "-" not in name


# ---------------------------------------------------------------- вывод

def fmt_person(p: dict) -> str:
    who = f'"{p["name"]}'
    if p.get("nick"):
        who += f' ({p["nick"]})'
    who += '"'
    extra = []
    if p.get("passport"):
        extra.append(p["passport"])
    if p.get("phone"):
        extra.append(f"тел. {p['phone']}")
    return who + ("  " + C.dim(" | ".join(extra)) if extra else "")


def _find_probe(query: str, text: str) -> int:
    """Позиция запроса в тексте, -1 если нет. Ищем по нормализованным."""
    for probe in (norm(query), key(query), loose(query)):
        if not probe:
            continue
        i = key(text).find(key(probe))
        if i >= 0:
            return i
    return -1


def mark_match(query: str, text: str | None) -> str | None:
    """Показывает, какое именно слово совпало: «Данила Йегер-[Стоун]»."""
    if not text:
        return None
    t = norm(text)
    i = _find_probe(query, t)
    if i < 0:
        return t
    n = len(key(norm(query)) or key(query)) or 1
    return f"{t[:i]}[{t[i:i + n]}]{t[i + n:]}"


def match_kind(query: str, p: dict) -> str:
    """Где именно совпало: в нике, в фамилии или в имени."""
    for probe in (key(query), loose(query)):
        if probe and key(p.get("nick") or "") .find(probe) >= 0:
            return "нике"

    nk = key(p["name"])
    for probe in (key(query), loose(query)):
        if not probe or probe not in nk:
            continue
        # совпадение в начале слова: слово после пробела или дефиса —
        # это фамилия, иначе имя
        before = nk.split(probe)[0]
        return "фамилии" if before.endswith(("-",)) or " " in before else "имени"
    return "имени"


WHERE = {"фамилии": "в фамилии представителя",
         "нике": "в нике представителя",
         "имени": "в имени представителя"}


def fmt_person_marked(p: dict, query: str) -> str:
    """То же, что fmt_person, но с подсвеченным совпадением."""
    name = mark_match(query, p["name"]) or p["name"]
    who = f'"{name}'
    if p.get("nick"):
        nick = mark_match(query, p["nick"])
        who += f" ({nick})" if nick else f" ({p['nick']})"
    who += '"'
    extra = []
    if p.get("passport"):
        extra.append(p["passport"])
    if p.get("phone"):
        extra.append(f"тел. {p['phone']}")
    return who + ("  " + C.dim(" | ".join(extra)) if extra else "")


CYR = re.compile(r"[А-Яа-яЁё]")
LAT = re.compile(r"[A-Za-z]")


def looks_like_surname(q: str) -> bool:
    """Кириллица — значит человек ищет фамилию, а не ник."""
    return bool(CYR.search(q))


def print_person_hits(query: str, persons, contains, numbers, contacts: bool) -> None:
    """Находки по людям.

    Важно не выдавать найденную семью за ответ на запрос: если фамилии
    в списке нет, это и есть ответ — выдать её нельзя. Совпадение в
    двойной фамилии показываем отдельной строкой, а не вместо ответа.
    """
    if contains or numbers:
        print(C.bold(f'Найдено по представителю: "{query}"') + "\n")
        for e, p in persons:
            print(f'{C.bold("Зарегестрированная фамилия:")} "{e["name"]}"')
            print(f"  {C.green('Роль:')} {p['role']}  {fmt_person_marked(p, query)}")
        return

    if looks_like_surname(query):
        print(C.red(f'Фамилия "{query}" не зарегистрирована — выдать её нельзя.'))
        kinds = {match_kind(query, p) for _, p in persons}
        where = WHERE[sorted(kinds)[0]] if len(kinds) == 1 else None
        if where:
            print()
            print(C.dim(f"Совпадение нашлось {where}:"))
    else:
        print(C.green(f'Представитель с ником "{query}" найден:') + "\n")

    for e, p in persons:
        print(f"  {C.dim('семья')} {C.bold(e['name'])}  —  "
              f"{p['role']}, {fmt_person_marked(p, query)}")


def print_entry(e: dict, entries_list, depth: int = 0, seen=None, links=None,
                tag: str = "", indent: str = "") -> None:
    seen = seen or set()
    pad = indent + "  " * depth

    # Звёздочка = условия выдачи отличаются, подходит любой представитель.
    # Показываем в заголовке, чтобы не прочитать по диагонали.
    if e.get("star"):
        head = (f'{pad}{C.bold("Зарегестрированная фамилия:")} '
                f'"{C.bold(e["name"])}"  '
                f'{C.yellow(C.bold("* подойдёт любой представитель"))}')
    else:
        head = f'{pad}{C.bold("Зарегестрированная фамилия:")} "{C.bold(e["name"])}"'
    if tag:
        head += "  " + C.dim(tag)
    print(head)

    banned = ban_reason(e["name"])
    if banned:
        lines = banned.split("\n")
        print(f"{pad}  {C.red(C.bold('⛔ ' + lines[0]))}")
        for extra in lines[1:]:
            print(f"{pad}     {C.yellow(extra)}")

    people = [p for b in e["blocks"] for p in b["people"]]
    heads = [p for p in people if p["role"] == ROLE_HEAD]
    deputies = [p for p in people if p["role"] != ROLE_HEAD]
    note = e.get("note")

    def star_notice(indent: str) -> None:
        text = note or "Подойдет любой представитель"
        print(f"{indent}{C.yellow('⚠ ' + text)}")
        if RE_CAVEAT.search(text):
            print(f"{indent}{C.dim('Только с предварительного разрешения Главы фамилии.')}")

    if not people and e.get("ref"):
        target = resolve_ref(entries_list, e["ref"])
        print(f"{pad}  {C.dim('→ относится к фамилии')} \"{e['ref']}\":")
        if target and key(target["name"]) not in seen:
            print_entry(target, entries_list, depth + 1,
                        seen | {key(target["name"])}, links)
        else:
            print(f"{pad}  {C.red('⛔')} {C.yellow('битая ссылка')} "
                  f"({e['ref']}{C.red(' больше нет в документе)')}")
            print(f"{pad}  {C.dim('спроси автора документа, на какую фамилию ссылаться')}")
    elif not people and e.get("star"):
        star_notice(pad + "  ")
    elif not people:
        print(f"{pad}  {C.yellow(note or 'нет данных в документе')}")
    else:
        for p in heads:
            print(f"{pad}  {C.green('Представитель:')} {fmt_person(p)}")
        for p in deputies:
            print(f"{pad}  {C.cyan('Заместитель:')} {fmt_person(p)}")
        if e.get("star"):
            star_notice(pad + "  ")
        elif note and not e.get("ref"):
            print(f"{pad}  {C.yellow('Примечание:')} {note}")
        if links:
            rel = related(links, e)
            if rel:
                print(f"{pad}  {C.magenta('Двойные фамилии того же человека:')} "
                      + ", ".join(f'"{r}"' for r in rel))


# ---------------------------------------------------------------- РП имя

RULES_BAN = ("Запрет команды. Фраза «а на весте можно», «мне такое выдали» "
             "и «я раньше так ходил» аргументом не является.")


def forebears_url(kind: str, word: str) -> str:
    """Готовая ссылка на forebears.io: kind — name или surnames."""
    return f"https://forebears.io/{kind}/{urllib.parse.quote(loose(word))}"


def forebears_coverage(kind: str, word: str) -> int | None:
    """Охват с forebears.io. None — не смогли или сайт не ответил.

    Сайт рейтлимитит и при частых запросах отдаёт пустую страницу, поэтому
    ответы кэшируются, а отсутствие ответа — это не «мало носителей».
    """
    slug = f"{kind}:{loose(word)}"
    cache = os.path.join(cache_dir(), "forebears.tsv")
    try:
        with open(cache, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.rstrip("\n").partition("\t")
                if k == slug:
                    return int(v)
    except (OSError, ValueError):
        pass

    url = f"https://forebears.io/{kind}/{urllib.parse.quote(loose(word))}"
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None

    if len(html) < 500:
        return None
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text)))
    m = re.search(r"Approximately\s+([\d,]+)\s+people bear this (?:surname|name)", text)
    if not m:
        return None
    value = int(m.group(1).replace(",", ""))

    try:
        lines = []
        if os.path.isfile(cache):
            with open(cache, encoding="utf-8") as f:
                lines = [x for x in f.read().split("\n") if x]
        lines = [x for x in lines if not x.startswith(slug + "\t")]
        lines.append(f"{slug}\t{value}")
        _write_private(cache, "\n".join(lines) + "\n")
    except OSError:
        pass
    return value


def print_name_report(raw: str, entries_list, links, use_net: bool) -> int:
    clean = rpname.clean_name(raw)
    findings = rpname.check_name(raw)

    # --- реестр: запреты и одобрение Главы --------------------------------
    parts = [p for p in re.split(r"\s+", clean) if p]
    surname_only = len(parts) == 1
    surname = parts[0] if surname_only else (parts[1] if len(parts) > 1 else "")
    hits: list = []
    near: list = []
    if surname and entries_list:
        _, found, persons, _ = do_search(surname, entries_list)
        # Зарегистрирована только точное совпадение фамилии. «Тестов» — это
        # начало «Тестова-Арч» из двойной фамилии, а не своя фамилия,
        # и одобрение Главы для неё не нужно.
        key = surname.casefold()
        hits = [e for e in found if e["name"].casefold() == key]
        if not hits:
            near = sorted({e["name"] for e in found}
                          | {e["name"] for e, _ in persons})

    for e in hits:
        reason = ban_reason(e["name"])
        if reason:
            findings.insert(0, rpname.Finding(
                "block", f"Фамилия «{e['name']}» запрещена",
                reason, RULES_BAN))

    blocks = [f for f in findings if f.level == "block"]
    warns = [f for f in findings if f.level == "warn"]
    oks = [f for f in findings if f.level == "ok"]

    print(C.bold("═" * 58))
    print(C.bold(f" Проверка РП имени: {clean or raw}"))
    print(C.bold("═" * 58))
    print()

    if blocks:
        print(C.red(C.bold(f" ✗ ПАСПОРТ ВЫДАВАТЬ НЕЛЬЗЯ — "
                           f"нарушений: {len(blocks)}")))
        print()
        for f in blocks:
            print(f"  {C.red('✗')} {C.bold(f.title)}")
            for line in f.detail.split("\n"):
                print(f"     {C.yellow(line)}")
            if f.fix:
                print(f"     {C.dim('как надо:')} {C.green(f.fix)}")
            if f.rule:
                print(f"     {C.dim('правило:')} {f.rule}")
            print()
    else:
        print(C.green(C.bold(" ✓ Грубых нарушений не найдено")))
        print(C.dim("   Это не разрешение выдать паспорт — см. «что проверить "
                    "вручную» ниже."))
        print()

    if warns:
        for f in warns:
            print(f"  {C.yellow('⚠')} {C.bold(f.title)}")
            if f.detail:
                print(f"     {C.yellow(f.detail)}")
            if f.rule:
                print(f"     {C.dim('правило:')} {f.rule}")
            print()

    if oks:
        print(C.dim(" ——— что проверено и в порядке ———"))
        for f in oks:
            print(f"  {C.green('✓')} {C.dim(f.title)}")
        print()

    if hits:
        print(C.yellow(C.bold(" ⚠ фамилия есть в реестре зарегистрированных")))
        print()
        for e in {e["name"]: e for e in hits}.values():
            print_entry(e, entries_list, links=links, indent="   ")
        print(C.dim("   Без личного присутствия Главы или Зама главы и его "
                    "одобрения не выдавать."))
        print(C.dim("   Фразы «а мне на весте такое дали», «это моё старое "
                    "имя» — не аргумент."))
        print()
    elif surname:
        if not near and entries_list:
            near = suggestions(surname, entries_list)
        print(C.yellow(C.bold(" ⚠ фамилии в реестре нет")))
        if near:
            print(C.dim("   Похожее в реестре: " + ", ".join(near)
                        + " — проверь, не та ли фамилия."))
            print(C.dim("   Совпадение может быть и внутри двойной фамилии, "
                        "это не регистрация твоей."))
        print(C.dim("   Проверь Discord-канал 🪪фам-документ: там последние "
                    "регистрации, которые ещё не в документе."))
        print()

    # --- что вручную -----------------------------------------------------
    print(C.bold(" Что проверить вручную"))
    first = "" if surname_only else (parts[0] if parts else "")
    if first or surname:
        print(C.dim("   forebears.io, охват нужен от 2000:"))
    if first:
        print(f"     имя      {C.cyan(forebears_url('name', first))}")
        if use_net:
            cov = forebears_coverage("name", first)
            print("     " + (C.green(f"охват: {cov}")
                             if cov else C.yellow("охват не удалось получить")))
    if surname:
        print(C.dim("     фамилия  " + C.cyan(forebears_url("surnames", surname))))
        if use_net:
            cov = forebears_coverage("surnames", surname)
            if cov is None:
                print("     " + C.yellow("охват не удалось получить"))
            elif cov < 2000:
                print(C.red(f"     охват: {cov} — меньше 2000, не выдавать"))
            else:
                print(C.green(f"     охват: {cov}"))
    print(C.dim("   Поисковик: не известная личность, политик, аниме-персонаж."))
    if first:
        print(C.dim("   Имя должно звучать естественно, а не редко и вычурно."))
    print()
    return 1 if blocks else 0


def footer(db: dict, ctx: dict) -> None:
    print()
    print(C.dim("─" * 46))
    print(C.dim(f"Данные актуальны на {fetched_at(ctx['cache'])}"))
    print(C.dim(f"источник: {ctx['label']}"))
    at, by = db.get("updated_at"), db.get("updated_by")
    if at or by:
        line = "Документ обновлён: " + (at or "дата не указана")
        if by:
            line += f" ({by})"
        print(C.dim(line))


# ---------------------------------------------------------------- поиск


def script_compatible(query: str, token: str) -> bool:
    """Кириллический запрос не должен попадать в латинский ник.

    Транслитерация нужна для обратного: набранное латиницей «Ambrous»
    обязано находить «Амброус». А вот «Аливе» → «alive» → ник
    «_aLIVEshka_» — случайное совпадение, и такие находки вредят больше,
    чем помогают.
    """
    q_cyr, q_lat = bool(CYR.search(query)), bool(LAT.search(query))
    t_cyr, t_lat = bool(CYR.search(token)), bool(LAT.search(token))
    return not (q_cyr and not q_lat and t_lat and not t_cyr)


def matches_at_word_start(query: str, text: str, orig: str | None = None) -> bool:
    """Запрос встречается в тексте, начинаясь с начала слова.

    Обычный поиск подстроки давал ерунду: транслитерация «Хейс» (heys)
    сидит внутри «Джейс» (dzheys), и поиск выдавал чужую фамилию. Поэтому
    совпадение засчитывается только там, где перед запросом не буква и не
    цифра. Двойные фамилии и дефисы при этом не мешают: дефис — не буква.

    orig — исходный запрос до транслитерации, нужен чтобы отличить
    кириллицу от латиницы (см. script_compatible).
    """
    if not query:
        return False
    start = 0
    while True:
        pos = text.find(query, start)
        if pos < 0:
            return False
        if pos == 0 or not (text[pos - 1].isalnum()):
            if orig is not None and not script_compatible(orig,
                                                          text[pos:pos + len(query)]):
                start = pos + 1
                continue
            return True
        start = pos + 1


def loose_words(text: str) -> str:
    """Транслитерация с сохранением границ слов.

    loose() склеивает всё в одну строку, и границы слов пропадают — тогда
    «Ambros» не находил «Амброус». Здесь каждое слово переводится отдельно.
    """
    return " ".join(loose(t) for t in re.split(r"[\s\-]+", norm(text)) if t)


def text_matches(query: str, text: str) -> bool:
    """Совпадение запроса с текстом: сначала точно, потом по транслитерации."""
    text = norm(text)
    if matches_at_word_start(key(query), key(text)):
        return True
    return matches_at_word_start(loose(query), loose_words(text), orig=query)


def do_search(q: str, entries_list):
    exact = [e for e in entries_list
             if key(e["name"]) == key(q) or (loose(q) and loose(e["name"]) == loose(q))]
    if exact:
        return "surname", exact, [], []

    contains = [e for e in entries_list if text_matches(q, e["name"])]

    persons = []
    for e in entries_list:
        for b in e["blocks"]:
            for p in b["people"]:
                who = " ".join(filter(None, [p["name"], p.get("nick") or ""]))
                if text_matches(q, who):
                    persons.append((e, p))

    numbers = []
    # номер ищется и как есть, так и с префиксом RPM-, как в документе
    digits_only = re.sub(r"^rpm\s*-\s*", "", key(q), flags=re.I)
    if RE_DIGITS.match(digits_only):
        for e in entries_list:
            for b in e["blocks"]:
                for p in b["people"]:
                    digits = re.sub(r"\D", "", " ".join(
                        filter(None, [p.get("passport") or "", p.get("phone") or ""])))
                    if digits_only in digits:
                        numbers.append((e, p))

    return "contains", contains, persons, numbers


SUGGEST_LIMIT = 5
SUGGEST_RATIO = 0.7        # ниже — уже не опечатка, а мусор


def _suggest_score(query: str, name: str) -> float | None:
    """Насколько вероятно, что name — опечатка вместо query. None — нет."""
    q, n = key(query), key(name)
    if not q or not n:
        return None
    # опечатка почти не меняет длину: на 1 в коротком слове, на 2 в длинном
    slack = 1 if len(q) < 5 else 2
    if abs(len(n) - len(q)) > slack:
        return None
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    if ratio < SUGGEST_RATIO:
        return None
    # Общий префикс важнее всего: опечатка обычно в конце слова, поэтому
    # «Хейс» -> «Хейз» отсеивается, а «Хейс» -> «Грейс» не проходит.
    prefix = 0
    for x, y in zip(q, n):
        if x != y:
            break
        prefix += 1
    return prefix * 10 + ratio


def suggestions(q: str, entries_list) -> list[str]:
    """Похожие фамилии в исходном регистре, лучшие первыми."""
    scored = []
    for e in entries_list:
        score = _suggest_score(q, e["name"])
        if score is not None:
            scored.append((score, e["name"]))
    scored.sort(key=lambda t: (-t[0], len(t[1]), t[1]))
    return [n for _, n in scored[:SUGGEST_LIMIT]]



# ---------------------------------------------------------------- история

def load_snapshot(path: str) -> dict:
    snap: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                name, _, ts = line.rstrip("\n").partition("\t")
                if name and ts.isdigit():
                    snap[name] = int(ts)
    except OSError:
        pass
    return snap


def update_snapshot(path: str, entries_list) -> tuple[dict, list, list, bool]:
    """Отмечает дату первого появления фамилии.

    На первом запуске всё, что уже есть, — базовая линия, а не «новые
    фамилии», поэтому её метим как давно существующую (0).
    """
    first_run = not os.path.exists(path)
    snap = load_snapshot(path)
    now = int(time.time())
    names = {e["name"] for e in entries_list}
    fresh = [] if first_run else [n for n in names if n not in snap]
    gone = [] if first_run else [n for n in snap if n not in names]
    for n in names:
        if n not in snap:
            snap[n] = 0 if first_run else now
    for n in gone:
        snap.pop(n, None)
    try:
        _write_private(path, "".join(
            f"{n}\t{snap[n]}\n" for n in sorted(snap)))
    except OSError:
        pass
    return snap, fresh, gone, first_run


def show_new(entries_list, links, snap, fresh, gone, window, first_run, ctx) -> None:
    now = time.time()
    since = time.strftime("%d.%m.%Y", time.localtime(now - window))

    if first_run:
        print(C.yellow("Первый запуск отслеживания."))
        print(C.dim(f"Запомнил {len(entries_list)} фамилий. Со следующего раза здесь "
                    "будут появляться новые — с представителями."))
        print(C.dim(f"отслеживание с {time.strftime('%d.%m.%Y %H:%M', time.localtime(now))}"
                    f" | источник: {ctx['label']}"))
        return

    if gone:
        print(C.red(f"Исчезли из документа ({len(gone)}): ") + ", ".join(gone))
        print()

    recent = [e for e in entries_list if now - snap.get(e["name"], now) <= window]
    if fresh:
        print(C.green(f"С прошлого запуска добавлено: {len(fresh)}")
              + C.dim("  (" + ", ".join(fresh) + ")"))
        print()

    if not recent:
        print(C.dim(f"Новых фамилий с {since} нет."))
    else:
        print(C.bold(f"Новые фамилии с {since} ({len(recent)}):"))
        for e in sorted(recent, key=lambda x: snap.get(x["name"], 0), reverse=True):
            ts = snap.get(e["name"])
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "?"
            print_entry(e, entries_list, links=links, tag=f"добавлена {when}")
            print()

    print(C.dim(f"всего в базе: {len(entries_list)}"
                f" | источник: {ctx['label']}"
                f" | выгрузка {fetched_at(ctx['cache'])}"))


# ---------------------------------------------------------------- --check

def run_check(db: dict, entries_list, ctx: dict) -> int:
    used = {m for s in db["surnames"] for m in s["markers"]}
    lost = sorted(set(db["notes"]) - used)
    people = [p for e in entries_list for b in e["blocks"] for p in b["people"]]
    empty = [e["name"] for e in entries_list
             if not any(b["people"] for b in e["blocks"])]
    refs = [(e["name"], e["ref"]) for e in entries_list if e.get("ref")]
    broken = [(a, r) for a, r in refs if not resolve_ref(entries_list, r)]
    weird = [e["name"] for e in entries_list if "<" in e["name"] or ">" in e["name"]]

    # огрехи самого документа: не ломают утилиту, но их надо чинить
    # автору. Находим автоматически, чтобы не искать руками.
    spaced = [e["name"] for e in entries_list if suspicious_spaced(e["name"])]
    no_prefix, unknown_num = [], []
    for e in entries_list:
        for b in e["blocks"]:
            for p in b["people"]:
                num = p.get("passport")
                if not num or RE_PASSPORT_OK.match(num):
                    continue
                (unknown_num if num.upper().startswith("RPM-") else no_prefix) \
                    .append((e["name"], num))

    print(C.bold("Проверка целостности данных"))
    rows = [
        ("источник", ctx["label"]),
        ("выгрузка", fetched_at(ctx["cache"])),
        ("документ обновлён", db.get("updated_at") or "дата не указана"),
        ("фамилий", str(len(entries_list))),
        ("сносок в документе", str(len(db["notes"]))),
        ("привязано к фамилиям", str(len(used))),
        ("людей", str(len(people))),
    ]
    for name, val in rows:
        print(f"  {name + ':':<22} {val}")

    problems = []
    if lost:
        problems.append(f"потеряно сносок: {len(lost)} — {', '.join(lost[:10])}")
    if weird:
        problems.append(f"в именах фамилий мусор: {', '.join(weird[:5])}")
    for pr in registry_problems(db):
        problems.append(pr)

    if empty:
        print(f"  {'без представителей:':<22} {len(empty)} — "
              + C.dim(", ".join(empty[:8])))
    if broken:
        print(f"  {'из них битых ссылок:':<22} {len(broken)}")

    print()
    if problems:
        for pr in problems:
            print(C.red("  ! " + pr))
    # Некорректности самого документа не делают утилиту сломанной,
    # поэтому код возврата зависит только от problems.
    for a, r in broken:
        print(C.yellow(f"  ~ в документе битая ссылка: {a} → "
                       f"{r} (такой фамилии в списке нет)"))
    for n in spaced:
        print(C.yellow(f"  ~ в фамилии лишний пробел: \"{n}\" — "
                       f"похоже на опечатку, поиск по склеенному виду не сработает"))
    banned_here = [e["name"] for e in entries_list if ban_reason(e["name"])]
    for n in banned_here:
        print(C.red("  ⛔ запрещено к выдаче: ") + n
              + C.dim(" — когда фамилию уберут из документа, "
                      "предупреждение исчезнет само"))
    for n, p in no_prefix:
        print(C.yellow(f"  ~ номер паспорта без префикса: {n} — \"{p}\" "
                       f"(у остальных формат RPM-XXXXXX)"))
    for n, p in unknown_num:
        print(C.yellow(f"  ~ номер паспорта пока неизвестен: {n} — \"{p}\"; "
                       f"утилита с таким не поможет, уточняйте у автора"))
    if not problems:
        print(C.green("  всё в порядке, данные разобраны без потерь"))
    elif not (broken or spaced or no_prefix or unknown_num):
        print(C.dim("  огрехов в самом документе не найдено"))
    return 1 if problems else 0


# ---------------------------------------------------------------- --clean

def clean(keep_config: bool) -> int:
    """Удаляет только свои файлы: doc-*.txt, surnames-*.tsv и config.json."""
    import glob

    removed, freed = [], 0
    base = cache_dir()
    # по префиксам, а не по списку: так чистятся сразу все документы
    patterns = [os.path.join(base, "doc-*.txt"),
                os.path.join(base, "surnames-*.tsv"),
                os.path.join(base, "forebears.tsv"),
                update_state_path(), netfail_path()]
    if not keep_config:
        patterns.append(config_path())
    for pattern in patterns:
        for p in sorted(glob.glob(pattern)):
            if not os.path.isfile(p):
                continue
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                removed.append(p)
            except OSError as e:
                sys.stderr.write(C.yellow(f"! не удалось удалить {p}: {e}\n"))

    # каталоги убираем только после удаления файлов и только если пустые
    for d in (base, config_dir() if not keep_config else None):
        if not d or not os.path.isdir(d) or os.listdir(d):
            continue
        try:
            os.rmdir(d)
            removed.append(d + " (пустой каталог)")
        except OSError:
            pass

    print(C.green("Готово. Удалено:") if removed else C.dim("Нечего удалять — уже чисто."))
    for p in removed:
        print("  " + C.dim(p))
    print(C.dim(f"  всего {freed // 1024} КБ"))
    # sys.argv[0] ненадёжен: при запуске как python -c там «-c» или «-».
    # __file__ всегда указывает на сам модуль.
    print(C.dim("\nСам скрипт не тронут. Удалить его: rm "
                + os.path.realpath(__file__)))
    return 0


# ---------------------------------------------------------------- --doc

def handle_doc(value: str | None, ctx: dict, name: str | None = None) -> int:
    """Показать / задать / сбросить документ."""
    cfg = load_config()
    cur = cfg.get("doc_id")

    if value == "reset":
        cfg.pop("doc_id", None)
        cfg.pop("label", None)
        save_config(cfg)
        print(C.green("Документ отключён."))
        print(C.dim("Теперь утилита работать не будет — при следующем запуске "
                    "снова спросит документ."))
        print(C.dim("Вернуть: rpmfam-search --doc <ссылка>"))
        return 0

    if not value:
        if not cur:
            print(C.yellow("Документ не подключён."))
            print(C.dim("подключить: rpmfam-search --doc <ссылка>"))
            return 0
        print(C.bold("Текущий документ:"))
        print(f"  {C.dim('источник:')} {ctx['label']}")
        print(f"  {C.dim('id:')}       {ctx['doc_id']}")
        print(f"  {C.dim('откуда:')}   конфиг {config_path()}")
        print()
        print(C.dim("сменить:  rpmfam-search --doc <ссылка>"))
        print(C.dim("отключить: rpmfam-search --doc reset"))
        return 0

    doc_id = extract_doc_id(value)
    if not doc_id:
        print(C.red("Не понял, что это за ссылка."))
        print(C.dim("нужно: https://docs.google.com/document/d/<ID>/edit"
                    " или сам <ID>"))
        return 1

    print(C.dim(f"проверяю документ {doc_id}…"))
    db, err = fetch_and_check(doc_id)
    if err:
        print(C.red("Этот документ не подходит:"))
        print("  ! " + err)
        print(C.dim("\nдокумент не подключён, ничего не изменилось"))
        return 1

    want_label = norm(name) if name else None
    if not want_label:
        if cfg.get("doc_id") == doc_id and cfg.get("label"):
            want_label = cfg["label"]        # тот же документ — подпись не трогаем
        elif value.strip().startswith("http"):
            want_label = "свой документ"     # не простыню из ссылки
        else:
            want_label = norm(value)

    err = connect_doc(doc_id, want_label[:60])
    if err:
        print(C.red(err))
        print(C.dim("документ не подключён, ничего не изменилось"))
        return 1
    print(C.green(f"Готово. Подключён документ: {want_label[:60]}"))
    print(C.dim(f"  фамилий: {len(entries(db))} | сносок: {len(db['notes'])}"))
    print(C.dim(f"  сохранено в {config_path()} — вводить ссылку больше не нужно"))
    if not name:
        print(C.dim("  подписать: rpmfam-search --doc <ссылка> --name \"Название\""))
    return 0


# ---------------------------------------------------------------- обновления

def update_state_path() -> str:
    return os.path.join(cache_dir(), "update.json")


def module_dir() -> str:
    # realpath, а не abspath: утилиту часто запускают через symlink из
    # ~/.local/bin, и abpath оставил бы нас в каталоге без репозитория
    return os.path.dirname(os.path.realpath(__file__))


def git_root() -> str | None:
    """Ближайший каталог с .git, если утилита поставлена из репозитория."""
    d = module_dir()
    while True:
        if os.path.isdir(os.path.join(d, ".git")) or \
                os.path.isfile(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def detect_install() -> str:
    """Как утилита установлена: из git или через pip."""
    if git_root():
        return "git"
    d = module_dir().replace("\\", "/")
    parent = os.path.basename(os.path.dirname(module_dir()))
    if parent.endswith(".dist-info") or parent.endswith(".egg-info"):
        return "pip"
    if "site-packages" in d or "dist-packages" in d:
        return "pip"
    return "unknown"


def fetch_latest_version(timeout: float = 8.0) -> str | None:
    """Версия из последней версии файла на GitHub. None — не смогли."""
    try:
        req = urllib.request.Request(
            API_FILE_URL, headers={"User-Agent": f"{APP}/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = base64.b64decode(payload["content"]).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None
    m = RE_VERSION.search(text)
    return m.group(1) if m else None


def load_update_state() -> dict:
    try:
        with open(update_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_update_state(state: dict) -> None:
    try:
        _write_private(update_state_path(),
                       json.dumps(state, ensure_ascii=False) + "\n")
    except OSError:
        pass


def latest_known() -> str | None:
    """Последняя известная версия, без похода в сеть."""
    return load_update_state().get("latest")


def check_update(force: bool = False) -> str | None:
    """Версия, на которую надо обновиться, либо None.

    Сеть дёргается не чаще раза в UPDATE_INTERVAL, чтобы не замедлять
    каждый запуск. При неудаче возвращаем прошлый известный результат.
    """
    state = load_update_state()
    now = int(time.time())
    fresh = int(state.get("checked", 0))
    # Кэш хранит версию, против которой сравнивали. Если локальная версия
    # с тех пор изменилась (например, сделали git pull вручную) — прошлый
    # результат больше не имеет смысла и проверку надо повторить.
    stale = state.get("local") != __version__

    if not force and not stale and now - fresh < UPDATE_INTERVAL:
        return state.get("latest") or None

    latest = fetch_latest_version()
    if latest is None:
        return state.get("latest") or None
    save_update_state({"checked": now, "latest": latest, "local": __version__})
    return latest


def parse_version(v: str | None) -> tuple:
    """Версия как кортеж чисел, чтобы сравнивать, а не просто на неравенство."""
    return tuple(int(p) for p in re.findall(r"\d+", v or "")) or (0,)


def update_pending(latest: str | None) -> bool:
    """Обновление нужно только если удалённая версия НОВЕЕ местной.

    Строгое «больше», а не «не равно»: иначе при откате версии на сервере
    или устаревшем кэше утилита предлагала бы «обновиться» назад.
    """
    if not latest:
        return False
    return parse_version(latest) > parse_version(__version__)


def print_update_notice() -> None:
    latest = check_update()
    if not update_pending(latest):
        return
    print()
    print(C.yellow(C.bold(f"⤴  Доступно обновление: {__version__} → {latest}"))
          + C.dim(f"   {REPO}"))
    print(C.dim("   Обновить: rpmfam-search --update"))


def perform_update() -> int:
    kind = detect_install()
    print(C.bold(f"Обновление {APP} с {__version__}")
          + C.dim(f"  ({kind}-установка)"))
    print()

    sys.stdout.flush()   # иначе вывод subprocess перемешается с нашим

    if kind == "git":
        d = git_root()
        if not d:
            print(C.red("  Каталог репозитория не найден."))
            return 1
        print(C.dim(f"  git -C {d} pull --ff-only"))
        code = subprocess.call(["git", "-C", d, "pull", "--ff-only"])
    elif kind == "pip":
        url = f"git+https://github.com/{REPO}.git@main"
        print(C.dim(f"  {sys.executable} -m pip install --upgrade {url}"))
        code = subprocess.call([sys.executable, "-m", "pip", "install",
                                "--upgrade", url])
    else:
        print(C.yellow("  Не понято, как утилита установлена."))
        print(C.dim(f"  Обновите вручную: git -C {module_dir()} pull --ff-only"))
        print(C.dim(f"  или заново: pip install --upgrade "
                    f"git+https://github.com/{REPO}.git@main"))
        return 1


    print()
    if code == 0:
        print(C.green("  Обновлено."))
        print(C.dim(f"  Проверка версии: python3 -c "
                    f"'import rpmfam_search; print(rpmfam_search.__version__)'"))
    else:
        print(C.red(f"  Не обновилось (код {code})."))
        print(C.dim("  Если правки в репозитории есть, а pull не помог — "
                    "проверь, не забыл ли сделать pull, и нет ли "
                    "незакоммиченных изменений: git status"))
    return 0 if code == 0 else 1


# ---------------------------------------------------------------- main

EPILOG = """\
ПЕРВЫЙ ЗАПУСК
  Утилита спросит, из какого документа читать фамилии. Пропустить
  нельзя: пока документ не выбран, ни одна команда не работает —
  кроме -h, --doc и --clean. Потом выбор запоминается.

  Документ должен быть доступен ВСЕМ, у кого есть ссылка
  (Google Docs → Доступ → Читатель для всех, у кого есть ссылка).

примеры
  rpmfam-search Амброус      по фамилии (можно часть: Амб)
  rpmfam-search sqW1nz       по нику представителя
  rpmfam-search 910442       по номеру паспорта или телефона
  rpmfam-search Кингсманн    опечатка -> покажет похожие
  rpmfam-search              новые фамилии за неделю
  rpmfam-search --all        весь список по алфавиту
  rpmfam-search --check      проверить, что данные разобрались верно

двойные фамилии ищутся так же, как обычные:
  rpmfam-search Блэйд-Арч   найдёт фамилию Арч
  rpmfam-search Хёдо         найдёт Вендеркольт, Вейл и Гроуз разом
  rpmfam-search -u           обновить утилиту

знаки в выводе
  *   подойдёт любой представитель фамилии
  ⚠   битая ссылка «относится к фамилии» — правьте у автора документа
  ⛔  фамилию нельзя выдавать (запрет команды)

запреты показываются, только пока фамилия есть в документе.
Уберут из документа — предупреждение исчезнет само.

обновления
  Утилита сама замечает новые версии и пишет об этом в конце вывода.
  Ничего не происходит само — обновляться нужно вручную:
  rpmfam-search --update

свой документ
  rpmfam-search --doc                    что подключено
  rpmfam-search --doc <ссылка> --name X  подключить и запомнить
  rpmfam-search --doc reset              отключить (потом снова спросит)

утилита только читает документ и ничего в нём не меняет.
Кеш и настройки удаляются командой --clean.
"""


# Кириллица, визуально неотличимая от латиницы. Нередко `--сheck` набирают
# с русской «с» — для argparse это незнакомый флаг, и человек не понимает,
# в чём дело. Меняем такие буквы в названиях флагов на латинские.
# К самому запросу не прикасаемся: «Арч» должен остаться «Арч».
HOMOGLYPHS = str.maketrans({
    "а": "a", "А": "A", "в": "b", "В": "B", "е": "e", "Е": "E",
    "ё": "e", "Ё": "E", "к": "k", "К": "K", "м": "m", "М": "M",
    "н": "h", "Н": "H", "о": "o", "О": "O", "р": "p", "Р": "P",
    "с": "c", "С": "C", "т": "t", "Т": "T", "у": "y", "У": "Y",
    "х": "x", "Х": "X", "і": "i", "І": "I", "ѕ": "s", "Ѕ": "S",
})


def fix_flags(argv: list[str]) -> list[str]:
    """Латинские буквы в названиях флагов: --сheck -> --check."""
    out = []
    for arg in argv:
        if len(arg) > 1 and arg.startswith("-"):
            out.append(arg.translate(HOMOGLYPHS))
        else:
            out.append(arg)
    return out


class Parser(argparse.ArgumentParser):
    """Вместо голого «unrecognized arguments» перечисляет доступные флаги."""

    def error(self, message):
        self.print_usage(sys.stderr)
        extra = ""
        if "unrecognized" in message or "invalid choice" in message:
            flags = []
            for act in self._actions:
                if not act.option_strings or act.help == argparse.SUPPRESS:
                    continue
                flags.append(act.option_strings[0])
            extra = (f"\n  доступные флаги: {', '.join(flags)}"
                     f"\n  подробности: {self.prog} -h\n")
        self.exit(2, f"{self.prog}: ошибка: {message}\n{extra}")


def build_parser() -> argparse.ArgumentParser:
    ap = Parser(
        prog=APP,
        description="Поиск зарегистрированных фамилий, их представителей и "
                    "заместителей. Без аргументов показывает новые фамилии. "
                    "При первом запуске нужно выбрать документ с фамилиями.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    ap.add_argument("query", nargs="*",
                    help="фамилия, имя, ник, номер паспорта или телефона")
    ap.add_argument("-a", "--all", action="store_true", help="список всех фамилий")
    ap.add_argument("-r", "--refresh", action="store_true", help="обновить кеш")
    ap.add_argument("-c", "--contacts", action="store_true",
                    help="искать только по имени/нику представителя")
    ap.add_argument("-s", "--since", type=int, default=NEW_WINDOW_DAYS, metavar="ДНЕЙ",
                    help="за сколько дней показать новые фамилии "
                         f"(по умолчанию {NEW_WINDOW_DAYS}, 0 — только с прошлого раза)")
    ap.add_argument("--doc", nargs="?", const="", metavar="ССЫЛКА",
                    help="показать, подключить или сбросить документ "
                         "(--doc <ссылка>, --doc reset)")
    ap.add_argument("--name", metavar="НАЗВАНИЕ",
                    help="подпись документа при подключении через --doc")
    ap.add_argument("--check", action="store_true",
                    help="проверить целостность разобранных данных")
    ap.add_argument("--clean", action="store_true",
                    help="удалить кеш, историю и настройки")
    ap.add_argument("--keep-config", action="store_true",
                    help="с --clean не трогать подключённый документ")
    ap.add_argument("-u", "--update", action="store_true",
                    help="обновить утилиту до последней версии")
    ap.add_argument("-V", "-v", "--version", action="version",
                    version=f"{APP} {__version__}")
    ap.add_argument("-N", "--check-name", metavar="ИМЯ",
                    help="проверить РП имя по правилам мерии")
    ap.add_argument("--forebears", action="store_true",
                    help="с --check-name: спросить охват на forebears.io")
    ap.add_argument("--no-color", action="store_true", help="без цветов")
    return ap


def resolve_context() -> dict:
    """Документ обязателен: пока он не выбран, работать не с чем."""
    cfg = load_config()
    doc_id = cfg.get("doc_id")
    if not doc_id:
        return {"doc_id": None, "cache": None, "snap": None,
                "label": "не подключён", "custom": False}
    cache, snap = doc_paths(doc_id)
    label = cfg.get("label") or "свой документ"
    return {"doc_id": doc_id, "cache": cache, "snap": snap, "label": label,
            "custom": True}


def fetch_and_check(doc_id: str) -> tuple[dict, str | None]:
    """Скачивает документ и проверяет, что это реестр фамилий.

    Возвращает (разобранный документ, текст ошибки)."""
    path = os.path.join(cache_dir(), f"doc-{doc_id[:12]}.txt")
    try:
        raw = fetch(doc_id, path, True)
    except SystemExit:
        return None, "не удалось скачать документ — нет сети или ссылка закрыта"
    db = parse(raw)
    problems = registry_problems(db, strict=True)
    if problems:
        return db, "; ".join(problems)
    return db, None


def first_run_setup() -> int:
    """Первичный выбор документа. Пропустить нельзя — без него нечего искать."""
    print(C.bold("Первый запуск — нужно выбрать документ с фамилиями."))
    print()
    print("Утилита читает реестр из Google Docs. Документ должен быть")
    print("доступен ВСЕМ, у кого есть ссылка: в Google Docs откройте")
    print("«Доступ» и поставьте «Читатель для всех, у кого есть ссылка».")
    print()
    print("  1 — реестр RPM North (по умолчанию)")
    print("  q — выйти")
    print()

    while True:
        try:
            raw = input("Вставьте ссылку или ID документа: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1

        if raw.lower() in ("q", "q!", "exit", "выход", "quit"):
            print(C.yellow("Пока не выбран документ, утилита не работает."))
            print(C.dim("Вернуться: rpmfam-search --doc <ссылка>"))
            return 1

        if raw == "1":
            doc_id, label = SUGGESTED_DOC_ID, SUGGESTED_LABEL
        else:
            doc_id = extract_doc_id(raw)
            label = norm(raw)[:60] if not raw.startswith("http") else "свой документ"

        if not doc_id:
            print(C.red("  не понял ссылку. Нужно что-то вроде:"))
            print(C.dim("    https://docs.google.com/document/d/<ID>/edit"))
            print(C.dim("    или сам ID"))
            continue

        print(C.dim("  проверяю документ…"))
        db, err = fetch_and_check(doc_id)
        if err:
            print(C.red("  не подходит: " + err))
            print(C.dim("  попробуйте другую ссылку или нажмите q для выхода"))
            print()
            continue

        err = connect_doc(doc_id, label)
        if err:
            print(C.red("  " + err))
            print(C.dim("  документ не подключён, ничего не изменилось"))
            print(C.dim("  попробуйте другую ссылку или нажмите q для выхода"))
            print()
            continue

        print()
        print(C.green(f"Готово, подключён документ: {label}"))
        print(C.dim(f"  фамилий: {len(entries(db))} | сносок: {len(db['notes'])}"))
        print(C.dim(f"  выбор сохранён в {config_path()}"))
        print()
        print(C.dim("Сменить документ: rpmfam-search --doc <ссылка>"))
        return 0


def run(argv: list[str] | None = None) -> int:
    fix_stdio()
    args = build_parser().parse_args(argv)

    C.on = not args.no_color and not os.environ.get("NO_COLOR") \
        and sys.stdout.isatty()

    if args.clean:
        return clean(args.keep_config)

    if args.update:
        return perform_update()

    ctx = resolve_context()

    if args.doc is not None:
        return handle_doc(args.doc, ctx, args.name)

    # Документ не выбран — работать не с чем. -h обработан argparse выше,
    # --doc и --clean уже выше, поэтому сюда попадает всё остальное.
    if ctx["doc_id"] is None:
        if not sys.stdin.isatty():
            sys.stderr.write(C.red(
                "Документ не подключён — сначала выберите его:\n"
                "  rpmfam-search --doc <ссылка>\n"))
            return 2
        code = first_run_setup()
        if code:
            return code
        ctx = resolve_context()

    query = norm(" ".join(args.query))
    try:
        raw = fetch(ctx["doc_id"], ctx["cache"], args.refresh)
    except SystemExit as e:
        return e.code or 2

    db = parse(raw)
    problems = registry_problems(db)
    if problems:
        sys.stderr.write(C.red("! документ не прочитан:\n"))
        for pr in problems:
            sys.stderr.write("  ! " + pr + "\n")
        sys.stderr.write(C.dim(f"  подключить другой: rpmfam-search --doc <ссылка>\n"))
        return 2

    entries_list = entries(db)
    links = build_links(entries_list)

    if args.check:
        return run_check(db, entries_list, ctx)

    if args.check_name:
        return print_name_report(args.check_name, entries_list, links,
                                 args.forebears)

    # Без запроса показываем новые фамилии. Пустая строка и пробелы — тоже.
    if not query and not args.all:
        snap, fresh, gone, first = update_snapshot(ctx["snap"], entries_list)
        show_new(entries_list, links, snap, fresh, gone,
                 max(args.since, 0) * 86400, first, ctx)
        return 0

    update_snapshot(ctx["snap"], entries_list)

    if args.all:
        cur = None
        for e in entries_list:
            head = e["name"][0].upper()
            if head != cur:
                cur = head
                print(f"\n{C.bold(cur)}")
            mark = " " + C.yellow("*") if e["star"] else ""
            if ban_reason(e["name"]):
                mark += " " + C.red("⛔")
            elif e.get("ref") and not resolve_ref(entries_list, e["ref"]):
                mark += " " + C.red("⚠")
            print("  " + e["name"] + mark)
        stars = [e["name"] for e in entries_list if e["star"]]
        broken = [e["name"] for e in entries_list
                  if e.get("ref") and not resolve_ref(entries_list, e["ref"])]
        banned_here = [e["name"] for e in entries_list if ban_reason(e["name"])]
        print(C.dim(f"\nвсего фамилий: {len(entries_list)}"
                    f" | со звёздочкой: {len(stars)}"
                    + (f" | битых ссылок: {len(broken)}" if broken else "")
                    + (f" | запрещено: {len(banned_here)}" if banned_here else "")))
        if stars:
            print()
            print(C.yellow(C.bold("* — подойдёт любой представитель фамилии"))
                  + C.dim(" (условия выдачи отличаются от обычных)"))
            print(C.dim("  Уточняй у Главы фамилии заранее, до выдачи. "
                        "Фамилии: " + ", ".join(stars)))
        for n in banned_here:
            print(C.red("⛔ ") + C.yellow("нельзя выдавать: ") + n)
        if broken:
            print(C.yellow("⚠ ") + C.yellow("битая ссылка в документе: ")
                  + ", ".join(broken)
                  + C.dim(" — надо поправить у автора документа"))
        footer(db, ctx)
        return 0

    status = do_search(query, entries_list)

    if status[0] == "surname":
        for e in status[1]:
            print_entry(e, entries_list, links=links)
        footer(db, ctx)
        return 0

    _, contains, persons, numbers = status

    if not contains and not persons and not numbers:
        print(C.red(f'Фамилия "{query}" не зарегистрирована.'))
        near = suggestions(query, entries_list)
        if near:
            print(C.dim("похожее: " + ", ".join(near)))
        footer(db, ctx)
        return 1

    if numbers:
        print(C.bold(f'Найдено по номеру: "{query}"') + "\n")
        for e, p in numbers:
            print(f'{C.bold("Зарегестрированная фамилия:")} "{e["name"]}"')
            print(f"  {C.green('Роль:')} {p['role']}  {fmt_person_marked(p, query)}")
        if not contains and not persons:
            footer(db, ctx)
            return 0
        print()

    if persons and (args.contacts or not contains):
        print_person_hits(query, persons, contains, numbers, args.contacts)
        if not contains:
            footer(db, ctx)
            return 0

    if contains:
        if (persons or numbers) and not args.contacts:
            print(C.dim("Также частичное совпадение по фамилии:"))
        for e in contains:
            print_entry(e, entries_list, links=links)
    footer(db, ctx)
    return 0


# Команды, после которых уведомление об обновлении показывать не нужно:
# они либо и так служебные, либо вывод уходит в пайп.
QUIET_NOTICE = {"-h", "--help", "--clean", "-u", "--update", "--version", "-v", "-V"}


def main(argv: list[str] | None = None) -> int:
    args = fix_flags(list(sys.argv[1:] if argv is None else argv))
    show_notice = not (set(args) & QUIET_NOTICE) and sys.stdout.isatty()

    code = run(args)

    if show_notice:
        try:
            print_update_notice()
        except Exception:      # уведомление не должно ломать выдачу
            pass
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
