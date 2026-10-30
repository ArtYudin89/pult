#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт из командной строки — для ИИ-агентов (Claude Code, Codex и других) и для людей.
Только стандартная библиотека, Python 3.9+.

Подключение:
  pult.py connect АДРЕС [--token-file ФАЙЛ]  адрес Пульта и ключ модели → .local/config.json
Чтение:
  pult.py brief [--project P | --area A]   сообщения для ИИ и что требует внимания — начало сессии
  pult.py ls [--project P] [--area A] [--status S] [--holder H] [--closed | --all]
  pult.py find СЛОВА…                       поиск, как на странице
  pult.py show КЛЮЧ [--journal N]           карточка: описание, шаги, сообщение для ИИ, модель, журнал
  pult.py log [--since 2d] [--by ID] [--project P] [--limit N]
  pult.py projects                          разделы и проекты с id
  pult.py url [КЛЮЧ]                        адрес страницы или задачи
  pult.py whoami                            проверка адреса и ключа
  pult.py digest                            утренняя сводка, как она придёт в Telegram
Запись:
  pult.py apply ФАЙЛ.json                   пакет изменений — так ИИ двигает карточки в конце сессии
  pult.py add --project P --title T [поля]  pult.py set КЛЮЧ [поля]   pult.py done КЛЮЧ [--result R]
  pult.py note КЛЮЧ ТЕКСТ                   запись в журнал
  pult.py step КЛЮЧ add ТЕКСТ | check N | uncheck N | remove N   шаги карточки: N — номер или кусок текста
  pult.py import ФАЙЛ.json                  перенос: участники, разделы, проекты, задачи
Вложения:
  pult.py attach КЛЮЧ ФАЙЛ… [-m ТЕКСТ]      прикрепить файлы (до 25 МБ каждый); список — в show
  pult.py fetch НОМЕР [-o ПУТЬ]             скачать вложение (по умолчанию — во временную папку pult-files)
  pult.py detach НОМЕР                      удалить вложение
Участники и доступ:
  pult.py people                            кто есть, какой доступ, входы и ключи
  pult.py person ID --name N [--kind person|model] [--owner ХОЗЯИН] [--role owner|member] [--note T] [--off|--on]
  pult.py grant ID ОБЛАСТЬ ДОСТУП           ОБЛАСТЬ: all | area:ID | ID проекта; ДОСТУП: view | edit | - (снять)
  pult.py invite ID [--days 7] [--note T]   одноразовая ссылка для входа человеку → файл в .local/
  pult.py key ID [--label L]                постоянный ключ модели → файл в .local/
  pult.py revoke НАЧАЛО_ХЕША | iНОМЕР       отозвать ключ или ссылку-приглашение
Ключи:
  pult.py keygen УЧАСТНИК --label L [--login-url] [--use]

Адрес и ключ — в .local/config.json рядом с программой ({"url": …, "token_file": …});
переменные PULT_URL, PULT_TOKEN, PULT_CONFIG их перекрывают. Ключ в вывод не печатается никогда.
Даты: 27.10, 27.10.2026, 2026-10-27, +3 (дней), +2w, сегодня, завтра; «-» — убрать значение.

Одновременная правка. Сессия помечается PULT_SESSION, а в Claude Code — первыми знаками CLAUDE_CODE_SESSION_ID,
и запоминает в .local/seen/, какими видела карточки: первое чтение Пульта — все задачи, show — открытую
карточку. set, done, delete и шаг по номеру уходят с этой версией: если с тех пор другие поменяли те же
поля, сервер не затрёт их правку, а pult.py покажет, что изменилось. Правки других полей сливаются сами.
"""
import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, ".local")
STATUS_RU = {"inbox": "Входящие", "next": "Следующие", "doing": "В работе", "wait": "Ждёт",
             "done": "Готово", "dropped": "Снято"}
STATUS_ALIASES = {"inbox": "inbox", "входящие": "inbox", "next": "next", "следующие": "next", "дальше": "next",
                  "doing": "doing", "работа": "doing", "в работе": "doing", "wait": "wait", "ждёт": "wait",
                  "ждет": "wait", "done": "done", "готово": "done", "dropped": "dropped", "снято": "dropped",
                  "отменено": "dropped"}
CLOSED = ("done", "dropped")
GROUP_ORDER = ("late", "doing", "wait", "next", "inbox")
GROUP_RU = {"late": "Просрочено", "check": "Пора проверить", "doing": "В работе", "wait": "Ждёт",
            "next": "Следующие", "inbox": "Входящие"}
FIELD_RU = {"project": "проект", "title": "название", "status": "статус", "holder": "держит",
            "next": "следующий шаг", "wait": "ждём", "check": "проверить", "due": "срок", "done_on": "закрыта",
            "notes": "описание", "mem": "подробности", "checklist": "шаги", "prompt": "сообщение для ИИ",
            "model": "модель ИИ", "effort": "усилие ИИ", "comment": "запись в журнал"}
TEXT_FIELDS = ("title", "next", "wait", "notes", "mem", "prompt", "model", "effort")
AI_FROM = {"task": "в карточке", "project": "из проекта", "area": "из раздела", "pult": "для всего Пульта"}
RUN_RU = {"queued": "в очереди", "started": "запускается", "working": "идёт", "blocked": "ждёт разрешения на ПК",
          "asked": "ждёт ответа", "done": "готово", "failed": "ошибка", "stopped": "остановлена",
          "cancelled": "отменена"}
RUN_LIVE = ("queued", "started", "working", "blocked", "asked")
DATE_FIELDS = ("check", "due", "done_on")
FOLD = str.maketrans("авсенкмортхуё", "abcehkmoptxye")
LOOKALIKE = str.maketrans("АВСЕНКМОРТХУ", "ABCEHKMOPTXY")
LEVEL_RU = {"view": "читает", "edit": "правит"}
LEVEL_ALIASES = {"view": "view", "читает": "view", "чтение": "view", "edit": "edit", "правит": "edit",
                 "правка": "edit", "-": None, "нет": None, "снять": None}
SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
SEEN_DAYS = 14


class PultError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.payload = {}


# ---------- настройки и связь ----------

def load_config():
    path = os.environ.get("PULT_CONFIG") or os.path.join(LOCAL, "config.json")
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["url"] = (os.environ.get("PULT_URL") or cfg.get("url") or "").rstrip("/")
    token = os.environ.get("PULT_TOKEN")
    if not token and cfg.get("token_file"):
        tf = cfg["token_file"]
        tf = tf if os.path.isabs(tf) else os.path.join(os.path.dirname(path), tf)
        if os.path.exists(tf):
            with open(tf, encoding="utf-8") as f:
                token = f.read().strip()
    cfg["token"] = token or ""
    if not cfg["url"]:
        raise PultError(0, f"не задан адрес Пульта: {path} или PULT_URL")
    if not cfg["token"]:
        raise PultError(0, f"нет ключа: token_file в {path} или PULT_TOKEN")
    session = os.environ.get("PULT_SESSION")
    if session is None:
        session = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "")[:8]
    cfg["session"] = session if SESSION_RE.match(session) else ""
    cfg["seen_dir"] = os.environ.get("PULT_SEEN_DIR") or os.path.join(LOCAL, "seen")
    return cfg


def request(cfg, method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return json.loads(request_raw(cfg, method, path, data, headers)[0].decode("utf-8"))


def request_raw(cfg, method, path, data=None, headers=None, timeout=30):
    """Запрос с телом и ответом в байтах (так ходят вложения); возвращает (тело, заголовки)."""
    url = cfg["url"] + path
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + cfg["token"])
    if cfg.get("session"):
        req.add_header("X-Pult-Session", cfg["session"])
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    host = urllib.parse.urlsplit(url).hostname or ""
    handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost", "::1") else []
    if url.startswith("https:"):
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        err = PultError(e.code, payload.get("error") or f"HTTP {e.code}")
        err.payload = payload
        raise err from None
    except urllib.error.URLError as e:
        raise PultError(0, f"нет связи с Пультом ({cfg['url']}): {e.reason}") from None


def q(text):
    return urllib.parse.quote(text, safe="")


class Seen:
    """Какими эта сессия видела карточки: {номер: версия}. Без метки сессии ничего не хранит.

    Задача попадает сюда при первом чтении Пульта в сессии (или когда впервые появилась в нём) и
    обновляется только show. Свои правки сессии версию не сдвигают: сервер их узнаёт по метке и
    чужими не считает, а чужие правки после этой версии остаются видны при сверке.
    """

    def __init__(self, cfg):
        self.path = os.path.join(cfg["seen_dir"], cfg["session"] + ".json") if cfg.get("session") else None
        self.tasks = {}
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.tasks = json.load(f).get("tasks", {})
            except (OSError, ValueError):
                self.tasks = {}

    def note(self, tasks):
        fresh = {t["key"]: t["version"] for t in tasks if t["key"] not in self.tasks}
        if self.path and fresh:
            self.tasks.update(fresh)
            self.save()

    def saw(self, task):
        if self.path and task and self.tasks.get(task["key"]) != task["version"]:
            self.tasks[task["key"]] = task["version"]
            self.save()

    def version(self, key):
        return self.tasks.get(key)

    def save(self):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": self.tasks}, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        cutoff = time.time() - SEEN_DAYS * 86400
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if name.endswith(".json") and os.path.getmtime(full) < cutoff:
                os.remove(full)


def seen(cfg):
    if "_seen" not in cfg:
        cfg["_seen"] = Seen(cfg)
    return cfg["_seen"]


def get_state(cfg):
    st = request(cfg, "GET", "/api/state")
    seen(cfg).note(st["tasks"])
    return st


# ---------- разбор значений ----------

def norm_key(key):
    return str(key or "").strip().upper().translate(LOOKALIKE)


def parse_date(value, today=None):
    """Возвращает ISO-дату, None (убрать) или бросает ValueError."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("", "-", "нет", "none", "null"):
        return None
    today = today or dt.date.today()
    if s in ("сегодня", "today"):
        return today.isoformat()
    if s in ("завтра", "tomorrow"):
        return (today + dt.timedelta(days=1)).isoformat()
    m = re.match(r"^\+(\d{1,3})\s*(d|д|w|н|нед)?$", s)
    if m:
        n = int(m.group(1))
        return (today + dt.timedelta(days=n * (7 if m.group(2) in ("w", "н", "нед") else 1))).isoformat()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$", s)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        return dt.date(y, int(m.group(2)), int(m.group(1))).isoformat()
    return dt.date.fromisoformat(s).isoformat()


