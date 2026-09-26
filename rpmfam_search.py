#!/usr/bin/env python3
"""rpmfam-search — поиск по списку зарегистрированных фамилий RPM North.

Данные читаются из Google Docs через публичный экспорт в txt и кэшируются
локально. Утилита ничего не изменяет в документе и никуда не отправляет данные.

Проект написан с помощью ИИ и может содержать ошибки.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

APP = "rpmfam-search"

# Документ по умолчанию. Переопределяется через --doc или конфиг.
DEFAULT_DOC_ID = "1a_7aQdGgEZadPHEW7WEq8rIaWDm2lcx4mXs7W-PzAwA"
DEFAULT_LABEL = "RPM North"

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
    _write_private(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def extract_doc_id(value: str) -> str | None:
    """Принимает полную ссылку, ссылку с /edit?pli=1 или голый ID."""
    v = (value or "").strip().strip("<>\"'")
    m = RE_DOC_URL.search(v)
    if m:
        return m.group(1)
    bare = v.split("?")[0].split("/")[-1]
    return bare if RE_DOC_ID.match(bare) else None


# ---------------------------------------------------------------- загрузка

def fetch(doc_id: str, path: str, force: bool = False) -> str:
    if not force and os.path.isfile(path):
        if time.time() - os.path.getmtime(path) < CACHE_TTL:
            with open(path, encoding="utf-8-sig") as f:
                return f.read()

    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8-sig", errors="replace")
    except urllib.error.URLError as e:
        if os.path.isfile(path):
            sys.stderr.write(C.yellow(
                f"! не удалось обновить документ ({e.reason}), беру кеш\n"))
            with open(path, encoding="utf-8-sig") as f:
                return f.read()
        sys.stderr.write(C.red(f"! не удалось загрузить документ: {e.reason}\n"))
        sys.stderr.write(C.dim("  проверь ссылку: rpmfam-search --doc <ссылка>\n"))
        raise SystemExit(2)

    _write_private(path, raw)
    return raw


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


def print_entry(e: dict, entries_list, depth: int = 0, seen=None, links=None,
                tag: str = "") -> None:
    seen = seen or set()
    pad = "  " * depth

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
            gone = f"(фамилия {e['ref']} больше не в документе)"
            print(f"{pad}  {C.dim(gone)}")
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

def do_search(q: str, entries_list):
    nq = key(q)
    lq = loose(q)
    exact = [e for e in entries_list
             if key(e["name"]) == nq or (lq and loose(e["name"]) == lq)]
    if exact:
        return "surname", exact, [], []

    contains = [e for e in entries_list
                if (nq and nq in key(e["name"]))
                or (lq and lq in loose(e["name"]))]

    persons = []
    for e in entries_list:
        for b in e["blocks"]:
            for p in b["people"]:
                hay = key(" ".join(filter(None, [p["name"], p.get("nick") or ""])))
                hayl = loose(" ".join(filter(None, [p["name"], p.get("nick") or ""])))
                if (nq and nq in hay) or (lq and lq in hayl):
                    persons.append((e, p))

    numbers = []
    if nq and RE_DIGITS.match(nq):
        for e in entries_list:
            for b in e["blocks"]:
                for p in b["people"]:
                    digits = re.sub(r"\D", "", " ".join(
                        filter(None, [p.get("passport") or "", p.get("phone") or ""])))
                    if nq in digits:
                        numbers.append((e, p))

    return "contains", contains, persons, numbers


def suggestions(q: str, entries_list) -> list[str]:
    """Похожие фамилии в исходном регистре, а не в нижнем."""
    by_key = {key(e["name"]): e["name"] for e in entries_list}
    return [by_key[m] for m in difflib.get_close_matches(
        key(q), list(by_key), n=5, cutoff=0.55) if m in by_key]


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
    if not problems:
        print(C.green("  всё в порядке, данные разобраны без потерь"))
    return 1 if problems else 0


# ---------------------------------------------------------------- --clean

def clean(keep_config: bool) -> int:
    """Удаляет только свои файлы."""
    cfg = load_config()
    doc_ids = {DEFAULT_DOC_ID}
    if cfg.get("doc_id"):
        doc_ids.add(cfg["doc_id"])

    removed, freed = [], 0
    base = cache_dir()
    known = {config_path()}
    for did in doc_ids:
        known.update(doc_paths(did))
    for p in sorted(known):
        if not os.path.isfile(p):
            continue
        try:
            freed += os.path.getsize(p)
            os.remove(p)
            removed.append(p)
        except OSError as e:
            sys.stderr.write(C.yellow(f"! не удалось удалить {p}: {e}\n"))

    if not keep_config:
        try:
            os.remove(config_path())
            removed.append(config_path())
        except OSError:
            pass

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
    print(C.dim("\nСам скрипт не тронут. Удалить его: rm "
                + os.path.expanduser(sys.argv[0])))
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
        print(C.green("Документ сброшен."))
        print(C.dim(f"Теперь используется документ по умолчанию: {DEFAULT_LABEL}"))
        return 0

    if not value:
        print(C.bold("Текущий документ:"))
        print(f"  {C.dim('источник:')} {ctx['label']}")
        print(f"  {C.dim('id:')}       {ctx['doc_id']}")
        print(f"  {C.dim('откуда:')}   "
              + ("конфиг " + config_path() if cur else "встроенный по умолчанию"))
        print()
        print(C.dim("задать другой:  rpmfam-search --doc <ссылка>"))
        print(C.dim("вернуть встроенный:  rpmfam-search --doc reset"))
        return 0

    doc_id = extract_doc_id(value)
    if not doc_id:
        print(C.red("Не понял, что это за ссылка."))
        print(C.dim("нужно: https://docs.google.com/document/d/<ID>/edit"
                    " или сам <ID>"))
        return 1

    print(C.dim(f"проверяю документ {doc_id}…"))
    try:
        raw = fetch(doc_id, os.path.join(cache_dir(), f"doc-{doc_id[:12]}.txt"), True)
    except SystemExit as e:
        return e.code or 2

    db = parse(raw)
    problems = registry_problems(db, strict=True)
    if problems:
        print(C.red("Это не похоже на реестр фамилий RPM:"))
        for pr in problems:
            print("  ! " + pr)
        print(C.dim("\nдокумент не подключён, ничего не изменилось"))
        return 1

    cfg["doc_id"] = doc_id
    label = norm(name) if name else None
    if not label:
        # без подписи показываем что-то читаемое, а не простыню из ссылки
        label = "свой документ" if value.strip().startswith("http") else norm(value)
    cfg["label"] = label[:60]
    save_config(cfg)
    print(C.green(f"Готово. Подключён документ: {cfg['label']}"))
    print(C.dim(f"  фамилий: {len(entries(db))} | сносок: {len(db['notes'])}"))
    print(C.dim(f"  сохранено в {config_path()} — вводить ссылку больше не нужно"))
    if not name:
        print(C.dim("  подписать: rpmfam-search --doc <ссылка> --name \"Название\""))
    return 0


# ---------------------------------------------------------------- main

EPILOG = """\
что можно искать
  Фамилия            rpmfam-search Амброус       (можно часть: Амб)
  Имя                rpmfam-search "Григорий"
  Ник                rpmfam-search sqW1nz
  Номер паспорта     rpmfam-search 910442         (или RPM-910442)
  Телефон            rpmfam-search 14882930
  Ошибка в фамилии   rpmfam-search Кингсманн  ->  покажет похожие