def parse_status(value):
    s = str(value or "").strip().lower()
    if s not in STATUS_ALIASES:
        raise ValueError(f"статус {value!r}: inbox, next, doing, wait, done, dropped (или по-русски)")
    return STATUS_ALIASES[s]


def clean_fields(raw, me_id):
    """Поля задачи из пакета или командной строки → тело запроса. «-» и null убирают значение."""
    body = {}
    for k, v in raw.items():
        if k in ("op", "key", "comment", "result", "version", "dropped"):
            continue
        if k in DATE_FIELDS:
            body[k] = parse_date(v)
        elif k == "status":
            body[k] = parse_status(v)
        elif k == "holder":
            s = "" if v is None else str(v).strip()
            body[k] = None if s in ("", "-", "никто", "none", "null") else (me_id if s in ("me", "я") else s)
        elif k == "checklist":
            if not isinstance(v, list):
                raise ValueError("checklist: нужен список")
            body[k] = [{"t": i, "on": False} if isinstance(i, str) else i for i in v]
        elif k in TEXT_FIELDS:
            body[k] = "" if v is None or str(v).strip() == "-" else str(v)
        elif k == "project":
            body[k] = str(v)
        else:
            raise ValueError(f"неизвестное поле {k!r}")
    if raw.get("comment"):
        body["comment"] = str(raw["comment"])
    return body


# ---------- вид ----------

def ddmm(iso):
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso[:10])
    return d.strftime("%d.%m") if d.year == dt.date.today().year else d.strftime("%d.%m.%Y")


def human_size(n):
    if n < 1024:
        return f"{n} Б"
    if n < 1 << 20:
        return f"{round(n / 1024)} КБ"
    return f"{n / (1 << 20):.1f} МБ".replace(".", ",")


def stamp(iso):
    try:
        d = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso or "")
    return d.strftime("%d.%m %H:%M") if d.year == dt.date.today().year else d.strftime("%d.%m.%Y %H:%M")


class View:
    def __init__(self, st):
        self.st = st
        self.today = st["today"]
        self.actors = {a["id"]: a for a in st["actors"]}
        self.areas = {a["id"]: a for a in st["areas"]}
        self.projects = {p["id"]: p for p in st["projects"]}

    def actor(self, aid):
        return self.actors[aid]["name"] if aid in self.actors else (aid or "никто")

    def where(self, t):
        p = self.projects.get(t["project"])
        a = self.areas.get(p["area"]) if p else None
        return (a["name"] + " › " if a else "") + (p["name"] if p else t["project"])

    def is_open(self, t):
        return t["status"] not in CLOSED

    def late(self, t):
        return self.is_open(t) and bool(t["due"]) and t["due"] < self.today

    def check_due(self, t):
        return self.is_open(t) and bool(t["check"]) and t["check"] <= self.today

    def lamp(self, t):
        return "late" if self.late(t) else t["status"]

    def step(self, t):
        return ("ждём: " + t["wait"]) if self.lamp(t) == "wait" and t["wait"] else (t["next"] or "")

    def chip(self, t):
        if not self.is_open(t) and t["done_on"]:
            return ("снято " if t["status"] == "dropped" else "готово ") + ddmm(t["done_on"])
        if self.late(t):
            return "ПРОСРОЧЕНО, срок " + ddmm(t["due"])
        if self.check_due(t):
            return "пора проверить, " + ddmm(t["check"])
        if t["due"]:
            return "срок " + ddmm(t["due"])
        if t["check"]:
            return "проверить " + ddmm(t["check"])
        return ""

    def line(self, t, project=True, width=0):
        bits = [t["title"]]
        if project:
            bits.append(self.where(t))
        s = self.step(t)
        if s:
            bits.append(s)
        c = self.chip(t)
        if c:
            bits.append(c)
        bits.append(self.actor(t["holder"]) if t["holder"] else "никто")
        text = " · ".join(bits)
        if width and len(text) > width:
            text = text[:width - 1] + "…"
        return f"{t['key']:<8} {text}"

    @staticmethod
    def ordered(tasks):
        """Как на странице: ближайшие срок или проверка сверху, дальше — недавно изменённые."""
        out = sorted(tasks, key=lambda t: t["updated_at"] or "", reverse=True)
        out.sort(key=lambda t: t["due"] or t["check"] or "9999-12-31")
        return out

    def filtered(self, args):
        tasks = self.st["tasks"]
        if getattr(args, "project", None):
            want = set(args.project.split(","))
            unknown = want - set(self.projects)
            if unknown:
                raise PultError(0, "нет проекта: " + ", ".join(sorted(unknown)) + " (pult.py projects)")
            tasks = [t for t in tasks if t["project"] in want]
        if getattr(args, "area", None):
            if args.area not in self.areas:
                raise PultError(0, f"нет раздела {args.area!r}")
            tasks = [t for t in tasks if self.projects.get(t["project"], {}).get("area") == args.area]
        return tasks


def matches(view, t, words):
    hay = " ".join([t["key"], t["title"], view.where(t), t["next"] or "", t["wait"] or "", t.get("prompt") or "",
                    view.actor(t["holder"]) if t["holder"] else ""]).lower().translate(FOLD)
    return all(w in hay for w in words)


def print_groups(view, tasks, limit=None, project=True, width=0):
    groups = {k: [] for k in ("late", "check", "doing", "wait", "next", "inbox")}
    for t in tasks:
        if not view.is_open(t):
            continue
        k = view.lamp(t)
        if k != "late" and view.check_due(t):
            k = "check"
        groups[k].append(t)
    shown = 0
    for k in ("late", "check", "doing", "wait", "next", "inbox"):
        items = view.ordered(groups[k])
        if not items:
            continue
        print(f"{GROUP_RU[k]} ({len(items)})")
        for t in items[:limit] if limit else items:
            print("  " + view.line(t, project=project, width=width))
            shown += 1
        if limit and len(items) > limit:
            print(f"  … ещё {len(items) - limit}")
    return shown


# ---------- команды чтения ----------

def cmd_brief(cfg, args):
    st = get_state(cfg)
    v = View(st)
    tasks = v.filtered(args)
    scoped = bool(args.project or args.area)
    open_n = sum(1 for t in tasks if v.is_open(t))
    scope = ""
    if args.project:
        scope = " · " + ", ".join(v.projects[p]["name"] for p in args.project.split(","))
    elif args.area:
        scope = " · " + v.areas[args.area]["name"]
    print(f"Пульт · {ddmm(st['today'])}{scope} · открытых {open_n} · {cfg['url']}")
    live = [t for t in v.ordered(tasks) if (t.get("run") or {}).get("status") in RUN_LIVE]
    if live:
        print(f"Сессии ИИ ({len(live)}) — их ведёт исполнитель, вручную не браться")
        for t in live:
            print("  " + v.line(t, project=not args.project, width=0 if scoped else 160)
                  + f" · сессия: {RUN_RU[t['run']['status']]}")
    asks = [t for t in v.ordered(tasks) if t.get("prompt") and v.is_open(t)]
    if asks:
        print(f"Сообщения для ИИ ({len(asks)}) — поручения людей, начать с них")
        for t in asks:
            print("  " + v.line(t, project=not args.project, width=0 if scoped else 160))
            text = " / ".join(t["prompt"].splitlines())
            print("           «" + (text if len(text) <= 300 else text[:299] + "…") + "»")
    if not print_groups(v, tasks, limit=None if scoped else 8, project=not args.project, width=0 if scoped else 160):
        print("Открытых задач нет.")
    week = (dt.date.fromisoformat(st["today"]) - dt.timedelta(days=7)).isoformat()
    closed = [t for t in tasks if not v.is_open(t) and (t["done_on"] or "") >= week]
    if closed and scoped:
        print(f"Закрыто за неделю ({len(closed)})")
        for t in sorted(closed, key=lambda x: x["done_on"] or "", reverse=True):
            print("  " + v.line(t, project=False))
    since = (dt.date.fromisoformat(st["today"]) - dt.timedelta(days=2)).isoformat()
    log = request(cfg, "GET", "/api/journal?" + urllib.parse.urlencode({"since": since, "limit": 300}))["journal"]
    others = [e for e in log if e["actor"] != st["me"]["id"]]
    if others:
        who = sorted({v.actor(e["actor"]) for e in others})
        print(f"За два дня изменений от других: {len(others)} ({', '.join(who)}) — pult.py log --since 2d")
    mine = [e for e in log if e["actor"] == st["me"]["id"] and e.get("session")
            and e["session"] != cfg.get("session")]
    if mine:
        print(f"От других сессий {st['me']['name']} за два дня: {len(mine)} "
              f"(сессии {', '.join(sorted({e['session'] for e in mine}))}) — pult.py log --since 2d --by me")


def cmd_ls(cfg, args):
    st = get_state(cfg)
    v = View(st)
    tasks = v.filtered(args)
    if args.status:
        want = {parse_status(s) for s in args.status.split(",")}
        tasks = [t for t in tasks if t["status"] in want]
    elif args.closed:
        tasks = [t for t in tasks if not v.is_open(t)]
    elif not args.all:
        tasks = [t for t in tasks if v.is_open(t)]
    if args.holder:
        h = None if args.holder in ("-", "никто") else (st["me"]["id"] if args.holder in ("me", "я") else args.holder)
        tasks = [t for t in tasks if t["holder"] == h]
    rank = {k: i for i, k in enumerate(GROUP_ORDER + CLOSED)}
    tasks = v.ordered(tasks)
    tasks.sort(key=lambda t: rank[v.lamp(t)])
    for t in tasks:
        print(f"{STATUS_RU[t['status']] if not v.late(t) else 'Просрочено':<11}" + v.line(t, project=not args.project))
    print(f"— {len(tasks)} {plural(len(tasks), 'задача', 'задачи', 'задач')}")


def plural(n, one, few, many):
    a, b = n % 10, n % 100
    return one if a == 1 and b != 11 else few if 2 <= a <= 4 and not 12 <= b <= 14 else many


def cmd_find(cfg, args):
    st = get_state(cfg)
    v = View(st)
    words = [w.lower().translate(FOLD) for w in args.words]
    found = v.ordered([t for t in st["tasks"] if matches(v, t, words)])
    found.sort(key=lambda t: not v.is_open(t))
    for t in found:
        print(f"{STATUS_RU[t['status']]:<11}" + v.line(t))
    print(f"— найдено {len(found)}")


def via_text(e):
    bits = [x for x in (e.get("via"), e.get("session")) if x]
    return f" [{' · '.join(bits)}]" if bits else ""