примеры
  rpmfam-search                новые фамилии за неделю
  rpmfam-search -s 1           новые за сегодня
  rpmfam-search -s 0           только с прошлого запуска
  rpmfam-search --all          весь список по алфавиту
  rpmfam-search Аккерман       покажет и двойные фамилии того же человека
  rpmfam-search -c Ли          искать только по представителю
  rpmfam-search -r Амброус     обновить данные, игнорируя кеш
  rpmfam-search --check        проверить, что данные разобрались верно
  rpmfam-search --doc          показать, какой документ подключён
  rpmfam-search --clean        удалить кеш и историю (скрипт останется)

что означают пометки в выводе
  * подойдёт любой представитель    условия выдачи отличаются
  ⚠                               предупреждение из документа
  Двойные фамилии того же человека   у человека две фамилии
  → относится к фамилии           фамилия привязана к другой

где хранятся данные
  кеш и история фамилий — в системном каталоге кеша
  подключённый документ — в конфиге пользователя
  Всё это удаляется командой --clean. Утилита только читает документ,
  ничего в нём не меняет и никуда ничего не отправляет.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=APP,
        description="Поиск зарегистрированных фамилий RPM North, их представителей "
                    "и заместителей. Без аргументов показывает новые фамилии.",
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
    ap.add_argument("--no-color", action="store_true", help="без цветов")
    return ap


def resolve_context() -> dict:
    cfg = load_config()
    doc_id = cfg.get("doc_id") or DEFAULT_DOC_ID
    cache, snap = doc_paths(doc_id)
    if cfg.get("doc_id"):
        label = cfg.get("label") or "свой документ"
    else:
        label = DEFAULT_LABEL
    return {"doc_id": doc_id, "cache": cache, "snap": snap, "label": label,
            "custom": bool(cfg.get("doc_id"))}


def main(argv: list[str] | None = None) -> int:
    fix_stdio()
    args = build_parser().parse_args(argv)

    C.on = not args.no_color and not os.environ.get("NO_COLOR") \
        and sys.stdout.isatty()

    if args.clean:
        return clean(args.keep_config)

    ctx = resolve_context()

    if args.doc is not None:
        return handle_doc(args.doc, ctx, args.name)

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
            print("  " + e["name"] + (" " + C.yellow("*") if e["star"] else ""))
        stars = [e["name"] for e in entries_list if e["star"]]
        print(C.dim(f"\nвсего фамилий: {len(entries_list)}"
                    f" | со звёздочкой: {len(stars)}"))
        if stars:
            print()
            print(C.yellow(C.bold("* — подойдёт любой представитель фамилии"))
                  + C.dim(" (условия выдачи отличаются от обычных)"))
            print(C.dim("  Уточняй у Главы фамилии заранее, до выдачи. "
                        "Фамилии: " + ", ".join(stars)))
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
            print(f"  {C.green('Роль:')} {p['role']}  {fmt_person(p)}")
        if not contains and not persons:
            footer(db, ctx)
            return 0
        print()

    if persons and (args.contacts or not contains):
        print(C.bold(f'Найдено по представителю: "{query}"') + "\n")
        for e, p in persons:
            print(f'{C.bold("Зарегестрированная фамилия:")} "{e["name"]}"')
            print(f"  {C.green('Роль:')} {p['role']}  {fmt_person(p)}")
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