def cmd_show(cfg, args):
    st = get_state(cfg)
    v = View(st)
    r = request(cfg, "GET", f"/api/task/{q(norm_key(args.key))}?journal={args.journal}")
    t = r["task"]
    seen(cfg).saw(t)
    print(f"{t['key']}  {t['title']}")
    print(f"  проект:   {v.where(t)} ({t['project']})")
    print(f"  статус:   {STATUS_RU[t['status']]}" + ("  — ПРОСРОЧЕНО" if v.late(t) else ""))
    print(f"  держит:   {v.actor(t['holder']) if t['holder'] else 'никто'}")
    ai = t.get("ai") or {}
    bits = [f"{label} {ai[k]} ({AI_FROM.get(ai.get(k + '_from'), '?')})"
            for k, label in (("model", "модель"), ("effort", "усилие")) if ai.get(k)]
    if bits:
        print("  ИИ:       " + " · ".join(bits))
    if t["next"]:
        print(f"  {'итог:    ' if not v.is_open(t) else 'дальше:  '} {t['next']}")
    if t["wait"]:
        print(f"  ждём:     {t['wait']}")
    for k, label in (("check", "проверить"), ("due", "срок"), ("done_on", "закрыта")):
        if t[k]:
            print(f"  {label + ':':<11}{ddmm(t[k])}")
    mem = t["mem"] or v.projects.get(t["project"], {}).get("mem", "")
    if mem:
        print(f"  подробно: {mem}")
    print(f"  версия {t['version']}, создана {stamp(t['created_at'])} ({v.actor(t['created_by'])}), "
          f"изменена {stamp(t['updated_at'])} ({v.actor(t['updated_by'])})")
    print(f"  ссылка:   {cfg['url']}/#t={q(t['key'])}")
    if t.get("prompt"):
        print("Сообщение для ИИ (взяв в работу, очистить: \"prompt\": \"-\"):")
        for line in t["prompt"].splitlines():
            print("  " + line)
    if t["notes"]:
        print("Описание:")
        for line in t["notes"].splitlines():
            print("  " + line)
    if t.get("checklist"):  # сервер схемы 4–5 поля «Шаги» не отдаёт
        print("Шаги:")
        for i, c in enumerate(t["checklist"], 1):
            print(f"  {i}. [{'x' if c['on'] else ' '}] {c['t']}")
    if r.get("runs"):
        print("Сессии ИИ (новое сверху):")
        for x in r["runs"][:5]:
            bits = [f"#{x['id']} {RUN_RU.get(x['status'], x['status'])}", stamp(x["created_at"]),
                    v.actor(x["executor"]), x["model"], x["effort"] and "усилие " + x["effort"],
                    x["spent"] is not None and f"{x['spent']:g}% недели", x["reason"]]
            print("  " + " · ".join(str(b) for b in bits if b))
            for label, text in (("поручение", x["message"]), ("вопрос", x["question"]),
                                ("ответ", x["result"] if x["status"] != "asked" else "")):
                if text:
                    one = " / ".join(text.splitlines())
                    print(f"    {label}: {one if len(one) <= 300 else one[:299] + '…'}")
    if r.get("files"):
        print("Вложения (скачать — pult.py fetch НОМЕР):")
        for f in r["files"]:
            print(f"  #{f['id']} {f['name']} ({human_size(f['size'])}) — {v.actor(f['created_by'])}, "
                  f"{stamp(f['created_at'])}")
    if r["journal"]:
        print("Журнал (новое сверху):")
        for e in r["journal"]:
            bits = [x for x in [e["text"], " · ".join(e["changes"])] if x]
            print(f"  {stamp(e['at'])} {v.actor(e['actor'])}{via_text(e)}: " + " — ".join(bits).replace("\n", " / "))


def parse_since(value):
    s = str(value).strip().lower()
    m = re.match(r"^(\d{1,3})\s*([dhдч]?)$", s)
    if m:
        n = int(m.group(1))
        delta = dt.timedelta(hours=n) if m.group(2) in ("h", "ч") else dt.timedelta(days=n)
        return (dt.datetime.now().astimezone() - delta).replace(microsecond=0).isoformat()
    return parse_date(value)


def cmd_log(cfg, args):
    st = get_state(cfg)
    v = View(st)
    params = {"limit": args.limit}
    if args.since:
        params["since"] = parse_since(args.since)
    if args.by:
        params["actor"] = st["me"]["id"] if args.by in ("me", "я") else args.by
    if args.project:
        params["project"] = args.project
    if args.task:
        params["task"] = norm_key(args.task)
    entries = request(cfg, "GET", "/api/journal?" + urllib.parse.urlencode(params))["journal"]
    for e in reversed(entries):
        what = f"{e['task']} {e['task_title'] or ''}".strip() if e["task"] else (
            v.projects[e["project"]]["name"] if e["project"] in v.projects else "")
        bits = [x for x in [e["text"], " · ".join(e["changes"])] if x]
        print(f"{stamp(e['at'])} {v.actor(e['actor'])}{via_text(e) if e.get('session') else ''}: {what}"
              + (" (удалена)" if e["gone"] else "") + " — " + " — ".join(bits).replace("\n", " / "))
    print(f"— записей {len(entries)}")


def cmd_projects(cfg, args):
    st = get_state(cfg)
    v = View(st)
    for a in st["areas"]:
        print(f"{a['name']} [{a['id']}, номера {a['prefix']}-…]" + (" — закрытый раздел" if a["closed"] else ""))
        for p in st["projects"]:
            if p["area"] != a["id"] or (p["archived"] and not args.all):
                continue
            n = sum(1 for t in st["tasks"] if t["project"] == p["id"] and v.is_open(t))
            flags = [x for x in ["закрытый" if p["closed"] else "", f"общий {p['shared']}" if p["shared"] else "",
                                 f"пауза с {ddmm(p['paused'])}" if p["paused"] else "",
                                 "в архиве" if p["archived"] else "",
                                 "только чтение" if st.get("access", {}).get(p["id"]) == "view" else ""] if x]
            print(f"  {p['id']:<10} {p['name']} — открытых {n}" + (f" ({', '.join(flags)})" if flags else "")
                  + (f" · память {p['mem']}" if p["mem"] else ""))


def cmd_url(cfg, args):
    print(cfg["url"] + "/" + (f"#t={q(norm_key(args.key))}" if args.key else ""))


def cmd_whoami(cfg, args):
    r = request(cfg, "GET", "/api/me")
    role = {"owner": "владелец Пульта", "member": "участник"}.get(r["me"].get("role"), "")
    print(f"{cfg['url']}: вы — {r['me']['name']} ({r['me']['id']})" + (f", {role}" if role else "")
          + f", ключ «{r['via']}»" + (f", сессия {cfg['session']}" if cfg.get("session") else ""))


def cmd_digest(cfg, args):
    print(request(cfg, "GET", "/api/digest")["text"])


# ---------- вложения ----------

def attach_file(cfg, key, path, name=None):
    with open(path, "rb") as f:
        data = f.read()
    name = name or os.path.basename(path)
    if "_limit" not in cfg:
        cfg["_limit"] = request(cfg, "GET", "/api/state").get("limits", {}).get("file")
    if cfg["_limit"] and len(data) > cfg["_limit"]:
        raise ValueError(f"{name}: {human_size(len(data))} — больше предела {human_size(cfg['_limit'])}")
    body, _ = request_raw(cfg, "POST", f"/api/task/{q(key)}/files", data,
                          {"X-File-Name": q(name), "Content-Type": "application/octet-stream"}, timeout=300)
    f = json.loads(body.decode("utf-8"))["file"]
    return f"{key}: вложение #{f['id']} {f['name']} ({human_size(f['size'])})"


def cmd_attach(cfg, args):
    key = norm_key(args.key)
    for path in args.files:
        print(attach_file(cfg, key, path))
    if args.message:
        request(cfg, "POST", f"/api/task/{q(key)}/comment", {"text": args.message})
        print(f"{key}: запись в журнал")


def cmd_fetch(cfg, args):
    fid = args.id.lstrip("#")
    data, headers = request_raw(cfg, "GET", f"/api/file/{q(fid)}?dl=1", timeout=300)
    m = re.search(r"filename\*=UTF-8''([^;]+)", headers.get("Content-Disposition") or "")
    name = urllib.parse.unquote(m.group(1)) if m else f"file-{fid}"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or f"file-{fid}"
    out = args.out or os.path.join(tempfile.gettempdir(), "pult-files", f"{fid}-{safe}")
    if os.path.isdir(out):
        out = os.path.join(out, safe)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    print(f"#{fid} {name} ({human_size(len(data))}): {out}")


def cmd_detach(cfg, args):
    fid = args.id.lstrip("#")
    r = request(cfg, "DELETE", f"/api/file/{q(fid)}")
    print(f"{r['task']['key']}: вложение #{fid} удалено")


# ---------- команды записи ----------

def me_id(cfg):
    if "_me" not in cfg:
        cfg["_me"] = request(cfg, "GET", "/api/me")["me"]["id"]
    return cfg["_me"]


def summary(key, sent, resp):
    if resp.get("unchanged"):
        return f"{key}: без изменений"
    fields = [FIELD_RU.get(k, k) for k in sent if k != "version"]
    return f"{key}: " + (", ".join(fields) if fields else "записано")


class Conflict(Exception):
    """Сервер не принял правку по устаревшей версии карточки; текст для вывода уже собран."""


def conflict_text(cfg, key, err, ver):
    lines = [f"{key}: КОНФЛИКТ — {str(err).replace(' — карточка обновлена', '')}"]
    entries = [e for e in err.payload.get("conflicts", []) if e.get("changes") or e.get("text")]
    if entries:
        try:
            names = {a["id"]: a["name"] for a in request(cfg, "GET", "/api/state")["actors"]}
        except PultError:
            names = {}
        lines.append(f"   после версии {ver}, какой её видела эта сессия:")
        for e in entries:
            bits = [x for x in [e["text"], " · ".join(e["changes"])] if x]
            lines.append(f"     {stamp(e['at'])} {names.get(e['actor'], e['actor'])}{via_text(e)}: "
                         + " — ".join(bits).replace("\n", " / "))
    lines.append(f"   Не записано. Посмотрите pult.py show {key} и повторите, если правка ещё нужна.")
    return "\n".join(lines)


def patch(cfg, key, body):
    try:
        return request(cfg, "PATCH", f"/api/task/{q(key)}", body)
    except PultError as e:
        if e.status == 409 and body.get("version") is not None:
            raise Conflict(conflict_text(cfg, key, e, body["version"])) from None
        raise


def with_version(cfg, key, op, body):
    """Версия из пакета, иначе та, какой эта сессия видела карточку; без неё правка идёт как раньше."""
    ver = op["version"] if "version" in op else seen(cfg).version(key)
    if ver is not None:
        body["version"] = ver
    return body


def op_step(cfg, key, action, arg):
    # Шаг по номеру сверяем с версией, какой сессия видела карточку: номера могли сдвинуться.
    # Шаг по тексту ищется в свежем списке — его сливаем с чужими правками сами.
    by_number = action != "add" and bool(re.match(r"^\d+$", str(arg)))
    ver = seen(cfg).version(key) if by_number else None
    for attempt in (1, 2):
        t = request(cfg, "GET", f"/api/task/{q(key)}?journal=1")["task"]
        items = [dict(c) for c in t.get("checklist", [])]
        if action == "add":
            items.append({"t": str(arg), "on": False})
        else:
            idx = resolve_step(items, arg)
            if action == "check":
                items[idx]["on"] = True
            elif action == "uncheck":
                items[idx]["on"] = False
            elif action == "remove":
                items.pop(idx)
            else:
                raise ValueError(f"шаг: add, check, uncheck или remove, а не {action!r}")
        body = {"checklist": items, "version": t["version"] if ver is None else ver}
        try:
            return body, request(cfg, "PATCH", f"/api/task/{q(key)}", body)
        except PultError as e:
            if e.status != 409:
                raise
            if ver is not None:
                raise Conflict(conflict_text(cfg, key, e, ver)) from None
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def resolve_step(items, arg):
    """Шаг по номеру (с 1) или по куску текста — ровно одно совпадение."""
    if isinstance(arg, int) or re.match(r"^\d+$", str(arg)):
        i = int(arg) - 1
        if not 0 <= i < len(items):
            raise ValueError(f"нет шага {arg}, всего {len(items)}")
        return i
    hits = [i for i, c in enumerate(items) if str(arg).lower() in c["t"].lower()]
    if len(hits) != 1:
        raise ValueError(f"шаг «{arg}»: совпадений {len(hits)}, нужно ровно одно")
    return hits[0]


def run_op(cfg, op):
    kind = op.get("op")
    if kind == "add":
        raw = dict(op)
        raw.pop("op", None)
        body = clean_fields(raw, me_id(cfg))
        if not body.get("project") or not body.get("title"):
            raise ValueError("add: нужны project и title")
        r = request(cfg, "POST", "/api/task", body)
        seen(cfg).saw(r["task"])
        return f"{r['task']['key']}: создана — {r['task']['title']}"
    key = norm_key(op.get("key"))
    if not key:
        raise ValueError(f"{kind}: нужен key")
    if kind == "set":
        body = with_version(cfg, key, op, clean_fields(op, me_id(cfg)))
        if op.get("result"):
            body["next"] = str(op["result"])
        return summary(key, body, patch(cfg, key, body))
    if kind == "done":
        body = clean_fields({k: v for k, v in op.items() if k not in ("status",)}, me_id(cfg))
        body["status"] = "dropped" if op.get("dropped") else "done"
        body.pop("dropped", None)
        if op.get("result"):
            body["next"] = str(op["result"])
        return summary(key, body, patch(cfg, key, with_version(cfg, key, op, body)))
    if kind == "note":
        text = str(op.get("text") or op.get("comment") or "").strip()
        if not text:
            raise ValueError("note: пустой текст")
        request(cfg, "POST", f"/api/task/{q(key)}/comment", {"text": text})
        return f"{key}: запись в журнал"
    if kind == "step":
        for action in ("add", "check", "uncheck", "remove"):
            if action in op:
                body, resp = op_step(cfg, key, action, op[action])
                return f"{key}: шаг {action} — " + ("без изменений" if resp.get("unchanged") else "записано")
        raise ValueError("step: нужен один из add, check, uncheck, remove")
    if kind == "delete":
        ver = with_version(cfg, key, op, {}).get("version")
        try:
            request(cfg, "DELETE", f"/api/task/{q(key)}" + (f"?version={ver}" if ver is not None else ""))
        except PultError as e:
            if e.status == 409 and ver is not None:
                raise Conflict(conflict_text(cfg, key, e, ver)) from None
            raise
        return f"{key}: удалена"
    if kind == "attach":
        if not op.get("file"):
            raise ValueError("attach: нужен file — путь к файлу")
        out = attach_file(cfg, key, op["file"], op.get("name"))
        if op.get("comment"):
            request(cfg, "POST", f"/api/task/{q(key)}/comment", {"text": str(op["comment"])})
        return out
    raise ValueError(f"неизвестная операция {kind!r}: add, set, done, note, step, attach, delete, project, area, "
                     f"settings")


def cmd_apply(cfg, args):
    with open(args.file, encoding="utf-8-sig") as f:
        ops = json.load(f)
    if isinstance(ops, dict):
        ops = ops.get("ops", [])
    failed = 0
    for i, op in enumerate(ops, 1):
        try:
            if op.get("op") in ("project", "area"):
                kind = op["op"]
                body = {k: v for k, v in op.items() if k != "op"}
                for k in ("model", "effort"):
                    if k in body and (body[k] is None or str(body[k]).strip() == "-"):
                        body[k] = ""
                pid = body.pop("id")
                st = get_state(cfg)
                exists = any(p["id"] == pid for p in st[kind + "s"])
                if exists:
                    request(cfg, "PATCH", f"/api/{kind}/{q(pid)}", body)
                else:
                    request(cfg, "POST", f"/api/{kind}", dict(body, id=pid))
                print(f"{i}. {'проект' if kind == 'project' else 'раздел'} {pid}: {'изменён' if exists else 'создан'}")
            elif op.get("op") == "settings":
                body = {k: ("" if v is None or str(v).strip() == "-" else v) for k, v in op.items() if k != "op"}
                ai = request(cfg, "PATCH", "/api/settings", body)["ai"]
                print(f"{i}. Пульт: модель ИИ {ai['model'] or 'не задана'}, усилие {ai['effort'] or 'не задано'}")
            else:
                print(f"{i}. {run_op(cfg, op)}")
        except Conflict as e:
            failed += 1
            print(f"{i}. {e}")
        except (PultError, ValueError, KeyError, TypeError) as e:
            failed += 1
            print(f"{i}. ОШИБКА ({op.get('op')} {op.get('key') or op.get('title') or ''}): {e}")
    print(f"— выполнено {len(ops) - failed} из {len(ops)}")
    return 1 if failed else 0


CLI_FIELDS = ("project", "title", "status", "holder", "next", "wait", "check", "due", "notes", "mem", "prompt",
              "model", "effort")


def cmd_add(cfg, args):
    raw = {k: getattr(args, k) for k in CLI_FIELDS if getattr(args, k) is not None}
    raw["op"] = "add"
    if args.message:
        raw["comment"] = args.message
    print(run_op(cfg, raw))


def cmd_set(cfg, args):
    raw = {k: getattr(args, k) for k in CLI_FIELDS if getattr(args, k) is not None}
    raw.update(op="set", key=args.key)
    if args.message:
        raw["comment"] = args.message
    print(run_op(cfg, raw))


def cmd_done(cfg, args):
    raw = {"op": "done", "key": args.key, "dropped": args.dropped}
    if args.result:
        raw["result"] = args.result
    if args.message:
        raw["comment"] = args.message
    print(run_op(cfg, raw))


def cmd_note(cfg, args):
    print(run_op(cfg, {"op": "note", "key": args.key, "text": " ".join(args.text)}))


def cmd_step(cfg, args):
    arg = " ".join(args.arg)
    print(run_op(cfg, {"op": "step", "key": args.key, args.action: arg}))


def cmd_import(cfg, args):
    with open(args.file, encoding="utf-8-sig") as f:
        bundle = json.load(f)
    for i, a in enumerate(bundle.get("areas") or []):
        a.setdefault("sort", (i + 1) * 10)
    for i, p in enumerate(bundle.get("projects") or []):
        p.setdefault("sort", (i + 1) * 10)
    tasks = []
    for t in bundle.get("tasks") or []:
        t = dict(t)
        for k in ("stale", "stale_reason", "group"):
            t.pop(k, None)
        for k in DATE_FIELDS:
            if t.get(k) is not None:
                t[k] = parse_date(t[k])
        if t.get("checklist"):
            t["checklist"] = [{"t": c, "on": False} if isinstance(c, str) else {"t": c["t"], "on": bool(c.get("on"))}
                              for c in t["checklist"]]
        for k in ("next", "wait", "notes", "mem", "holder"):
            if t.get(k) is None:
                t.pop(k, None)
        tasks.append(t)
    bundle["tasks"] = tasks
    r = request(cfg, "POST", "/api/import", bundle)
    print(f"перенесено задач: {len(r['tasks'])}" + (f" ({r['tasks'][0]} … {r['tasks'][-1]})" if r["tasks"] else ""))


# ---------- участники и доступ ----------

def scope_text(st, g):
    if g["kind"] == "all":
        return "все проекты, кроме закрытых"
    pool, what = (st["areas"], "раздел") if g["kind"] == "area" else (st["projects"], "проект")
    hit = next((x for x in pool if x["id"] == g["target"]), None)
    return f"{what} «{hit['name'] if hit else g['target']}»"


def last_seen(u):
    live = [k for k in u["keys"] if not k["revoked_at"]]
    marks = [k["last_used_at"] for k in live if k["last_used_at"]] + [s["last_seen_at"] for k in live for s in k["sessions"]]
    return max(marks) if marks else None


def cmd_people(cfg, args):
    r = request(cfg, "GET", "/api/people")
    st = get_state(cfg)
    names = {a["id"]: a["name"] for a in st["actors"]}
    names.update({u["id"]: u["name"] for u in r["people"]})
    for u in r["people"]:
        kind = "человек" if u["kind"] == "person" else "модель" + (
            f" (хозяин: {names.get(u['owner'], u['owner'])})" if u["owner"] else "")
        role = "владелец Пульта" if u["role"] == "owner" else "участник"
        print(f"{u['name']} ({u['id']}) — {kind}, {role}" + ("" if u["active"] else " — ДОСТУП ОТКЛЮЧЁН"))
        if u["role"] == "owner":
            print("  доступ: всё, включая закрытые разделы и проекты")
        else:
            grants = [f"{scope_text(st, g)} — {LEVEL_RU.get(g['level'], g['level'])}" for g in u["grants"]]
            print("  доступ: " + ("; ".join(grants) if grants else "нет") + f" · видит проектов: {len(u['access'])}")
        live = [k for k in u["keys"] if not k["revoked_at"]]
        if live:
            print("  ключи и входы: " + "; ".join(
                f"{k['label']} [{k['kid']}]" + (" — этот" if k.get("current") else "")
                + (f", последний раз {stamp(max([k['last_used_at'] or ''] + [s['last_seen_at'] for s in k['sessions']]))}"
                   if k["last_used_at"] or k["sessions"] else "") for k in live))
        invites = [i for i in u["invites"] if i["state"] == "active"]
        if invites:
            print("  ссылки для входа: " + "; ".join(f"i{i['id']} до {stamp(i['expires_at'])}" for i in invites))
        if not live and not invites:
            print("  входов нет" if u["kind"] == "person" else "  ключа нет")


def cmd_person(cfg, args):
    body = {k: getattr(args, k) for k in ("name", "kind", "owner", "role", "note", "color") if getattr(args, k) is not None}
    if body.get("owner") in ("-", ""):
        body["owner"] = None
    if args.off or args.on:
        body["active"] = bool(args.on)
    known = {u["id"] for u in request(cfg, "GET", "/api/people")["people"]}
    if args.id in known:
        if not body:
            raise ValueError("нечего менять: --name, --note, --role, --owner, --off, --on")
        request(cfg, "PATCH", f"/api/actor/{q(args.id)}", body)
        print(f"{args.id}: изменено — {', '.join(body)}")
    else:
        if not args.name:
            raise ValueError("для нового участника нужен --name")
        r = request(cfg, "POST", "/api/actor", dict(body, id=args.id))
        print(f"{r['id']}: добавлен")


def cmd_grant(cfg, args):
    scope = args.scope.strip()
    if scope in ("all", "все"):
        body = {"kind": "all"}
    elif scope.startswith("area:"):
        body = {"kind": "area", "target": scope[len("area:"):]}
    else:
        body = {"kind": "project", "target": scope[len("project:"):] if scope.startswith("project:") else scope}
    level = args.level.strip().lower()
    if level not in LEVEL_ALIASES:
        raise ValueError("доступ: view (читает), edit (правит) или - (снять)")
    body["level"] = LEVEL_ALIASES[level]
    r = request(cfg, "POST", f"/api/actor/{q(args.id)}/grant", body)
    print(f"{args.id}: " + ("без изменений" if r.get("unchanged") else
                            "доступ снят" if body["level"] is None else f"доступ — {LEVEL_RU[body['level']]}"))


def save_secret(name, text, shortcut=False):
    """Секрет — в файл рядом с ключами, не в вывод. Возвращает путь."""
    folder = os.environ.get("PULT_SECRETS_DIR") or LOCAL
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path, n = os.path.join(folder, f"{stem}-{n}{ext}"), n + 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    if shortcut:
        with open(os.path.splitext(path)[0] + ".url", "w", encoding="utf-8") as f:
            f.write("[InternetShortcut]\nURL=" + text + "\n")
    return path


def cmd_invite(cfg, args):
    body = {"days": args.days}
    if args.note:
        body["note"] = args.note
    r = request(cfg, "POST", f"/api/actor/{q(args.id)}/invite", body)
    path = save_secret(f"invite-{args.id}-{dt.datetime.now():%Y%m%d-%H%M}.txt", cfg["url"] + "/login#i=" + r["token"],
                       shortcut=True)
    print(f"ссылка для входа {args.id} (i{r['invite']['id']}) действует до {stamp(r['invite']['expires_at'])}, "
          f"войти по ней можно один раз: {path}")


def cmd_key(cfg, args):
    r = request(cfg, "POST", f"/api/actor/{q(args.id)}/key", {"label": args.label} if args.label else {})
    path = save_secret(r["key"]["label"] + ".token", r["token"])
    print(f"ключ «{r['key']['label']}» [{r['key']['kid']}] для {args.id}: {path}")


def cmd_revoke(cfg, args):
    what = args.what.strip().lower()
    if re.match(r"^i\d+$", what):
        r = request(cfg, "POST", f"/api/invite/{what[1:]}/revoke", {})
        print("ссылка для входа " + ("уже не действовала" if r.get("unchanged") else "отозвана"))
    else:
        r = request(cfg, "POST", f"/api/key/{q(what)}/revoke", {})
        print("ключ " + ("уже был отозван" if r.get("unchanged") else "отозван, его входы закрыты"))


def cmd_keygen(args):
    """Новый ключ участника. Секрет — в .local/<label>.token, хеш — в .local/keys_public.json для сервера."""
    os.makedirs(LOCAL, exist_ok=True)
    if not re.match(r"^[a-z0-9][a-z0-9_-]{1,40}$", args.label):
        raise SystemExit("label: латиница, цифры, - и _")
    token_path = os.path.join(LOCAL, args.label + ".token")
    if os.path.exists(token_path):
        raise SystemExit(f"уже есть {token_path}; чтобы выпустить новый, удалите его и отзовите старый на сервере")
    token = secrets.token_urlsafe(32)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    pub_path = os.path.join(LOCAL, "keys_public.json")
    pub = {"actors": [], "keys": []}
    if os.path.exists(pub_path):
        with open(pub_path, encoding="utf-8") as f:
            pub = json.load(f)
    if args.seed and not pub.get("actors"):
        with open(args.seed, encoding="utf-8") as f:
            pub["actors"] = json.load(f).get("actors", [])
    pub["keys"].append({"actor": args.actor, "hash": hashlib.sha256(token.encode()).hexdigest(), "label": args.label})
    with open(pub_path, "w", encoding="utf-8") as f:
        json.dump(pub, f, ensure_ascii=False, indent=2)
    print(f"ключ {args.label} для {args.actor}: секрет в {token_path}, хеш добавлен в {pub_path}")
    if args.login_url:
        url = (args.url or load_url_only()).rstrip("/") + "/login#k=" + token
        with open(os.path.join(LOCAL, args.label + "_login.url"), "w", encoding="utf-8") as f:
            f.write("[InternetShortcut]\nURL=" + url + "\n")
        with open(os.path.join(LOCAL, args.label + "_login.txt"), "w", encoding="utf-8") as f:
            f.write(url + "\n")
        print(f"ссылка для входа: {os.path.join(LOCAL, args.label + '_login.url')} (и .txt рядом)")
    if args.use:
        cfg_path = os.path.join(LOCAL, "config.json")
        cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["token_file"] = args.label + ".token"
        if args.url:
            cfg["url"] = args.url
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"{cfg_path}: token_file = {args.label}.token")


def cmd_connect(args):
    """Адрес Пульта и ключ модели → config.json и файл ключа рядом с ним. Ключ проверяется запросом /api/me."""
    url = args.url.rstrip("/")
    if not re.match(r"^https?://[^/\s]+$", url):
        raise SystemExit("адрес Пульта: https://сервер:9447 или http://127.0.0.1:9447")
    if args.token_file:
        with open(args.token_file, encoding="utf-8-sig") as f:
            token = f.read().strip()
    elif sys.stdin.isatty():
        token = getpass.getpass("Ключ модели (ввод не виден): ").strip()
    else:
        token = sys.stdin.readline().strip()
    if not token:
        raise SystemExit("нет ключа")
    try:
        me = request({"url": url, "token": token}, "GET", "/api/me")["me"]
    except PultError as e:
        raise SystemExit(f"Пульт не принял ключ: {e}") from None
    cfg_path = os.environ.get("PULT_CONFIG") or os.path.join(LOCAL, "config.json")
    folder = os.path.dirname(os.path.abspath(cfg_path))
    os.makedirs(folder, exist_ok=True)
    if args.token_file and os.path.dirname(os.path.abspath(args.token_file)) == folder:
        token_name = os.path.basename(args.token_file)
    else:
        token_name = (args.label or me["id"]) + ".token"
        fd = os.open(os.path.join(folder, token_name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg.update(url=url, token_file=token_name)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"подключено: {url}, вы — {me['name']} ({me['id']}); ключ — {os.path.join(folder, token_name)}, "
          f"настройки — {cfg_path}")


def load_url_only():
    path = os.path.join(LOCAL, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            url = json.load(f).get("url")
        if url:
            return url
    raise SystemExit("нужен --url или url в .local/config.json")


def build_parser():
    p = argparse.ArgumentParser(prog="pult.py", description="Пульт из командной строки — для ИИ-агентов и людей")
    sub = p.add_subparsers(dest="cmd", required=True)
    cn = sub.add_parser("connect", help="подключиться к Пульту: адрес и ключ модели")
    cn.add_argument("url")
    cn.add_argument("--token-file", help="файл с ключом; без него ключ спросят (ввод не виден)")
    cn.add_argument("--label", help="имя файла ключа без .token; по умолчанию id модели")
    b = sub.add_parser("brief", help="что требует внимания")
    b.add_argument("--project")
    b.add_argument("--area")
    ls = sub.add_parser("ls", help="список задач")
    ls.add_argument("--project")
    ls.add_argument("--area")
    ls.add_argument("--status")
    ls.add_argument("--holder")
    ls.add_argument("--closed", action="store_true")
    ls.add_argument("--all", action="store_true")
    f = sub.add_parser("find", help="поиск")
    f.add_argument("words", nargs="+")
    s = sub.add_parser("show", help="карточка задачи")
    s.add_argument("key")
    s.add_argument("--journal", type=int, default=30)
    lg = sub.add_parser("log", help="журнал изменений")
    lg.add_argument("--since")
    lg.add_argument("--by")
    lg.add_argument("--project")
    lg.add_argument("--task")
    lg.add_argument("--limit", type=int, default=100)
    pr = sub.add_parser("projects", help="разделы и проекты")
    pr.add_argument("--all", action="store_true")
    u = sub.add_parser("url", help="адрес страницы")
    u.add_argument("key", nargs="?")
    sub.add_parser("whoami", help="проверка адреса и ключа")
    sub.add_parser("digest", help="утренняя сводка, как она придёт в Telegram")
    at = sub.add_parser("attach", help="прикрепить файлы к задаче")
    at.add_argument("key")
    at.add_argument("files", nargs="+")
    at.add_argument("-m", "--message", help="запись в журнал")
    fe = sub.add_parser("fetch", help="скачать вложение")
    fe.add_argument("id")
    fe.add_argument("-o", "--out", help="файл или папка")
    de = sub.add_parser("detach", help="удалить вложение")
    de.add_argument("id")
    ap = sub.add_parser("apply", help="пакет изменений из JSON")
    ap.add_argument("file")
    for name in ("add", "set"):
        x = sub.add_parser(name)
        if name == "set":
            x.add_argument("key")
        x.add_argument("--project", required=(name == "add"))
        x.add_argument("--title", required=(name == "add"))
        x.add_argument("--status")
        x.add_argument("--holder")
        x.add_argument("--next")
        x.add_argument("--wait")
        x.add_argument("--check")
        x.add_argument("--due")
        x.add_argument("--notes")
        x.add_argument("--mem")
        x.add_argument("--prompt", help="сообщение для ИИ; «-» — очистить")
        x.add_argument("--model", help="модель ИИ для сессии по карточке; «-» — как у проекта")
        x.add_argument("--effort", help="усилие ИИ; «-» — как у проекта")
        x.add_argument("-m", "--message", help="запись в журнал")
    d = sub.add_parser("done")
    d.add_argument("key")
    d.add_argument("--result", help="итог (ляжет в поле «Итог»)")
    d.add_argument("--dropped", action="store_true", help="снято, а не сделано")
    d.add_argument("-m", "--message")
    n = sub.add_parser("note")
    n.add_argument("key")
    n.add_argument("text", nargs="+")
    st = sub.add_parser("step")
    st.add_argument("key")
    st.add_argument("action", choices=("add", "check", "uncheck", "remove"))
    st.add_argument("arg", nargs="+")
    im = sub.add_parser("import")
    im.add_argument("file")
    sub.add_parser("people", help="участники, доступ, входы")
    pe = sub.add_parser("person", help="новый участник или правка")
    pe.add_argument("id")
    pe.add_argument("--name")
    pe.add_argument("--kind", choices=("person", "model"))
    pe.add_argument("--owner", help="хозяин модели (id человека) или - убрать")
    pe.add_argument("--role", choices=("owner", "member"))
    pe.add_argument("--note")
    pe.add_argument("--color")
    onoff = pe.add_mutually_exclusive_group()
    onoff.add_argument("--off", action="store_true", help="отключить доступ")
    onoff.add_argument("--on", action="store_true", help="включить доступ")
    gr = sub.add_parser("grant", help="выдать или снять доступ")
    gr.add_argument("id")
    gr.add_argument("scope", help="all | area:ID | ID проекта")
    gr.add_argument("level", help="view | edit | -")
    iv = sub.add_parser("invite", help="одноразовая ссылка для входа")
    iv.add_argument("id")
    iv.add_argument("--days", type=int, default=7)
    iv.add_argument("--note")
    ky = sub.add_parser("key", help="ключ модели")
    ky.add_argument("id")
    ky.add_argument("--label")
    rk = sub.add_parser("revoke", help="отозвать ключ или ссылку")
    rk.add_argument("what", help="начало хеша ключа или iНОМЕР ссылки")
    kg = sub.add_parser("keygen")
    kg.add_argument("actor")
    kg.add_argument("--label", required=True)
    kg.add_argument("--login-url", action="store_true")
    kg.add_argument("--use", action="store_true", help="записать ключ в .local/config.json")
    kg.add_argument("--url")
    kg.add_argument("--seed", help="seed.json — взять из него участников для сервера")
    return p


COMMANDS = {"brief": cmd_brief, "ls": cmd_ls, "find": cmd_find, "show": cmd_show, "log": cmd_log,
            "projects": cmd_projects, "url": cmd_url, "whoami": cmd_whoami, "apply": cmd_apply, "add": cmd_add,
            "set": cmd_set, "done": cmd_done, "note": cmd_note, "step": cmd_step, "import": cmd_import,
            "people": cmd_people, "person": cmd_person, "grant": cmd_grant, "invite": cmd_invite, "key": cmd_key,
            "revoke": cmd_revoke, "digest": cmd_digest, "attach": cmd_attach, "fetch": cmd_fetch,
            "detach": cmd_detach}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    if args.cmd in ("keygen", "connect"):
        (cmd_keygen if args.cmd == "keygen" else cmd_connect)(args)
        return 0
    try:
        cfg = load_config()
        rc = COMMANDS[args.cmd](cfg, args)
        return rc or 0
    except Conflict as e:
        print(e, file=sys.stderr)
        return 1
    except PultError as e:
        print(f"Пульт: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
