#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт — доска дел для людей и их ИИ-агентов: HTTPS, API, страница и база SQLite в одной программе.

На сервере живёт как /opt/pult/pult_server.py под системным пользователем pult (служба
pult.service, порт 9447, ставит deploy/install.sh). Сертификат и ключ отдаёт systemd через
LoadCredential, так что от root служба не стартует вовсе. Только стандартная библиотека, Python 3.9+.

    pult_server.py init --db … --name ИМЯ [--ai Claude] [--url …]   новый Пульт: владелец, его ИИ, первая карточка
    pult_server.py serve [--db …] [--static …] [--port 9447]
    pult_server.py serve --plain-http --db …                         на своём компьютере: без TLS, только 127.0.0.1
    pult_server.py serve --proxy --db …                              за обратным прокси с HTTPS (Caddy, nginx)
    pult_server.py admin --db … load-keys keys_public.json           завести людей и ключи
    pult_server.py admin --db … keys | sessions | revoke <начало хеша> | stats
    pult_server.py admin --db … role <участник> owner|member          если владельцев не осталось
    pult_server.py admin --db … digest-to <участник> <чат> | digest-off <участник> | digest-list
    pult_server.py digest --db … --url https://… [--actor …] [--send]   утренняя сводка в Telegram

Ключ входа хранится только хешем. Человек входит ссылкой /login#k=<ключ> или одноразовой
ссылкой-приглашением /login#i=<код>: часть после # браузер серверу не отправляет, её читает
login.js и меняет на сессию (куки pult_s). Модель ходит с ключом в заголовке Authorization: Bearer.

Права: владелец видит и правит всё и управляет участниками; участник — только проекты, к которым
выдан доступ («читает» или «правит»). Доступ ко всем проектам или к разделу не открывает закрытые
разделы и проекты — к ним доступ выдаётся отдельно. Модель получает не больше, чем у её хозяина.

Вложения лежат файлами рядом с базой (files/ab/<sha256>): одинаковые хранятся один раз, а удалённое
стирается с диска через FILES_KEEP_DAYS — дольше, чем живут копии базы, так что любая копия цела.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

STATUSES = ("inbox", "next", "doing", "wait", "done", "dropped")
CLOSED = ("done", "dropped")
STATUS_RU = {"inbox": "Входящие", "next": "Следующие", "doing": "В работе",
             "wait": "Ждёт", "done": "Готово", "dropped": "Снято"}
KINDS = ("person", "model")
ROLES = ("owner", "member")
ROLE_RU = {"owner": "владелец", "member": "участник"}
LEVELS = ("view", "edit")
LEVEL_RANK = {"view": 1, "edit": 2}
LEVEL_RU = {"view": "читает", "edit": "правит"}
GRANT_KINDS = ("all", "area", "project")
# Номер задачи можно набрать и русскими буквами: «1С-7» и «1C-7» — одна задача.
LOOKALIKE = str.maketrans("АВСЕНКМОРТХУ", "ABCEHKMOPTXY")
TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PREFIX_RE = re.compile(r"^[A-Z0-9]{1,6}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
COOKIE = "pult_s"
SESSION_TOUCH_SEC = 600
SESSION_TTL_DAYS = 180
INVITE_DAYS = 7
SCHEMA_VERSION = 3
FILE_MAX = 25 << 20          # одно вложение
FILES_QUOTA = 500 << 20      # все вложения вместе; меняется ключом --files-quota-mb
FILES_MIN_FREE = 300 << 20   # столько должно остаться на диске после загрузки
FILES_KEEP_DAYS = 15         # удалённое вложение стирается с диска позже, чем уходит последняя копия базы
FILES_LOCK = threading.Lock()  # загрузка и уборка файлов не пересекаются: уборка не сотрёт то, что только что пришло
# Показываем на странице только картинки, узнанные по первым байтам; остальное отдаём на скачивание.
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
FILE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
TELEGRAM_API = "https://api.telegram.org"
DIGEST_LIMIT = 3900          # Telegram принимает до 4096 знаков текста
WEEKDAY_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTH_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
            "ноября", "декабря")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
INSERT OR IGNORE INTO meta(k, v) VALUES ('schema', '3'), ('rev', '0');
CREATE TABLE IF NOT EXISTS actors(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, color TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  role TEXT NOT NULL DEFAULT 'member', owner_id TEXT REFERENCES actors(id));
CREATE TABLE IF NOT EXISTS keys(
  hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id), label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id), key_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS grants(
  actor_id TEXT NOT NULL REFERENCES actors(id), kind TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  PRIMARY KEY(actor_id, kind, target));
CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL REFERENCES actors(id),
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, created_by TEXT NOT NULL, expires_at TEXT NOT NULL,
  used_at TEXT, key_hash TEXT, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS areas(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL UNIQUE, closed INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0, next_num INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, area_id TEXT NOT NULL REFERENCES areas(id), name TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', mem TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
  closed INTEGER NOT NULL DEFAULT 0, shared TEXT NOT NULL DEFAULT '', paused TEXT,
  archived INTEGER NOT NULL DEFAULT 0, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL, status TEXT NOT NULL, holder TEXT REFERENCES actors(id),
  next_step TEXT NOT NULL DEFAULT '', wait_for TEXT NOT NULL DEFAULT '',
  check_on TEXT, due_on TEXT, done_on TEXT, notes TEXT NOT NULL DEFAULT '',
  mem TEXT NOT NULL DEFAULT '', checklist TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  task_key TEXT, task_title TEXT, project_id TEXT, actor_id TEXT NOT NULL, at TEXT NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
  via TEXT NOT NULL DEFAULT '', task_version INTEGER);
CREATE INDEX IF NOT EXISTS journal_task ON journal(task_id, id);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL, task_key TEXT NOT NULL,
  name TEXT NOT NULL, size INTEGER NOT NULL, sha TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, created_by TEXT NOT NULL, deleted_at TEXT, deleted_by TEXT);
CREATE INDEX IF NOT EXISTS files_task ON files(task_id);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha);
"""

# Поля задачи: имя в API → (столбец, вид значения, подпись в журнале)
TASK_FIELDS = {
    "project": ("project_id", "project", "Проект"),
    "title": ("title", "title", "Название"),
    "status": ("status", "status", "Статус"),
    "holder": ("holder", "actor", "Держит"),
    "next": ("next_step", "text", "Следующий шаг"),
    "wait": ("wait_for", "text", "Ждём"),
    "check": ("check_on", "date", "Проверить"),
    "due": ("due_on", "date", "Срок"),
    "done_on": ("done_on", "date", "Закрыта"),
    "notes": ("notes", "notes", "Описание"),
    "mem": ("mem", "text", "Подробности"),
    "checklist": ("checklist", "checklist", "Шаги"),
}
PROJECT_FIELDS = {
    "area": ("area_id", "area"), "name": ("name", "title"), "note": ("note", "text"),
    "mem": ("mem", "text"), "path": ("path", "text"), "closed": ("closed", "bool"),
    "shared": ("shared", "text"), "paused": ("paused", "date"), "archived": ("archived", "bool"),
    "sort": ("sort", "int"),
}
AREA_FIELDS = {"name": ("name", "title"), "prefix": ("prefix", "prefix"), "closed": ("closed", "bool"),
               "sort": ("sort", "int")}
ACTOR_FIELDS = {"name": ("name", "title"), "kind": ("kind", "kind"), "color": ("color", "text"),
                "note": ("note", "text"), "sort": ("sort", "int"), "active": ("active", "bool"),
                "role": ("role", "role"), "owner": ("owner_id", "owner")}
ACTOR_LABELS = {"name": "имя", "kind": "вид", "color": "цвет", "note": "заметка", "sort": "порядок",
                "active": "доступ", "role": "роль", "owner_id": "хозяин модели"}


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def now_iso():
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm_key(key):
    return str(key or "").strip().upper().translate(LOOKALIKE)


def short(text, n=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def ddmm(iso):
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso[:10])
    return d.strftime("%d.%m") if d.year == dt.date.today().year else d.strftime("%d.%m.%Y")


def slug(name):
    """id участника из имени: «Маша» → masha."""
    s = "".join(TRANSLIT.get(ch, ch) for ch in str(name).lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:24].strip("-")
    return s or "u"


def linked(fields):
    """Статус и дата закрытия меняются вместе: задеть одно — значит задеть оба."""
    fields = set(fields)
    return fields | {"status", "done_on"} if fields & {"status", "done_on"} else fields


def plural(n, one, few, many):
    a, b = n % 10, n % 100
    return one if a == 1 and b != 11 else few if 2 <= a <= 4 and not 12 <= b <= 14 else many


# ---------- вложения ----------

def human_size(n):
    if n < 1024:
        return f"{n} Б"
    if n < 1 << 20:
        return f"{round(n / 1024)} КБ"
    return f"{n / (1 << 20):.1f} МБ".replace(".", ",")


def clean_name(name):
    """Имя вложения только для показа и для Content-Disposition: на диске файл лежит под своим хешем."""
    name = CTRL_RE.sub("", str(name or "")).replace("\\", "/").split("/")[-1].strip()
    return name[:200] or "файл"


def sniff(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF-"):
        return "pdf"
    return ""


def blob_path(files_dir, sha):
    return os.path.join(files_dir, sha[:2], sha)


def disposition(kind, name):
    """inline или attachment с именем: ASCII-запас для старых программ и UTF-8 по RFC 5987."""
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name).strip() or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def gc_files(db, files_dir, keep_days=FILES_KEEP_DAYS):
    """Стереть с диска файлы, которые удалены из всех задач дольше keep_days назад, и брошенные загрузки."""
    cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=keep_days)).replace(microsecond=0).isoformat()
    removed = 0
    with FILES_LOCK:
        for r in db.q("SELECT sha FROM files GROUP BY sha HAVING SUM(deleted_at IS NULL) = 0 "
                      "AND MAX(deleted_at) < ?", (cutoff,)):
            with contextlib.suppress(FileNotFoundError):
                os.remove(blob_path(files_dir, r["sha"]))
                removed += 1
        tmp = os.path.join(files_dir, "tmp")
        for name in os.listdir(tmp) if os.path.isdir(tmp) else ():
            path = os.path.join(tmp, name)
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(path) > 86400:
                    os.remove(path)
    return removed


# ---------- проверка входных значений ----------

def v_text(value, field, maxlen=500, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"{field}: нужна строка")
    value = CTRL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if required and not value:
        raise ApiError(400, f"{field}: пусто")
    if len(value) > maxlen:
        raise ApiError(400, f"{field}: длиннее {maxlen} знаков")
    return value


def v_date(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{field}: дата строкой ГГГГ-ММ-ДД")
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ApiError(400, f"{field}: дата в виде ГГГГ-ММ-ДД") from None


def v_checklist(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ApiError(400, "шаги: нужен список до 100 пунктов")
    out = []
    for item in value:
        if isinstance(item, str):
            item = {"t": item, "on": False}
        if not isinstance(item, dict):
            raise ApiError(400, "шаги: каждый пункт — {t, on}")
        out.append({"t": v_text(item.get("t"), "шаг", 300, required=True), "on": bool(item.get("on"))})
    return out


def v_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f"{field}: нужно целое число")
    return value


def v_id(value, field):
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ApiError(400, f"{field}: латиница, цифры, - и _, до 32 знаков")
    return value


# ---------- журнал: человеческие описания изменений ----------

def checklist_changes(old, new):
    out, pool = [], {}
    for item in old:
        pool.setdefault(item["t"], []).append(item["on"])
    for item in new:
        if pool.get(item["t"]):
            was = pool[item["t"]].pop(0)
            if was != item["on"]:
                out.append(f"Шаг «{short(item['t'], 70)}» — {'выполнен' if item['on'] else 'снова открыт'}")
        else:
            out.append(f"Добавлен шаг «{short(item['t'], 70)}»" + (" (выполнен)" if item["on"] else ""))
    for text, rest in pool.items():
        out.extend(f"Убран шаг «{short(text, 70)}»" for _ in rest)
    if not out and [i["t"] for i in old] != [i["t"] for i in new]:
        out.append("Шаги переставлены")
    return out


class DB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        for pragma in ("foreign_keys=ON", "journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=5000"):
            self.conn.execute("PRAGMA " + pragma)
        self.conn.executescript(SCHEMA)
        self.migrate()

    def migrate(self):
        ver = int(self.q1("SELECT v FROM meta WHERE k = 'schema'")[0])
        if ver >= SCHEMA_VERSION:
            return
        with self.tx() as c:
            if ver < 2:
                have = {r["name"] for r in c.execute("PRAGMA table_info(actors)")}
                if "role" not in have:
                    c.execute("ALTER TABLE actors ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
                if "owner_id" not in have:
                    c.execute("ALTER TABLE actors ADD COLUMN owner_id TEXT REFERENCES actors(id)")
                if "task_version" not in {r["name"] for r in c.execute("PRAGMA table_info(journal)")}:
                    c.execute("ALTER TABLE journal ADD COLUMN task_version INTEGER")
                # На этапе 1 прав не было: кто мог войти, тот видел и правил всё — тот и остаётся владельцем.
                c.execute("UPDATE actors SET role = 'owner' WHERE id IN "
                          "(SELECT actor_id FROM keys WHERE revoked_at IS NULL)")
            # Этап 3 добавил только таблицу вложений: её заводит SCHEMA, переносить нечего.
            c.execute("UPDATE meta SET v = ? WHERE k = 'schema'", (str(SCHEMA_VERSION),))

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    @staticmethod
    def bump(c):
        c.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'rev'")

    def rev(self):
        return int(self.q1("SELECT v FROM meta WHERE k = 'rev'")[0])

    # ----- люди и модели, ключи, сессии -----

    # Модель действует, только пока её хозяин не отключён.
    ACTIVE_SQL = ("a.active = 1 AND (a.owner_id IS NULL OR EXISTS "
                  "(SELECT 1 FROM actors o WHERE o.id = a.owner_id AND o.active = 1))")

    @staticmethod
    def actor_dict(r):
        return {"id": r["id"], "name": r["name"], "kind": r["kind"], "color": r["color"],
                "note": r["note"], "sort": r["sort"], "active": bool(r["active"]), "role": r["role"],
                "owner": r["owner_id"]}

    def actor(self, actor_id):
        r = self.q1("SELECT * FROM actors WHERE id = ?", (actor_id,))
        return self.actor_dict(r) if r else None

    def actor_by_key(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        h = sha(token)
        r = self.q1("SELECT a.*, k.label AS key_label, k.last_used_at AS key_used FROM keys k "
                    "JOIN actors a ON a.id = k.actor_id "
                    "WHERE k.hash = ? AND k.revoked_at IS NULL AND " + self.ACTIVE_SQL, (h,))
        if not r:
            return None
        if not r["key_used"] or r["key_used"] < self._ago(SESSION_TOUCH_SEC):
            with self.tx() as c:
                c.execute("UPDATE keys SET last_used_at = ? WHERE hash = ?", (now_iso(), h))
        a = self.actor_dict(r)
        a["via"] = r["key_label"] or "ключ"
        a["key_hash"] = h
        return a

    def _session(self, c, actor_id, key_hash, agent):
        sid = secrets.token_urlsafe(32)
        stamp = now_iso()
        c.execute("INSERT INTO sessions(hash, actor_id, key_hash, created_at, last_seen_at, agent) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (sha(sid), actor_id, key_hash, stamp, stamp,
                                                v_text(agent, "agent", 10_000)[:200]))
        return sid

    def create_session(self, actor, agent):
        with self.tx() as c:
            return self._session(c, actor["id"], actor["key_hash"], agent)

    def actor_by_session(self, sid):
        if not sid or len(sid) > 200:
            return None
        h = sha(sid)
        r = self.q1("SELECT a.*, s.last_seen_at AS seen, s.key_hash AS skey FROM sessions s "
                    "JOIN actors a ON a.id = s.actor_id "
                    "JOIN keys k ON k.hash = s.key_hash AND k.revoked_at IS NULL "
                    "WHERE s.hash = ? AND " + self.ACTIVE_SQL, (h,))
        if not r:
            return None
        if r["seen"] < self._ago(SESSION_TOUCH_SEC):
            with self.tx() as c:
                c.execute("UPDATE sessions SET last_seen_at = ? WHERE hash = ?", (now_iso(), h))
        a = self.actor_dict(r)
        a["via"] = "веб"
        a["key_hash"] = r["skey"]
        return a

    def drop_session(self, sid):
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE hash = ?", (sha(sid),))

    def expire_sessions(self):
        cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=SESSION_TTL_DAYS)).isoformat()
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE last_seen_at < ?", (cutoff,))

    @staticmethod
    def _ago(seconds):
        return (dt.datetime.now().astimezone() - dt.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()

    # ----- права -----

    @staticmethod
    def _levels(c, a):
        projects = c.execute("SELECT p.id, p.area_id, p.closed, ar.closed AS area_closed FROM projects p "
                             "JOIN areas ar ON ar.id = p.area_id").fetchall()
        if a["role"] == "owner":
            return {p["id"]: 2 for p in projects}
        grants = c.execute("SELECT kind, target, level FROM grants WHERE actor_id = ?", (a["id"],)).fetchall()
        out = {}
        for p in projects:
            best = 0
            for g in grants:
                if (g["kind"] == "project" and g["target"] == p["id"]
                        or g["kind"] == "area" and g["target"] == p["area_id"] and not p["closed"]
                        or g["kind"] == "all" and not p["closed"] and not p["area_closed"]):
                    best = max(best, LEVEL_RANK.get(g["level"], 0))
            if best:
                out[p["id"]] = best
        return out

    def access(self, c, actor_id):
        """{проект: 1 — читает, 2 — правит}. Модель получает не больше, чем у её хозяина."""
        a = c.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
        if not a or not a["active"]:
            return {}
        mine = self._levels(c, a)
        if a["owner_id"]:
            o = c.execute("SELECT * FROM actors WHERE id = ?", (a["owner_id"],)).fetchone()
            if not o or not o["active"]:
                return {}
            theirs = self._levels(c, o)
            mine = {p: min(n, theirs[p]) for p, n in mine.items() if p in theirs}
        return mine

    @staticmethod
    def is_owner(c, actor_id):
        a = c.execute("SELECT role, owner_id, active FROM actors WHERE id = ?", (actor_id,)).fetchone()
        if not a or not a["active"] or a["role"] != "owner":
            return False
        if a["owner_id"]:
            o = c.execute("SELECT role, active FROM actors WHERE id = ?", (a["owner_id"],)).fetchone()
            return bool(o and o["active"] and o["role"] == "owner")
        return True

    def need_owner(self, c, actor, what="это"):
        if not self.is_owner(c, actor["id"]):
            raise ApiError(403, f"{what} может только владелец Пульта")

    def _need_edit(self, c, acc, project_id, key):
        level = acc.get(project_id, 0)
        if not level:
            raise ApiError(404, f"нет задачи {key}")
        if level < 2:
            raise ApiError(403, f"в проекте «{self._project_name(c, project_id)}» у вас доступ только на чтение")

    def _check_holder(self, c, actor, holder, project_id):
        """Держать задачу может только тот, кто правит её проект. Владелец назначает кого угодно."""
        if holder and not self.is_owner(c, actor["id"]) and self.access(c, holder).get(project_id, 0) < 2:
            raise ApiError(400, f"«{holder}» не может держать задачу: нет права править проект "
                                f"«{self._project_name(c, project_id)}»")

    # ----- чтение -----

    @staticmethod
    def task_dict(r, full=False, files=0):
        d = {"key": r["key"], "project": r["project_id"], "title": r["title"], "status": r["status"],
             "holder": r["holder"], "next": r["next_step"], "wait": r["wait_for"], "check": r["check_on"],
             "due": r["due_on"], "done_on": r["done_on"], "mem": r["mem"],
             "checklist": json.loads(r["checklist"]), "created_at": r["created_at"],
             "created_by": r["created_by"], "updated_at": r["updated_at"], "updated_by": r["updated_by"],
             "version": r["version"], "has_notes": bool(r["notes"]), "files": files}
        if full:
            d["notes"] = r["notes"]
        return d

    def task_out(self, c, r):
        """Карточка для ответа на правку: с описанием и числом вложений."""
        n = c.execute("SELECT COUNT(*) FROM files WHERE task_id = ? AND deleted_at IS NULL", (r["id"],)).fetchone()[0]
        return self.task_dict(r, full=True, files=n)

    @staticmethod
    def file_dict(r):
        return {"id": r["id"], "task": r["task_key"], "name": r["name"], "size": r["size"], "kind": r["kind"],
                "image": r["kind"] in IMAGE_TYPES, "created_at": r["created_at"], "created_by": r["created_by"]}

    @staticmethod
    def journal_dict(r):
        data = json.loads(r["data"])
        return {"id": r["id"], "task": r["task_key"], "task_title": r["task_title"], "project": r["project_id"],
                "actor": r["actor_id"], "at": r["at"], "kind": r["kind"], "text": r["text"],
                "changes": data.get("changes", []), "via": r["via"], "session": data.get("session", ""),
                "v": r["task_version"], "gone": r["task_id"] is None and r["task_key"] is not None}

    def state(self, me):
        with self.lock:
            c = self.conn
            rev = int(c.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
            owner = self.is_owner(c, me["id"])
            acc = self.access(c, me["id"])
            every = {r["id"]: self.access(c, r["id"])
                     for r in c.execute("SELECT id FROM actors WHERE active = 1").fetchall()}
            projects = [{"id": r["id"], "area": r["area_id"], "name": r["name"], "note": r["note"],
                         "mem": r["mem"], "path": r["path"], "closed": bool(r["closed"]), "shared": r["shared"],
                         "paused": r["paused"], "archived": bool(r["archived"]), "sort": r["sort"],
                         "writers": [a for a, m in every.items() if m.get(r["id"]) == 2]}
                        for r in c.execute("SELECT * FROM projects ORDER BY sort, name") if r["id"] in acc]
            shown = {p["area"] for p in projects}
            areas = [{"id": r["id"], "name": r["name"], "prefix": r["prefix"], "closed": bool(r["closed"]),
                      "sort": r["sort"]} for r in c.execute("SELECT * FROM areas ORDER BY sort, name")
                     if owner or r["id"] in shown]
            nfiles = dict(c.execute("SELECT task_id, COUNT(*) FROM files WHERE deleted_at IS NULL GROUP BY task_id"))
            tasks = [self.task_dict(r, files=nfiles.get(r["id"], 0))
                     for r in c.execute("SELECT * FROM tasks ORDER BY id") if r["project_id"] in acc]
            rows = c.execute("SELECT * FROM actors ORDER BY sort, name").fetchall()
            if not owner:
                # участнику — только те, с кем у него общие проекты, свои модели и упомянутые в его задачах
                seen = {me["id"], me.get("owner")} | {r["id"] for r in rows if r["owner_id"] == me["id"]}
                seen |= {a for a, m in every.items() if m.keys() & acc.keys()}
                for t in tasks:
                    seen |= {t["holder"], t["created_by"], t["updated_by"]}
                rows = [r for r in rows if r["id"] in seen]
            actors = [self.actor_dict(r) for r in rows]
        return {"rev": rev, "today": dt.date.today().isoformat(), "me": public_actor(me), "owner": owner,
                "access": {p: LEVELS[n - 1] for p, n in acc.items()}, "actors": actors,
                "areas": areas, "projects": projects, "tasks": tasks}

    def task_row(self, c, key):
        r = c.execute("SELECT * FROM tasks WHERE key = ?", (norm_key(key),)).fetchone()
        if not r:
            raise ApiError(404, f"нет задачи {norm_key(key)}")
        return r

    def task(self, actor, key, journal_limit=200):
        with self.lock:
            r = self.task_row(self.conn, key)
            if r["project_id"] not in self.access(self.conn, actor["id"]):
                raise ApiError(404, f"нет задачи {r['key']}")
            j = self.conn.execute("SELECT * FROM journal WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                                  (r["id"], journal_limit)).fetchall()
            files = [self.file_dict(f) for f in self.conn.execute(
                "SELECT * FROM files WHERE task_id = ? AND deleted_at IS NULL ORDER BY id", (r["id"],))]
        return {"task": self.task_dict(r, full=True, files=len(files)), "journal": [self.journal_dict(x) for x in j],
                "files": files}

    def journal(self, actor, limit=100, before=None, who=None, project=None, since=None, task=None):
        sql, args = ["SELECT * FROM journal WHERE 1 = 1"], []
        with self.lock:
            if not self.is_owner(self.conn, actor["id"]):
                # участник видит записи только своих проектов; записи об участниках и разделах — владельцу
                acc = sorted(self.access(self.conn, actor["id"]))
                if not acc:
                    return []
                sql.append(f"AND project_id IN ({', '.join('?' * len(acc))})")
                args.extend(acc)
        if before:
            sql.append("AND id < ?")
            args.append(int(before))
        if who:
            sql.append("AND actor_id = ?")
            args.append(who)
        if project:
            sql.append("AND project_id = ?")
            args.append(project)
        if since:
            sql.append("AND at >= ?")
            args.append(since)
        if task:
            sql.append("AND task_key = ?")
            args.append(norm_key(task))
        sql.append("ORDER BY id DESC LIMIT ?")
        args.append(max(1, min(int(limit), 500)))
        return [self.journal_dict(r) for r in self.q(" ".join(sql), args)]

    # ----- запись: задачи -----

    def _actor_name(self, c, actor_id):
        if not actor_id:
            return "никто"
        r = c.execute("SELECT name FROM actors WHERE id = ?", (actor_id,)).fetchone()
        return r["name"] if r else actor_id

    def _project_name(self, c, pid):
        r = c.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
        return r["name"] if r else pid

    def _clean_task(self, c, body, creating):
        out = {}
        for name, (col, kind, label) in TASK_FIELDS.items():
            if name not in body:
                continue
            value = body[name]
            if kind == "project":
                if not isinstance(value, str) or not c.execute("SELECT 1 FROM projects WHERE id = ?",
                                                               (value,)).fetchone():
                    raise ApiError(400, f"нет проекта {value!r}")
            elif kind == "title":
                value = v_text(value, label, 200, required=True)
            elif kind == "status":
                if value not in STATUSES:
                    raise ApiError(400, "статус: " + ", ".join(STATUSES))
            elif kind == "actor":
                if value in ("", None):
                    value = None
                elif not isinstance(value, str) or not c.execute(
                        "SELECT 1 FROM actors WHERE id = ? AND active = 1", (value,)).fetchone():
                    raise ApiError(400, f"нет участника {value!r}")
            elif kind == "text":
                value = v_text(value, label, 500)
            elif kind == "notes":
                value = v_text(value, label, 20_000)
            elif kind == "date":
                value = v_date(value, label)
            elif kind == "checklist":
                value = v_checklist(value)
            out[col] = value
        if creating:
            if "project_id" not in out or "title" not in out:
                raise ApiError(400, "нужны project и title")
            out.setdefault("status", "inbox")
        return out

    def _journal(self, c, actor, via, kind, *, task=None, project=None, text="", changes=(), fields=(), at=None,
                 extra=None):
        data = dict(extra or {})
        if changes:
            data["changes"] = list(changes)
        if fields:
            data["fields"] = sorted(fields)
        if actor.get("session"):
            data["session"] = actor["session"]
        c.execute("INSERT INTO journal(task_id, task_key, task_title, project_id, actor_id, at, kind, text, data, "
                  "via, task_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (task["id"] if task else None, task["key"] if task else None, task["title"] if task else None,
                   project or (task["project_id"] if task else None), actor["id"], at or now_iso(), kind,
                   text, json.dumps(data, ensure_ascii=False), via or "", task["version"] if task else None))

    def _create_task(self, c, actor, via, body, imported=False):
        f = self._clean_task(c, body, creating=True)
        if not imported:
            acc = self.access(c, actor["id"])
            if not acc.get(f["project_id"]):
                raise ApiError(400, f"нет проекта {f['project_id']!r}")
            if acc[f["project_id"]] < 2:
                raise ApiError(403, f"в проекте «{self._project_name(c, f['project_id'])}» у вас доступ "
                                    f"только на чтение")
            self._check_holder(c, actor, f.get("holder"), f["project_id"])
        comment = v_text(body.get("comment"), "комментарий", 4000)
        stamp = now_iso()
        created_at = stamp
        if imported and body.get("created_at"):
            created_at = v_text(body["created_at"], "created_at", 40)
        if f["status"] in CLOSED and not f.get("done_on"):
            f["done_on"] = dt.date.today().isoformat()
        source = v_text(body.get("source"), "source", 1000) if imported else ""
        if source:
            f["notes"] = (f.get("notes", "") + "\n\nИсточник: " + source).strip()
        area = c.execute("SELECT a.id, a.prefix, a.next_num FROM projects p JOIN areas a ON a.id = p.area_id "
                         "WHERE p.id = ?", (f["project_id"],)).fetchone()
        key = f"{area['prefix']}-{area['next_num']}"
        c.execute("UPDATE areas SET next_num = next_num + 1 WHERE id = ?", (area["id"],))
        cols = {"key": key, "project_id": f["project_id"], "title": f["title"], "status": f["status"],
                "holder": f.get("holder"), "next_step": f.get("next_step", ""), "wait_for": f.get("wait_for", ""),
                "check_on": f.get("check_on"), "due_on": f.get("due_on"), "done_on": f.get("done_on"),
                "notes": f.get("notes", ""), "mem": f.get("mem", ""),
                "checklist": json.dumps(f.get("checklist", []), ensure_ascii=False),
                "created_at": created_at, "created_by": actor["id"], "updated_at": stamp,
                "updated_by": actor["id"], "version": 1}
        c.execute(f"INSERT INTO tasks({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  tuple(cols.values()))
        row = c.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
        changes = [f"Статус: {STATUS_RU[f['status']]}"]
        if f.get("holder"):
            changes.append(f"Держит: {self._actor_name(c, f['holder'])}")
        self._journal(c, actor, via, "create", task=row, text=comment, changes=changes,
                      at=created_at if imported else stamp, extra={"source": source} if source else None)
        for e in (body.get("journal") or []) if imported else ():
            who = e.get("actor") or actor["id"]
            if not c.execute("SELECT 1 FROM actors WHERE id = ?", (who,)).fetchone():
                raise ApiError(400, f"журнал: нет участника {who!r}")
            self._journal(c, {"id": who}, "перенос", "comment", task=row, text=v_text(e.get("text"), "журнал", 4000),
                          at=v_text(e.get("at"), "журнал.at", 40) or stamp)
        return row

    def create_task(self, actor, body):
        with self.tx() as c:
            row = self._create_task(c, actor, actor.get("via"), body)
            self.bump(c)
            return self.task_dict(row, full=True)

    def _check_clash(self, c, actor, row, ver, fields):
        """Правка по устаревшей версии карточки проходит, если с тех пор другие не трогали те же поля.

        Свои правки той же сессии (заголовок X-Pult-Session) чужими не считаются. Если между версиями
        есть правки без списка полей (сделаны до этапа 2), считаем, что задето всё.
        """
        if isinstance(ver, bool) or not isinstance(ver, int) or not 1 <= ver <= row["version"]:
            raise ApiError(400, f"version: номер версии карточки от 1 до {row['version']}")
        entries = c.execute("SELECT * FROM journal WHERE task_id = ? AND task_version > ? ORDER BY id",
                            (row["id"], ver)).fetchall()
        session = actor.get("session")
        known, touched = set(), set()
        for e in entries:
            if e["kind"] not in ("create", "change", "file"):  # вложение задевает только поле files
                continue
            known.add(e["task_version"])
            data = json.loads(e["data"])
            if session and e["actor_id"] == actor["id"] and data.get("session") == session:
                continue
            touched |= set(data.get("fields") or TASK_FIELDS)
        if not set(range(ver + 1, row["version"] + 1)) <= known:
            touched = set(TASK_FIELDS)
        clash = linked(touched) & linked(fields)
        if not clash:
            return
        names = [TASK_FIELDS[f][2].lower() for f in TASK_FIELDS if f in clash and not (
            f == "done_on" and "status" in clash)]
        if "files" in clash:
            names.append("вложения")
        raise ApiError(409, f"{row['key']} уже изменили ({self._actor_name(c, row['updated_by'])}, "
                            f"{row['updated_at'][:16].replace('T', ' ')}): {', '.join(names)} — карточка обновлена",
                       {"task": self.task_out(c, row), "conflicts": [self.journal_dict(e) for e in entries]})

    def update_task(self, actor, key, body):
        with self.tx() as c:
            row = self.task_row(c, key)
            acc = self.access(c, actor["id"])
            self._need_edit(c, acc, row["project_id"], row["key"])
            new = self._clean_task(c, body, creating=False)
            dest = new.get("project_id", row["project_id"])
            if dest != row["project_id"]:
                if not acc.get(dest):
                    raise ApiError(400, f"нет проекта {dest!r}")
                if acc[dest] < 2:
                    raise ApiError(403, f"в проекте «{self._project_name(c, dest)}» у вас доступ только на чтение")
            comment = v_text(body.get("comment"), "комментарий", 4000)
            if new.get("status") in CLOSED and row["status"] not in CLOSED:
                new["done_on"] = new.get("done_on") or row["done_on"] or dt.date.today().isoformat()
            elif "status" in new and new["status"] not in CLOSED and row["status"] in CLOSED:
                new["done_on"] = None
            sets, changes, fields = {}, [], set()
            for name, (col, kind, label) in TASK_FIELDS.items():
                if col not in new:
                    continue
                old = json.loads(row[col]) if kind == "checklist" else row[col]
                value = new[col]
                if old == value or (kind in ("text", "notes") and (old or "") == (value or "")):
                    continue
                sets[col] = json.dumps(value, ensure_ascii=False) if kind == "checklist" else value
                fields.add(name)
                if kind == "status":
                    changes.append(f"Статус: {STATUS_RU[old]} → {STATUS_RU[value]}")
                elif kind == "actor":
                    changes.append(f"Держит: {self._actor_name(c, old)} → {self._actor_name(c, value)}")
                elif kind == "project":
                    changes.append(f"Проект: {self._project_name(c, old)} → {self._project_name(c, value)}")
                elif kind == "title":
                    changes.append(f"Название: «{short(old, 60)}» → «{short(value, 60)}»")
                elif kind == "text":
                    changes.append(f"{label}: «{short(value)}»" if value else f"{label}: убрано")
                elif kind == "date":
                    if col == "done_on" and "status" in sets:
                        continue
                    changes.append(f"{label}: {ddmm(value)}" if value else f"{label}: дата убрана")
                elif kind == "notes":
                    changes.append("Описание добавлено" if not old else "Описание убрано" if not value
                                   else "Описание изменено")
                elif kind == "checklist":
                    changes.extend(checklist_changes(old, value))
            if not sets and not comment:
                return {"task": self.task_out(c, row), "unchanged": True}
            if sets.keys() & {"holder", "project_id"}:
                self._check_holder(c, actor, sets.get("holder", row["holder"]), sets.get("project_id", dest))
            if sets and body.get("version") is not None and body["version"] != row["version"]:
                self._check_clash(c, actor, row, body["version"], fields)
            if sets:
                sets.update(updated_at=now_iso(), updated_by=actor["id"])
                c.execute(f"UPDATE tasks SET {', '.join(k + ' = ?' for k in sets)}, version = version + 1 "
                          f"WHERE id = ?", (*sets.values(), row["id"]))
                row = c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            self._journal(c, actor, actor.get("via"), "change" if sets else "comment", task=row,
                          text=comment, changes=changes, fields=fields)
            self.bump(c)
            return {"task": self.task_out(c, row)}

    def delete_task(self, actor, key, version=None):
        with self.tx() as c:
            row = self.task_row(c, key)
            if row["project_id"] not in self.access(c, actor["id"]):
                raise ApiError(404, f"нет задачи {row['key']}")
            if not self.is_owner(c, actor["id"]):
                raise ApiError(403, "удалить задачу может только владелец Пульта; закройте её статусом «Снято»")
            if version is not None and version != row["version"]:
                self._check_clash(c, actor, row, version, set(TASK_FIELDS) | {"files"})
            snapshot = self.task_out(c, row)
            stamp = now_iso()
            # вложения уходят вместе с задачей; с диска их сотрёт уборка через FILES_KEEP_DAYS
            c.execute("UPDATE files SET deleted_at = ?, deleted_by = ? WHERE task_id = ? AND deleted_at IS NULL",
                      (stamp, actor["id"], row["id"]))
            self._journal(c, actor, actor.get("via"), "delete", task=row, changes=["Задача удалена"],
                          extra={"snapshot": snapshot})
            c.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
            self.bump(c)
            return {"deleted": row["key"]}

    # ----- вложения -----

    def can_attach(self, actor, key):
        """Проверка до приёма файла: нет доступа — не читаем тело зря. В add_file она повторяется."""
        with self.lock:
            row = self.task_row(self.conn, key)
            self._need_edit(self.conn, self.access(self.conn, actor["id"]), row["project_id"], row["key"])
            return row["key"]

    def files_used(self, keep_days=FILES_KEEP_DAYS):
        """Сколько места занимают вложения на диске, включая удалённые, которые уборка ещё не стёрла."""
        cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=keep_days)).replace(microsecond=0).isoformat()
        return self.q1("SELECT COALESCE(SUM(size), 0) FROM (SELECT MAX(size) AS size FROM files "
                       "WHERE deleted_at IS NULL OR deleted_at >= ? GROUP BY sha)", (cutoff,))[0]

    def _file_touch(self, c, actor, row, change, fid):
        """Вложение меняет карточку: новая версия (сверка правок видит, что задеты только вложения) и журнал."""
        c.execute("UPDATE tasks SET updated_at = ?, updated_by = ?, version = version + 1 WHERE id = ?",
                  (now_iso(), actor["id"], row["id"]))
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        self._journal(c, actor, actor.get("via"), "file", task=row, changes=[change], fields={"files"},
                      extra={"file": fid})
        self.bump(c)
        return row

    def add_file(self, actor, key, name, size, sha, kind):
        with self.tx() as c:
            row = self.task_row(c, key)
            self._need_edit(c, self.access(c, actor["id"]), row["project_id"], row["key"])
            cur = c.execute("INSERT INTO files(task_id, task_key, name, size, sha, kind, created_at, created_by) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (row["id"], row["key"], name, size, sha, kind, now_iso(), actor["id"]))
            row = self._file_touch(c, actor, row, f"Вложение: {name} ({human_size(size)})", cur.lastrowid)
            f = c.execute("SELECT * FROM files WHERE id = ?", (cur.lastrowid,)).fetchone()
            return {"file": self.file_dict(f), "task": self.task_out(c, row)}

    def _file_row(self, c, actor, fid):
        if not str(fid).isdigit():
            raise ApiError(400, "вложение: номер")
        r = c.execute("SELECT f.*, t.project_id FROM files f JOIN tasks t ON t.id = f.task_id "
                      "WHERE f.id = ? AND f.deleted_at IS NULL", (int(fid),)).fetchone()
        if not r or r["project_id"] not in self.access(c, actor["id"]):
            raise ApiError(404, "нет такого вложения")
        return r

    def file_for(self, actor, fid):
        with self.lock:
            return self._file_row(self.conn, actor, fid)

    def delete_file(self, actor, fid):
        with self.tx() as c:
            f = self._file_row(c, actor, fid)
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (f["task_id"],)).fetchone()
            self._need_edit(c, self.access(c, actor["id"]), row["project_id"], row["key"])
            c.execute("UPDATE files SET deleted_at = ?, deleted_by = ? WHERE id = ?", (now_iso(), actor["id"], f["id"]))
            row = self._file_touch(c, actor, row, f"Вложение удалено: {f['name']}", f["id"])
            return {"ok": True, "task": self.task_out(c, row)}

    # ----- утренняя сводка -----

    def digest(self, actor_id, base_url, today=None):
        """Сводка для участника — только то, что ему видно: просрочено, пора проверить, сроки на неделе,
        его задачи и что менялось за сутки. Возвращает текст и HTML для Telegram."""
        today = today or dt.date.today()
        t0 = today.isoformat()
        with self.lock:
            c = self.conn
            if not c.execute("SELECT 1 FROM actors WHERE id = ? AND active = 1", (actor_id,)).fetchone():
                raise ApiError(404, f"нет участника {actor_id!r}")
            acc = self.access(c, actor_id)
            owner = self.is_owner(c, actor_id)
            tasks = [r for r in c.execute("SELECT * FROM tasks ORDER BY id") if r["project_id"] in acc]
            names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM actors")}
            since = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).replace(microsecond=0).isoformat()
            log = [e for e in c.execute("SELECT actor_id, project_id FROM journal WHERE at >= ?", (since,))
                   if owner or e["project_id"] in acc]
        open_ = [t for t in tasks if t["status"] not in CLOSED]
        week = (today + dt.timedelta(days=7)).isoformat()
        late = sorted((t for t in open_ if t["due_on"] and t["due_on"] < t0), key=lambda t: t["due_on"])
        shown = {t["key"] for t in late}
        check = sorted((t for t in open_ if t["check_on"] and t["check_on"] <= t0 and t["key"] not in shown),
                       key=lambda t: t["check_on"])
        shown |= {t["key"] for t in check}
        soon = sorted((t for t in open_ if t["due_on"] and t0 <= t["due_on"] <= week and t["key"] not in shown),
                      key=lambda t: t["due_on"])
        shown |= {t["key"] for t in soon}
        mine = [t for t in open_ if t["holder"] == actor_id and t["key"] not in shown]
        doing = sorted((t for t in mine if t["status"] == "doing"), key=lambda t: t["due_on"] or t["check_on"] or "9999")
        queue = {s: sum(1 for t in mine if t["status"] == s) for s in ("next", "wait", "inbox")}
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        closed = [t for t in tasks if t["status"] == "done" and (t["done_on"] or "") >= yesterday]
        by_actor = {}
        for e in log:
            if e["actor_id"] != actor_id:
                by_actor[e["actor_id"]] = by_actor.get(e["actor_id"], 0) + 1

        def who(t):
            return "вы" if t["holder"] == actor_id else names.get(t["holder"], t["holder"]) if t["holder"] else "никто"

        def when(d):
            return "сегодня" if d == t0 else ddmm(d)

        sections = [
            ("🔴", "Просрочено", late, lambda t: f"срок {ddmm(t['due_on'])} · {who(t)}"),
            ("🟡", "Пора проверить", check, lambda t: (f"ждём: {short(t['wait_for'], 45)}" if t["wait_for"]
                                                      else f"проверить {ddmm(t['check_on'])}") + f" · {who(t)}"),
            ("📅", "Сроки на неделе", soon, lambda t: f"срок {when(t['due_on'])} · {who(t)}"),
            ("🔵", "В работе у вас", doing, lambda t: short(t["next_step"], 45) if t["next_step"] else ""),
        ]
        rest = ", ".join(f"{label} {queue[s]}" for s, label in (("next", "на очереди"), ("wait", "ждут"),
                                                                ("inbox", "не разобраны")) if queue[s])
        total = sum(by_actor.values())
        changes = ""
        if total:
            changes = (f"За сутки {total} {plural(total, 'изменение', 'изменения', 'изменений')}: "
                       + ", ".join(f"{names.get(a, a)} {n}" for a, n in sorted(by_actor.items(), key=lambda x: -x[1])))
        head = f"Пульт · {WEEKDAY_RU[today.weekday()]}, {today.day} {MONTH_RU[today.month - 1]}"

        def build(limit, as_html):
            esc = html.escape if as_html else (lambda s: s)
            link = (lambda k: f'<a href="{base_url}/#t={urllib.parse.quote(k)}">{k}</a>') if as_html else (lambda k: k)
            out = [f"<b>{esc(head)}</b>" if as_html else head]
            for icon, title, items, tail in sections:
                if not items:
                    continue
                out.append("")
                out.append(f"{icon} " + (f"<b>{title} · {len(items)}</b>" if as_html else f"{title} · {len(items)}"))
                for t in items[:limit]:
                    extra = tail(t)
                    out.append(f"{link(t['key'])} {esc(short(t['title'], 55))}" + (f" — {esc(extra)}" if extra else ""))
                if len(items) > limit:
                    out.append(f"… и ещё {len(items) - limit}")
            if not any(items for _i, _t, items, _f in sections[:3]):
                out += ["", "Ничего не горит: просроченного нет, проверять сегодня нечего."]
            if rest:
                out += ["", f"Ещё у вас: {rest}."]
            if changes or closed:
                bits = [esc(changes)] if changes else []
                if closed:
                    bits.append(f"Закрыто {len(closed)}: " + ", ".join(link(t["key"]) for t in closed[:6])
                                + (f" и ещё {len(closed) - 6}" if len(closed) > 6 else ""))
                out += ["", ". ".join(bits) + "."]
            out += ["", f'<a href="{base_url}/">Открыть Пульт</a>' if as_html else f"Открыть: {base_url}/"]
            return "\n".join(out)

        limit = 8
        while limit > 1 and len(build(limit, False)) > DIGEST_LIMIT:
            limit -= 1
        return {"text": build(limit, False), "html": build(limit, True),
                "counts": {"late": len(late), "check": len(check), "soon": len(soon), "doing": len(doing),
                           "mine": len(mine), "changes": total, "closed": len(closed)}}

    def digest_targets(self):
        """Кому слать сводку: {участник: {"chat": …, "last": дата последней}} — строки meta 'digest:<id>'."""
        return {r["k"][len("digest:"):]: json.loads(r["v"]) for r in self.q("SELECT k, v FROM meta WHERE k LIKE 'digest:%'")}

    def set_digest_target(self, actor_id, value):
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE k = ?", ("digest:" + actor_id,))
            else:
                c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", ("digest:" + actor_id, json.dumps(value)))

    # ----- запись: проекты, разделы, участники -----

    def _clean_simple(self, c, body, fields, what):
        out = {}
        for name, (col, kind) in fields.items():
            if name not in body:
                continue
            value = body[name]
            if kind == "title":
                value = v_text(value, f"{what}.{name}", 120, required=True)
            elif kind == "text":
                value = v_text(value, f"{what}.{name}", 500)
            elif kind == "bool":
                value = 1 if value else 0
            elif kind == "int":
                value = v_int(value, f"{what}.{name}")
            elif kind == "date":
                value = v_date(value, f"{what}.{name}")
            elif kind == "area":
                if not isinstance(value, str) or not c.execute("SELECT 1 FROM areas WHERE id = ?",
                                                               (value,)).fetchone():
                    raise ApiError(400, f"нет раздела {value!r}")
            elif kind == "prefix":
                value = norm_key(value)
                if not PREFIX_RE.match(value):
                    raise ApiError(400, "префикс: латиница и цифры, до 6 знаков")
            elif kind == "kind":
                if value not in KINDS:
                    raise ApiError(400, "вид: person или model")
            elif kind == "role":
                if value not in ROLES:
                    raise ApiError(400, "роль: owner или member")
            elif kind == "owner":
                if value in ("", None):
                    value = None
                elif not isinstance(value, str) or not c.execute(
                        "SELECT 1 FROM actors WHERE id = ? AND kind = 'person'", (value,)).fetchone():
                    raise ApiError(400, f"хозяин модели — человек из участников, а не {value!r}")
            out[col] = value
        return out

    def _upsert(self, c, table, ident, values, required):
        exists = c.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ident,)).fetchone()
        if exists:
            if values:
                c.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in values)} WHERE id = ?",
                          (*values.values(), ident))
            return False
        missing = [k for k in required if k not in values]
        if missing:
            raise ApiError(400, f"{table}: для нового нужны {', '.join(missing)}")
        cols = {"id": ident, **values}
        c.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  tuple(cols.values()))
        return True

    def upsert_area(self, c, body):
        ident = v_id(body.get("id"), "раздел.id")
        values = self._clean_simple(c, body, AREA_FIELDS, "раздел")
        if "prefix" in values:
            old = c.execute("SELECT prefix FROM areas WHERE id = ?", (ident,)).fetchone()
            if old and old["prefix"] != values["prefix"] and c.execute(
                    "SELECT 1 FROM tasks t JOIN projects p ON p.id = t.project_id WHERE p.area_id = ? LIMIT 1",
                    (ident,)).fetchone():
                raise ApiError(400, "префикс раздела с задачами не меняется: номера задач уже выданы")
        return self._upsert(c, "areas", ident, values, ("name", "prefix"))

    def upsert_project(self, c, body):
        ident = v_id(body.get("id"), "проект.id")
        values = self._clean_simple(c, body, PROJECT_FIELDS, "проект")
        return self._upsert(c, "projects", ident, values, ("area_id", "name"))

    def upsert_actor(self, c, body):
        ident = v_id(body.get("id"), "участник.id")
        values = self._clean_simple(c, body, ACTOR_FIELDS, "участник")
        return self._upsert(c, "actors", ident, values, ("name", "kind"))

    def save_simple(self, actor, kind, body, ident=None):
        if ident is not None:
            body = dict(body, id=ident)
        with self.tx() as c:
            self.need_owner(c, actor, "менять разделы и проекты")
            fn = {"area": self.upsert_area, "project": self.upsert_project}[kind]
            if ident is not None and not c.execute(
                    f"SELECT 1 FROM {kind}s WHERE id = ?", (ident,)).fetchone():
                raise ApiError(404, f"нет: {ident}")
            created = fn(c, body)
            what = {"area": "Раздел", "project": "Проект"}[kind]
            self._journal(c, actor, actor.get("via"), kind, project=body["id"] if kind == "project" else None,
                          text="", changes=[f"{what} «{body.get('name') or body['id']}» "
                                            f"{'создан' if created else 'изменён'}"])
            self.bump(c)
        return {"ok": True, "created": created}

    def import_bundle(self, actor, body):
        if not isinstance(body.get("tasks", []), list):
            raise ApiError(400, "tasks: нужен список")
        with self.tx() as c:
            self.need_owner(c, actor, "переносить задачи")
            for a in body.get("actors") or []:
                self.upsert_actor(c, a)
            for a in body.get("areas") or []:
                self.upsert_area(c, a)
            for p in body.get("projects") or []:
                self.upsert_project(c, p)
            keys = [self._create_task(c, actor, "перенос", t, imported=True)["key"] for t in body.get("tasks") or []]
            self.bump(c)
        return {"ok": True, "tasks": keys}

    # ----- участники: доступ, приглашения, ключи -----

    def _actor_row(self, c, ident):
        r = c.execute("SELECT * FROM actors WHERE id = ?", (ident,)).fetchone()
        if not r:
            raise ApiError(404, f"нет участника {ident!r}")
        return r

    def _people_journal(self, c, actor, text):
        self._journal(c, actor, actor.get("via"), "actor", changes=[text])

    def _scope_name(self, c, kind, target):
        if kind == "all":
            return "все проекты, кроме закрытых"
        if kind == "area":
            r = c.execute("SELECT name FROM areas WHERE id = ?", (target,)).fetchone()
            return f"раздел «{r['name'] if r else target}»"
        return f"проект «{self._project_name(c, target)}»"

    @staticmethod
    def _guard_owners(c):
        if not c.execute("SELECT 1 FROM actors WHERE role = 'owner' AND kind = 'person' AND active = 1 "
                         "AND owner_id IS NULL").fetchone():
            raise ApiError(400, "должен остаться хотя бы один владелец Пульта — человек с действующим доступом")

    def _free_id(self, c, base):
        ident, n = base, 2
        while c.execute("SELECT 1 FROM actors WHERE id = ?", (ident,)).fetchone():
            ident, n = f"{base}-{n}", n + 1
        return ident

    @staticmethod
    def _free_color(c):
        used = [r["color"] for r in c.execute("SELECT color FROM actors")]
        return min(("c1", "c2", "c3", "c4", "c5"), key=lambda k: (used.count(k), k))

    def save_actor(self, me, body, ident=None):
        """Новый участник или правка. Владелец — кто угодно и что угодно; участник — только свои модели."""
        with self.tx() as c:
            owner = self.is_owner(c, me["id"])
            if ident is None:
                name = v_text(body.get("name"), "участник.name", 120, required=True)
                if not owner:
                    if body.get("kind", "model") != "model":
                        raise ApiError(403, "участник может завести только свою модель")
                    body = {"id": body.get("id"), "name": name, "kind": "model", "note": body.get("note", ""),
                            "owner": me["id"], "role": "member"}
                ident = body.get("id") or self._free_id(c, slug(name))
                v_id(ident, "участник.id")
                if c.execute("SELECT 1 FROM actors WHERE id = ?", (ident,)).fetchone():
                    raise ApiError(400, f"участник {ident!r} уже есть")
                values = self._clean_simple(c, body, ACTOR_FIELDS, "участник")
                values.setdefault("kind", "person")
                values.setdefault("color", self._free_color(c))
                self._actor_consistent(values.get("kind"), values.get("owner_id"), ident)
                self._upsert(c, "actors", ident, values, ("name", "kind"))
                stamp = now_iso()
                if not owner:
                    # модель участника начинает с его же доступа; урезать можно в карточке модели
                    for g in c.execute("SELECT kind, target, level FROM grants WHERE actor_id = ?", (me["id"],)).fetchall():
                        c.execute("INSERT INTO grants(actor_id, kind, target, level, created_at, created_by) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", (ident, g["kind"], g["target"], g["level"], stamp, me["id"]))
                for g in (body.get("grants") or []) if owner else ():
                    kind, target, level = self._clean_grant(c, g)
                    if level:
                        c.execute("INSERT OR REPLACE INTO grants(actor_id, kind, target, level, created_at, created_by) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", (ident, kind, target, level, stamp, me["id"]))
                what = "Модель" if values["kind"] == "model" else "Участник"
                whose = f" (хозяин: {self._actor_name(c, values['owner_id'])})" if values.get("owner_id") else ""
                self._people_journal(c, me, f"{what} «{name}» добавлен{'а' if what == 'Модель' else ''}{whose}")
                created = True
            else:
                row = self._actor_row(c, ident)
                if not owner:
                    if row["owner_id"] != me["id"]:
                        raise ApiError(403, "участник меняет только свои модели")
                    extra = set(body) - {"id", "name", "note", "color", "active"}
                    if extra:
                        raise ApiError(403, "в своей модели можно менять имя, заметку, цвет и доступ: "
                                            + ", ".join(sorted(extra)) + " — только владелец Пульта")
                values = self._clean_simple(c, body, ACTOR_FIELDS, "участник")
                values = {k: v for k, v in values.items() if row[k] != v}
                kind, owner_id = values.get("kind", row["kind"]), values.get("owner_id", row["owner_id"])
                self._actor_consistent(kind, owner_id, ident)
                if values:
                    c.execute(f"UPDATE actors SET {', '.join(k + ' = ?' for k in values)} WHERE id = ?",
                              (*values.values(), ident))
                    self._guard_owners(c)
                    if values.get("active") == 0:
                        c.execute("DELETE FROM sessions WHERE actor_id = ?", (ident,))
                    bits = []
                    if "role" in values:
                        bits.append(f"роль: {ROLE_RU[row['role']]} → {ROLE_RU[values['role']]}")
                    if "active" in values:
                        bits.append("доступ " + ("включён" if values["active"] else "отключён"))
                    bits += [ACTOR_LABELS[k] for k in values if k not in ("role", "active")]
                    self._people_journal(c, me, f"«{values.get('name', row['name'])}»: {', '.join(bits)}")
                created = False
            self.bump(c)
        return {"ok": True, "created": created, "id": ident}

    @staticmethod
    def _actor_consistent(kind, owner_id, ident):
        if kind == "person" and owner_id:
            raise ApiError(400, "хозяин бывает только у модели")
        if owner_id and owner_id == ident:
            raise ApiError(400, "модель не может быть хозяином самой себе")

    def _clean_grant(self, c, body):
        if not isinstance(body, dict):
            raise ApiError(400, "доступ: нужен объект {kind, target, level}")
        kind, target, level = body.get("kind"), body.get("target") or "", body.get("level")
        if kind not in GRANT_KINDS:
            raise ApiError(400, "доступ: kind — all, area или project")
        if kind == "all":
            target = ""
        elif not isinstance(target, str) or not c.execute(
                f"SELECT 1 FROM {'areas' if kind == 'area' else 'projects'} WHERE id = ?", (target,)).fetchone():
            raise ApiError(400, f"доступ: нет {'раздела' if kind == 'area' else 'проекта'} {target!r}")
        if level in (None, "", "-"):
            level = None
        elif level not in LEVELS:
            raise ApiError(400, "доступ: level — view, edit или null (снять)")
        return kind, target, level

    def set_grant(self, me, ident, body):
        with self.tx() as c:
            who = self._actor_row(c, ident)
            kind, target, level = self._clean_grant(c, body)
            if not self.is_owner(c, me["id"]):
                if who["owner_id"] != me["id"]:
                    raise ApiError(403, "доступ выдаёт владелец Пульта; своим моделям — вы сами")
                if level and kind != "project":
                    raise ApiError(403, "своей модели можно выдать доступ только к отдельному проекту")
                if level and self.access(c, me["id"]).get(target, 0) < LEVEL_RANK[level]:
                    raise ApiError(403, "нельзя выдать модели больше, чем есть у вас")
            scope = self._scope_name(c, kind, target)
            if level:
                c.execute("INSERT OR REPLACE INTO grants(actor_id, kind, target, level, created_at, created_by) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (ident, kind, target, level, now_iso(), me["id"]))
                self._people_journal(c, me, f"Доступ «{who['name']}»: {scope} — {LEVEL_RU[level]}")
            else:
                gone = c.execute("DELETE FROM grants WHERE actor_id = ? AND kind = ? AND target = ?",
                                 (ident, kind, target)).rowcount
                if not gone:
                    return {"ok": True, "unchanged": True}
                self._people_journal(c, me, f"Доступ «{who['name']}» снят: {scope}")
            self.bump(c)
        return {"ok": True}

    @staticmethod
    def invite_dict(r):
        state = ("revoked" if r["revoked_at"] else "used" if r["used_at"]
                 else "expired" if dt.datetime.fromisoformat(r["expires_at"]) < dt.datetime.now().astimezone()
                 else "active")
        return {"id": r["id"], "actor": r["actor_id"], "note": r["note"], "created_at": r["created_at"],
                "created_by": r["created_by"], "expires_at": r["expires_at"], "used_at": r["used_at"],
                "revoked_at": r["revoked_at"], "state": state}

    def create_invite(self, me, ident, body):
        """Одноразовая ссылка для входа человеку: новому — приглашение, своему — вход на другом устройстве."""
        with self.tx() as c:
            who = self._actor_row(c, ident)
            if who["kind"] != "person":
                raise ApiError(400, "ссылка для входа — только человеку; модели выдайте ключ")
            if not who["active"]:
                raise ApiError(400, f"у «{who['name']}» доступ отключён")
            owner = self.is_owner(c, me["id"])
            if not owner and ident != me["id"]:
                raise ApiError(403, "ссылку для другого человека выдаёт владелец Пульта")
            days = v_int(body.get("days", INVITE_DAYS), "days")
            limit = 30 if owner else INVITE_DAYS
            if not 1 <= days <= limit:
                raise ApiError(400, f"days: от 1 до {limit}")
            note = v_text(body.get("note"), "заметка", 60)
            token = secrets.token_urlsafe(24)
            expires = (dt.datetime.now().astimezone() + dt.timedelta(days=days)).replace(microsecond=0).isoformat()
            cur = c.execute("INSERT INTO invites(hash, actor_id, note, created_at, created_by, expires_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (sha(token), ident, note, now_iso(), me["id"], expires))
            r = c.execute("SELECT * FROM invites WHERE id = ?", (cur.lastrowid,)).fetchone()
            self._people_journal(c, me, f"Ссылка для входа «{who['name']}» до {ddmm(expires)}"
                                        + (f" ({note})" if note else ""))
            self.bump(c)
        return {"token": token, "invite": self.invite_dict(r)}

    def use_invite(self, token, agent):
        """Вход по приглашению: ссылка гаснет, человек получает сессию на этом устройстве."""
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        with self.tx() as c:
            r = c.execute("SELECT * FROM invites WHERE hash = ?", (sha(token),)).fetchone()
            if not r or self.invite_dict(r)["state"] != "active":
                return None
            a = c.execute("SELECT a.* FROM actors a WHERE a.id = ? AND a.kind = 'person' AND " + self.ACTIVE_SQL,
                          (r["actor_id"],)).fetchone()
            if not a:
                return None
            # Сессии держатся за ключ: отзыв ключа закрывает вход на этом устройстве. Сам ключ никому
            # не нужен — хеш от случайной строки, которая нигде не сохраняется.
            key_hash = sha(secrets.token_urlsafe(32))
            stamp = now_iso()
            c.execute("INSERT INTO keys(hash, actor_id, label, created_at, last_used_at) VALUES (?, ?, ?, ?, ?)",
                      (key_hash, a["id"], (r["note"] or "вход по ссылке") + " " + ddmm(stamp), stamp, stamp))
            c.execute("UPDATE invites SET used_at = ?, key_hash = ? WHERE id = ?", (stamp, key_hash, r["id"]))
            sid = self._session(c, a["id"], key_hash, agent)
            actor = self.actor_dict(a)
            self._people_journal(c, dict(actor, via="ссылка"), f"«{a['name']}»: вход по ссылке-приглашению")
            self.bump(c)
        return sid, actor

    def create_key(self, me, ident, body):
        """Постоянный ключ модели для заголовка Authorization: Bearer. Секрет показывается один раз."""
        with self.tx() as c:
            who = self._actor_row(c, ident)
            if who["kind"] != "model":
                raise ApiError(400, "постоянный ключ — только модели; человеку выдайте ссылку для входа")
            if not who["active"]:
                raise ApiError(400, f"у «{who['name']}» доступ отключён")
            if not self.is_owner(c, me["id"]) and who["owner_id"] != me["id"]:
                raise ApiError(403, "ключ модели выдаёт её хозяин или владелец Пульта")
            label = v_text(body.get("label"), "название ключа", 40) or f"{ident}-{dt.date.today():%d%m}"
            token = secrets.token_urlsafe(32)
            c.execute("INSERT INTO keys(hash, actor_id, label, created_at) VALUES (?, ?, ?, ?)",
                      (sha(token), ident, label, now_iso()))
            self._people_journal(c, me, f"Ключ «{label}» для модели «{who['name']}»")
            r = c.execute("SELECT * FROM keys WHERE hash = ?", (sha(token),)).fetchone()
            key = self.key_dict(c, r)
            self.bump(c)
        return {"token": token, "key": key}

    def revoke_key(self, me, kid):
        if not re.match(r"^[0-9a-f]{12,64}$", kid or ""):
            raise ApiError(400, "ключ: начало хеша, 12 знаков и больше")
        with self.tx() as c:
            rows = c.execute("SELECT k.*, a.owner_id, a.name FROM keys k JOIN actors a ON a.id = k.actor_id "
                             "WHERE k.hash LIKE ?", (kid + "%",)).fetchall()
            mine = [r for r in rows if self.is_owner(c, me["id"]) or me["id"] in (r["actor_id"], r["owner_id"])]
            if len(mine) != 1:
                raise ApiError(404, "нет такого ключа")
            k = mine[0]
            if k["revoked_at"]:
                return {"ok": True, "unchanged": True}
            c.execute("UPDATE keys SET revoked_at = ? WHERE hash = ?", (now_iso(), k["hash"]))
            c.execute("DELETE FROM sessions WHERE key_hash = ?", (k["hash"],))
            self._people_journal(c, me, f"Ключ «{k['label']}» («{k['name']}») отозван, его входы закрыты")
            self.bump(c)
        return {"ok": True}

    def revoke_invite(self, me, iid):
        if not str(iid).isdigit():
            raise ApiError(400, "приглашение: номер")
        with self.tx() as c:
            r = c.execute("SELECT i.*, a.name FROM invites i JOIN actors a ON a.id = i.actor_id WHERE i.id = ?",
                          (int(iid),)).fetchone()
            if not r or not (self.is_owner(c, me["id"]) or r["actor_id"] == me["id"]):
                raise ApiError(404, "нет такого приглашения")
            if self.invite_dict(r)["state"] != "active":
                return {"ok": True, "unchanged": True}
            c.execute("UPDATE invites SET revoked_at = ? WHERE id = ?", (now_iso(), r["id"]))
            self._people_journal(c, me, f"Ссылка для входа «{r['name']}» отозвана")
            self.bump(c)
        return {"ok": True}

    @staticmethod
    def key_dict(c, r):
        sessions = c.execute("SELECT created_at, last_seen_at, agent FROM sessions WHERE key_hash = ? "
                             "ORDER BY last_seen_at DESC", (r["hash"],)).fetchall()
        return {"kid": r["hash"][:12], "label": r["label"], "created_at": r["created_at"],
                "last_used_at": r["last_used_at"], "revoked_at": r["revoked_at"],
                "sessions": [dict(s) for s in sessions]}

    def people(self, me):
        """Участники с доступом, ключами и приглашениями: владельцу — все, участнику — он сам и его модели."""
        with self.lock:
            c = self.conn
            owner = self.is_owner(c, me["id"])
            rows = c.execute("SELECT * FROM actors ORDER BY sort, name").fetchall()
            if not owner:
                rows = [r for r in rows if me["id"] in (r["id"], r["owner_id"])]
            out = []
            for r in rows:
                d = self.actor_dict(r)
                d["grants"] = [dict(g) for g in c.execute(
                    "SELECT kind, target, level, created_at, created_by FROM grants WHERE actor_id = ? "
                    "ORDER BY kind, target", (r["id"],)).fetchall()]
                d["access"] = {p: LEVELS[n - 1] for p, n in self.access(c, r["id"]).items()}
                d["keys"] = [dict(self.key_dict(c, k), current=k["hash"] == me.get("key_hash"))
                             for k in c.execute("SELECT * FROM keys WHERE actor_id = ? ORDER BY created_at DESC",
                                                (r["id"],)).fetchall()]
                d["invites"] = [self.invite_dict(i) for i in c.execute(
                    "SELECT * FROM invites WHERE actor_id = ? ORDER BY id DESC", (r["id"],)).fetchall()]
                out.append(d)
        return {"people": out, "owner": owner, "me": me["id"]}


def public_actor(a):
    return {k: a.get(k) for k in ("id", "name", "kind", "color", "note", "role", "owner")} if a else None


class LoginLimiter:
    """Не больше 10 неудачных входов с адреса за 10 минут: ключи длинные, это от шума, а не от подбора."""

    def __init__(self, limit=10, window=600):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, ip):
        with self.lock:
            now = time.monotonic()
            self.fails = {k: [t for t in v if now - t < self.window] for k, v in self.fails.items() if v}
            return len(self.fails.get(ip, [])) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.monotonic())


# ---------- HTTP ----------

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2",
                 ".png": "image/png", ".txt": "text/plain; charset=utf-8",
                 ".webmanifest": "application/manifest+json"}
PAGES = {"/": "index.html", "/login": "login.html"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def load_static(root):
    files = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            ext = os.path.splitext(name)[1].lower()
            if ext not in CONTENT_TYPES:
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as f:
                body = f.read()
            files[rel] = {"body": body, "type": CONTENT_TYPES[ext],
                          "etag": '"' + hashlib.sha256(body).hexdigest()[:20] + '"',
                          "cache": "public, max-age=2592000" if ext == ".woff2" else "no-cache"}
    return files


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pult"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20

    def log_message(self, fmt, *args):  # ничего не пишем: в адресах и заголовках бывают ключи
        pass

    def log_error(self, fmt, *args):
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    # ----- ответы -----

    def common_headers(self, csp=CSP):
        for k, v in (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("X-Frame-Options", "DENY"), ("Cross-Origin-Opener-Policy", "same-origin"),
                     ("Cross-Origin-Resource-Policy", "same-origin"), ("X-Robots-Tag", "noindex, nofollow"),
                     ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
                     ("Content-Security-Policy", csp)):
            self.send_header(k, v)

    def finish_unread(self):
        """Ошибка до чтения тела: остаток тела испортит следующий запрос — соединение закрываем."""
        if not self.body_read and int(self.headers.get("Content-Length") or 0) > 0:
            self.close_connection = True
            self.send_header("Connection", "close")

    def send_json(self, status, obj, extra_headers=()):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.common_headers()
        self.finish_unread()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_plain(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        self.finish_unread()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ----- разбор запроса -----

    def dispatch(self, method):
        self.body_read = False
        try:
            url = urllib.parse.urlsplit(self.path)
            if url.path.startswith("/api/"):
                self.api(method, url.path[len("/api/"):].strip("/").split("/"), urllib.parse.parse_qs(url.query))
            elif method in ("GET", "HEAD"):
                self.static(url.path)
            else:
                self.send_plain(405, "метод не поддерживается")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message, **e.extra})
        except (ConnectionError, socket.timeout, ssl.SSLError):
            self.close_connection = True
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self.send_json(500, {"error": "внутренняя ошибка"})
            except OSError:
                self.close_connection = True

    def static(self, path):
        name = PAGES.get(path) or (path[len("/static/"):] if path.startswith("/static/") else None)
        f = self.server.static.get(name) if name else None
        if not f:
            self.send_plain(404, "нет такой страницы")
            return
        if self.headers.get("If-None-Match") == f["etag"]:
            self.send_response(304)
            self.send_header("ETag", f["etag"])
            self.send_header("Cache-Control", f["cache"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", f["type"])
        self.send_header("Content-Length", str(len(f["body"])))
        self.send_header("ETag", f["etag"])
        self.send_header("Cache-Control", f["cache"])
        self.common_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(f["body"])

    def read_json(self, limit=1 << 20):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "нужен Content-Length")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "неверный Content-Length") from None
        if n > limit:
            raise ApiError(413, "слишком большой запрос")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if n and ctype != "application/json":
            raise ApiError(415, "нужен application/json")
        raw = self.rfile.read(n) if n else b""
        self.body_read = True
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "тело — не JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "нужен объект JSON")
        return data

    def upload(self, actor, key):
        """Вложение — сырым телом запроса, имя — в заголовке X-File-Name (UTF-8 в %-кодировке).
        Файл пишется на диск по частям, так что память службы от размера файла не зависит."""
        db, srv = self.server.db, self.server
        key = db.can_attach(actor, key)
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "нужен Content-Length")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "неверный Content-Length") from None
        if n <= 0:
            raise ApiError(400, "пустой файл")
        if n > srv.file_max:
            raise ApiError(413, f"файл больше {human_size(srv.file_max)}")
        if db.files_used() + n > srv.files_quota:
            raise ApiError(507, f"место для вложений кончилось: всего можно {human_size(srv.files_quota)}, "
                                f"удалите ненужные")
        os.makedirs(os.path.join(srv.files_dir, "tmp"), mode=0o700, exist_ok=True)
        if shutil.disk_usage(srv.files_dir).free - n < FILES_MIN_FREE:
            raise ApiError(507, "на диске сервера мало места")
        name = clean_name(urllib.parse.unquote(self.headers.get("X-File-Name") or ""))
        tmp = os.path.join(srv.files_dir, "tmp", secrets.token_hex(8))
        h, head, left = hashlib.sha256(), b"", n
        try:
            with open(tmp, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(1 << 16, left))
                    if not chunk:
                        raise ApiError(400, "файл пришёл не целиком")
                    if len(head) < 16:
                        head += chunk[:16 - len(head)]
                    h.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
            self.body_read = True
            sha = h.hexdigest()
            with FILES_LOCK:
                final = blob_path(srv.files_dir, sha)
                os.makedirs(os.path.dirname(final), mode=0o700, exist_ok=True)
                os.replace(tmp, final)  # такой же файл уже был — заменится тем же содержимым
                result = db.add_file(actor, key, name, n, sha, sniff(head))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)
        self.send_json(201, result)

    def download(self, actor, fid, attach):
        """Картинка — на страницу (inline), остальное — только на скачивание; везде своя строгая CSP."""
        f = self.server.db.file_for(actor, fid)
        path = blob_path(self.server.files_dir, f["sha"])
        try:
            fh = open(path, "rb")
        except FileNotFoundError:
            raise ApiError(404, "файла вложения нет на диске") from None
        with fh:
            etag = '"' + f["sha"][:32] + '"'
            inline = f["kind"] in IMAGE_TYPES and not attach
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", IMAGE_TYPES[f["kind"]] if inline else "application/octet-stream")
            self.send_header("Content-Length", str(f["size"]))
            self.send_header("Content-Disposition", disposition("inline" if inline else "attachment", f["name"]))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, max-age=2592000")
            self.common_headers(FILE_CSP)
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def base_url(self):
        return ("https" if self.server.tls else "http") + "://" + (self.headers.get("Host") or "127.0.0.1")

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def ip(self):
        # За обратным прокси все приходят с 127.0.0.1: адрес клиента — последний в X-Forwarded-For, его дописал прокси
        if self.server.proxy:
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if forwarded:
                return forwarded
        return self.client_address[0]

    def authenticate(self):
        actor = self._authenticate()
        # Метка сеанса модели: журнал различает сессии одного участника, а свои правки сессии
        # при сверке версий не считаются чужими.
        session = (self.headers.get("X-Pult-Session") or "").strip()
        actor["session"] = session if SESSION_RE.match(session) else ""
        return actor

    def _authenticate(self):
        db, limiter = self.server.db, self.server.limiter
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            if limiter.blocked(self.ip()):
                raise ApiError(429, "слишком много неудачных попыток, подождите 10 минут")
            actor = db.actor_by_key(header[7:].strip())
            if not actor:
                limiter.fail(self.ip())
                raise ApiError(401, "ключ не подходит")
            self.by_cookie = False
            return actor
        actor = db.actor_by_session(self.cookie(COOKIE))
        if actor:
            self.by_cookie = True
            return actor
        if self.server.dev_actor:
            self.by_cookie = False
            actor = db.actor(self.server.dev_actor)
            if actor:
                actor["via"] = "проверка"
                return actor
        raise ApiError(401, "нужен вход по ссылке")

    def check_csrf(self):
        """Запись по куки — только со своей страницы: свой заголовок и свой Origin."""
        if not self.by_cookie:
            return
        if self.headers.get("X-Pult") != "1":
            raise ApiError(403, "запрос не со страницы Пульта")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        if origin and urllib.parse.urlsplit(origin).netloc != host:
            raise ApiError(403, "чужой Origin")

    def api(self, method, parts, query):
        db = self.server.db
        one = lambda name, default=None: (query.get(name) or [default])[0]  # noqa: E731
        if parts == ["login"] and method == "POST":
            return self.login()
        actor = self.authenticate()
        if method not in ("GET", "HEAD"):
            self.check_csrf()
        head = parts[0] if parts else ""
        if method == "GET" and parts == ["state"]:
            etag = f'"r{db.rev()}-{actor["id"]}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            st = db.state(actor)
            st["limits"] = {"file": self.server.file_max}
            return self.send_json(200, st, [("ETag", etag)])
        if method == "GET" and parts == ["me"]:
            return self.send_json(200, {"me": public_actor(actor), "via": actor.get("via")})
        if method == "GET" and parts == ["digest"]:
            return self.send_json(200, db.digest(actor["id"], self.base_url()))
        if head == "file" and len(parts) == 2:
            if method in ("GET", "HEAD"):
                return self.download(actor, parts[1], one("dl") == "1")
            if method == "DELETE":
                return self.send_json(200, db.delete_file(actor, parts[1]))
        if method == "GET" and parts == ["journal"]:
            try:
                return self.send_json(200, {"journal": db.journal(
                    actor, limit=int(one("limit", 100)), before=one("before"), who=one("actor"),
                    project=one("project"), since=one("since"), task=one("task"))})
            except ValueError:
                raise ApiError(400, "limit и before — числа") from None
        if method == "GET" and parts == ["people"]:
            return self.send_json(200, db.people(actor))
        if head == "task":
            if method == "POST" and len(parts) == 1:
                return self.send_json(201, {"task": db.create_task(actor, self.read_json())})
            if len(parts) >= 2:
                key = urllib.parse.unquote(parts[1])
                if method == "GET" and len(parts) == 2:
                    try:
                        limit = int(one("journal", 200))
                    except ValueError:
                        raise ApiError(400, "journal — число") from None
                    return self.send_json(200, db.task(actor, key, journal_limit=max(1, min(limit, 1000))))
                if method == "PATCH" and len(parts) == 2:
                    return self.send_json(200, db.update_task(actor, key, self.read_json()))
                if method == "DELETE" and len(parts) == 2:
                    try:
                        ver = int(one("version")) if one("version") is not None else None
                    except ValueError:
                        raise ApiError(400, "version — число") from None
                    return self.send_json(200, db.delete_task(actor, key, ver))
                if method == "POST" and parts[2:] == ["comment"]:
                    body = self.read_json()
                    text = v_text(body.get("text"), "комментарий", 4000, required=True)
                    return self.send_json(200, db.update_task(actor, key, {"comment": text}))
                if method == "POST" and parts[2:] == ["files"]:
                    return self.upload(actor, key)
        if head in ("project", "area"):
            if method == "POST" and len(parts) == 1:
                return self.send_json(200, db.save_simple(actor, head, self.read_json()))
            if method == "PATCH" and len(parts) == 2:
                return self.send_json(200, db.save_simple(actor, head, self.read_json(), ident=parts[1]))
        if head == "actor":
            if method == "POST" and len(parts) == 1:
                return self.send_json(200, db.save_actor(actor, self.read_json()))
            if method == "PATCH" and len(parts) == 2:
                return self.send_json(200, db.save_actor(actor, self.read_json(), ident=parts[1]))
            action = {"grant": db.set_grant, "invite": db.create_invite, "key": db.create_key}.get(
                parts[2] if len(parts) == 3 else "")
            if method == "POST" and action:
                return self.send_json(200, action(actor, parts[1], self.read_json()))
        if method == "POST" and len(parts) == 3 and parts[2] == "revoke":
            if head == "key":
                return self.send_json(200, db.revoke_key(actor, parts[1]))
            if head == "invite":
                return self.send_json(200, db.revoke_invite(actor, parts[1]))
        if method == "POST" and parts == ["import"]:
            return self.send_json(200, db.import_bundle(actor, self.read_json(limit=8 << 20)))
        if method == "POST" and parts == ["logout"]:
            sid = self.cookie(COOKIE)
            if sid:
                db.drop_session(sid)
            return self.send_json(200, {"ok": True}, [("Set-Cookie", self.cookie_header("", 0))])
        raise ApiError(404, "нет такой ручки")

    def cookie_header(self, value, max_age):
        secure = "; Secure" if self.server.tls or self.server.proxy else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    def login(self):
        limiter, db = self.server.limiter, self.server.db
        if limiter.blocked(self.ip()):
            raise ApiError(429, "слишком много неудачных попыток, подождите 10 минут")
        body = self.read_json(limit=4096)
        agent = self.headers.get("User-Agent") or ""
        if "invite" in body:
            got = db.use_invite(body.get("invite"), agent)
            if not got:
                limiter.fail(self.ip())
                raise ApiError(403, "ссылка-приглашение не подходит: срок вышел, по ней уже вошли или её отозвали")
            sid, actor = got
        else:
            actor = db.actor_by_key(body.get("key"))
            if not actor:
                limiter.fail(self.ip())
                raise ApiError(403, "ссылка для входа не подходит или отозвана")
            sid = db.create_session(actor, agent)
        self.send_json(200, {"ok": True, "me": public_actor(actor)},
                       [("Set-Cookie", self.cookie_header(sid, SESSION_TTL_DAYS * 86400))])


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, addr, db, static, ssl_ctx=None, dev_actor=None, files_dir=None, file_max=FILE_MAX,
                 files_quota=FILES_QUOTA, proxy=False):
        self.db, self.static, self.ssl_ctx, self.dev_actor = db, static, ssl_ctx, dev_actor
        self.files_dir, self.file_max, self.files_quota = files_dir, file_max, files_quota
        self.tls = ssl_ctx is not None
        self.proxy = proxy
        self.limiter = LoginLimiter()
        super().__init__(addr, Handler)

    def finish_request(self, request, client_address):
        # TLS-рукопожатие — в потоке запроса, а не в общем accept: медленный клиент не держит остальных.
        request.settimeout(Handler.timeout)
        conn = request
        if self.ssl_ctx is not None:
            try:
                conn = self.ssl_ctx.wrap_socket(request, server_side=True)
            except OSError:
                return
        try:
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            if conn is not request:
                with contextlib.suppress(OSError):
                    conn.close()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout, ssl.SSLError)):
            return
        traceback.print_exc(file=sys.stderr)


def backup_once(db_path, backup_dir, keep=14):
    """Копия базы за сегодня средствами SQLite (целая и при открытой записи) и latest.db для ПК."""
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    dst = os.path.join(backup_dir, f"pult-{dt.date.today().isoformat()}.db")
    if os.path.exists(dst):
        return None
    tmp = dst + ".tmp"
    src, out = sqlite3.connect(db_path), sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    os.replace(tmp, dst)
    latest_tmp = os.path.join(backup_dir, "latest.db.tmp")
    with contextlib.suppress(FileNotFoundError):
        os.remove(latest_tmp)
    os.link(dst, latest_tmp)
    os.replace(latest_tmp, os.path.join(backup_dir, "latest.db"))
    old = sorted(f for f in os.listdir(backup_dir) if re.match(r"^pult-\d{4}-\d\d-\d\d\.db$", f))
    for name in old[:-keep]:
        os.remove(os.path.join(backup_dir, name))
    return dst


def upkeep_loop(db, db_path, backup_dir, files_dir):
    """Раз в час: копия базы за сутки (если задана папка) и уборка удалённых вложений."""
    while True:
        try:
            if backup_dir:
                backup_once(db_path, backup_dir)
            gc_files(db, files_dir)
        except Exception as e:  # копия и уборка не должны ронять службу
            print(f"upkeep: {e}", file=sys.stderr)
        time.sleep(3600)


def serve(args):
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    db = DB(args.db)
    db.expire_sessions()
    static = load_static(args.static)
    for need in ("index.html", "login.html", "app.js", "login.js", "app.css"):
        if need not in static:
            raise SystemExit(f"нет {need} в {args.static}")
    files_dir = args.files_dir or os.path.join(os.path.dirname(os.path.abspath(args.db)), "files")
    os.makedirs(files_dir, mode=0o700, exist_ok=True)
    plain = args.plain_http or args.proxy
    host = args.host or ("127.0.0.1" if plain else "0.0.0.0")
    ctx = None
    if args.dev_actor and not args.plain_http:
        raise SystemExit("--dev-actor только вместе с --plain-http")
    if plain:
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise SystemExit("--plain-http и --proxy только на 127.0.0.1")
    else:
        cred = os.environ.get("CREDENTIALS_DIRECTORY", "")
        cert = args.cert or os.path.join(cred, "cert")
        key = args.key or os.path.join(cred, "key")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
    threading.Thread(target=upkeep_loop, args=(db, args.db, args.backup_dir, files_dir), daemon=True).start()
    srv = Server((host, args.port), db, static, ctx, args.dev_actor, files_dir,
                 int(args.file_max_mb * (1 << 20)), int(args.files_quota_mb * (1 << 20)), proxy=args.proxy)
    print(f"pult: {'http' if ctx is None else 'https'}://{host}:{args.port}, база {args.db}", file=sys.stderr)
    srv.serve_forever()


# ---------- утренняя сводка в Telegram (pult-digest.timer → pult-digest.service) ----------

def read_bot_token(path):
    """Токен бота: файл вида TELEGRAM_BOT_TOKEN=… (как у плагина Telegram) или одна строка с токеном."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*['\"]?([^'\"\s]+)", text, re.M)
    token = m.group(1) if m else text.strip()
    if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", token):
        raise SystemExit(f"в {path} нет токена бота")
    return token


def send_telegram(token, chat, text, api=TELEGRAM_API, tries=3, pause=20):
    """sendMessage с повтором при сбое сети. В тексте ошибок токена нет: адрес запроса не печатаем."""
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    host = urllib.parse.urlsplit(api).hostname or ""
    handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost") else []
    opener = urllib.request.build_opener(*handlers)
    last = ""
    for attempt in range(tries):
        try:
            with opener.open(urllib.request.Request(f"{api}/bot{token}/sendMessage", data=data), timeout=30) as r:
                answer = json.loads(r.read().decode("utf-8"))
            if answer.get("ok"):
                return
            last = str(answer.get("description") or answer)[:200]
        except urllib.error.HTTPError as e:
            try:
                last = f"HTTP {e.code}: " + str(json.loads(e.read().decode("utf-8")).get("description"))[:200]
            except ValueError:
                last = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break  # неверный чат или разметка — повтор не поможет
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = type(e).__name__ + ": " + str(getattr(e, "reason", e))[:200]
        if attempt + 1 < tries:
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"Telegram не принял сводку: {last}")


def digest_cmd(args):
    """Без --send — напечатать сводки; с --send — отправить тем, кому заведена (admin digest-to), раз в день."""
    db = DB(args.db)
    targets = db.digest_targets()
    if args.actor:
        targets = {args.actor: targets.get(args.actor, {})}
    if not targets:
        print("сводку слать некому: pult_server.py admin digest-to <участник> <чат>")
        return 0
    if not args.send:
        for actor in targets:
            print(f"— {actor}\n{db.digest(actor, args.url)['text']}\n")
        return 0
    token = read_bot_token(args.token_file or os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", ""), "tg"))
    today, failed = dt.date.today().isoformat(), 0
    for actor, cfg in targets.items():
        if not cfg.get("chat"):
            print(f"{actor}: чат не задан", file=sys.stderr)
            failed += 1
            continue
        if cfg.get("last") == today and not args.force:
            print(f"{actor}: сегодня уже отправлено")
            continue
        try:
            send_telegram(token, cfg["chat"], db.digest(actor, args.url)["html"], api=args.api,
                          pause=0 if args.api != TELEGRAM_API else 20)
        except (RuntimeError, ApiError) as e:
            print(f"{actor}: {e}", file=sys.stderr)
            failed += 1
            continue
        db.set_digest_target(actor, dict(cfg, last=today))
        print(f"{actor}: отправлено")
    return 1 if failed else 0


# ---------- обслуживание (на сервере — от пользователя pult) ----------

def admin(args):
    db = DB(args.db)
    if args.cmd == "load-keys":
        with open(args.file, encoding="utf-8") as f:
            bundle = json.load(f)
        added = 0
        with db.tx() as c:
            # участников только заводим: имена, роли и доступ дальше правятся на странице
            for a in bundle.get("actors", []):
                if not c.execute("SELECT 1 FROM actors WHERE id = ?", (a.get("id"),)).fetchone():
                    db.upsert_actor(c, a)
            for k in bundle.get("keys", []):
                h = str(k["hash"]).lower()
                if not re.match(r"^[0-9a-f]{64}$", h):
                    raise SystemExit("hash: нужен sha256 в hex")
                if not c.execute("SELECT 1 FROM actors WHERE id = ?", (k["actor"],)).fetchone():
                    raise SystemExit(f"нет участника {k['actor']}")
                if c.execute("SELECT 1 FROM keys WHERE hash = ?", (h,)).fetchone():
                    continue
                c.execute("INSERT INTO keys(hash, actor_id, label, created_at) VALUES (?, ?, ?, ?)",
                          (h, k["actor"], k.get("label", ""), now_iso()))
                added += 1
            db.bump(c)
        print(f"ключей добавлено: {added}")
    elif args.cmd == "keys":
        for r in db.q("SELECT * FROM keys ORDER BY created_at"):
            state = "отозван " + r["revoked_at"][:16] if r["revoked_at"] else "действует"
            print(f"{r['hash'][:12]}  {r['actor_id']:<10} {r['label']:<16} создан {r['created_at'][:16]}  "
                  f"последний раз {(r['last_used_at'] or '—')[:16]}  {state}")
    elif args.cmd == "sessions":
        for r in db.q("SELECT * FROM sessions ORDER BY last_seen_at DESC"):
            print(f"{r['actor_id']:<10} ключ {r['key_hash'][:12]}  с {r['created_at'][:16]}  "
                  f"был {r['last_seen_at'][:16]}  {r['agent'][:60]}")
    elif args.cmd == "revoke":
        prefix = args.hash.lower()
        rows = db.q("SELECT hash FROM keys WHERE hash LIKE ? AND revoked_at IS NULL", (prefix + "%",))
        if len(rows) != 1:
            raise SystemExit(f"под начало {prefix!r} подходит ключей: {len(rows)}, нужен ровно один")
        with db.tx() as c:
            c.execute("UPDATE keys SET revoked_at = ? WHERE hash = ?", (now_iso(), rows[0]["hash"]))
            c.execute("DELETE FROM sessions WHERE key_hash = ?", (rows[0]["hash"],))
            db.bump(c)
        print("отозван, его сессии закрыты")
    elif args.cmd == "role":
        with db.tx() as c:
            if not c.execute("SELECT 1 FROM actors WHERE id = ?", (args.actor,)).fetchone():
                raise SystemExit(f"нет участника {args.actor}")
            c.execute("UPDATE actors SET role = ? WHERE id = ?", (args.role, args.actor))
            db.bump(c)
        print(f"{args.actor}: роль {ROLE_RU[args.role]}")
    elif args.cmd == "digest-to":
        if not db.actor(args.actor):
            raise SystemExit(f"нет участника {args.actor}")
        if not re.match(r"^-?\d{1,20}$", args.chat):
            raise SystemExit("чат: число — id пользователя или группы в Telegram")
        db.set_digest_target(args.actor, dict(db.digest_targets().get(args.actor, {}), chat=args.chat))
        print(f"{args.actor}: сводка будет приходить в Telegram")
    elif args.cmd == "digest-off":
        db.set_digest_target(args.actor, None)
        print(f"{args.actor}: сводка выключена")
    elif args.cmd == "digest-list":
        for actor, cfg in db.digest_targets().items():
            chat = str(cfg.get("chat", ""))
            print(f"{actor:<10} чат …{chat[-3:]}  последняя {cfg.get('last') or '—'}")
    elif args.cmd == "gc-files":
        print(f"стёрто файлов: {gc_files(db, args.files_dir or os.path.join(os.path.dirname(os.path.abspath(args.db)), 'files'))}")
    elif args.cmd == "stats":
        for table in ("actors", "keys", "sessions", "grants", "invites", "areas", "projects", "tasks", "journal",
                      "files"):
            print(f"{table:<9} {db.q1(f'SELECT COUNT(*) FROM {table}')[0]}")
        print(f"вложения  {human_size(db.files_used())} на диске")
        print(f"rev       {db.rev()}")
        print(f"schema    {db.q1('SELECT v FROM meta WHERE k = ?', ('schema',))[0]}")
    elif args.cmd == "wipe-tasks":
        if not args.yes:
            raise SystemExit("удаляет все задачи и журнал; повторите с --yes")
        with db.tx() as c:
            c.execute("DELETE FROM journal")
            c.execute("DELETE FROM tasks")
            c.execute("UPDATE areas SET next_num = 1")
            db.bump(c)
        print("задачи и журнал удалены, нумерация с 1")


# ---------- новый Пульт: владелец, его ИИ и первая карточка ----------

WELCOME_STEPS = [
    "Открыть страницу по ссылке для входа",
    "Вписать в инструкции ИИ (CLAUDE.md, AGENTS.md) блок из docs/for-ai.md",
    "Сказать ИИ: «заведи в Пульте мои проекты и задачи»",
    "В конце сессии ИИ сам двигает карточки — посмотреть журнал на странице",
]
AI_COLORS = {"claude": "claude", "codex": "codex"}


def repo_local_dir():
    """.local/ рядом с pult.py, если сервер запущен из папки с кодом; иначе None."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, ".local") if os.path.exists(os.path.join(root, "pult.py")) else None


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def init_cmd(args):
    """Пустая база → владелец-человек, его модель, раздел, проект и карточка «Подключить ИИ к Пульту».
    Ссылка для входа и ключ модели ложатся в файлы папки --out; в вывод секреты не попадают."""
    url = args.url.rstrip("/")
    if not re.match(r"^https?://[^/\s]+$", url):
        raise SystemExit("--url: адрес страницы, например http://127.0.0.1:9447")
    out = args.out or repo_local_dir()
    if not out:
        raise SystemExit("--out: папка для ссылки входа и ключа модели")
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    os.makedirs(out, mode=0o700, exist_ok=True)
    db = DB(args.db)
    if db.q1("SELECT COUNT(*) FROM actors")[0]:
        raise SystemExit(f"в {args.db} уже есть участники: init — только для новой базы")
    pid = slug(args.name)
    aid = None if args.no_ai else slug(args.ai)
    if aid == pid:
        aid += "-ai"
    with db.tx() as c:
        db.upsert_actor(c, {"id": pid, "name": args.name, "kind": "person", "role": "owner",
                            "color": db._free_color(c)})
        if aid:
            db.upsert_actor(c, {"id": aid, "name": args.ai, "kind": "model", "role": "owner", "owner": pid,
                                "color": AI_COLORS.get(aid) or db._free_color(c)})
        db.upsert_area(c, {"id": "main", "name": "Проекты", "prefix": args.prefix, "sort": 0})
        db.upsert_project(c, {"id": "pult", "area": "main", "name": "Пульт",
                              "note": "настройка Пульта и подключение ИИ", "sort": 0})
        db.bump(c)
    me = dict(db.actor(pid), via="установка")
    task = db.create_task(me, {
        "project": "pult", "title": "Подключить ИИ к Пульту", "status": "next", "holder": aid or pid,
        "next": "вписать в инструкции ИИ блок из docs/for-ai.md",
        "notes": "ИИ читает сводку в начале сессии (pult.py brief) и сам двигает карточки в конце (pult.py apply). "
                 "Эта карточка — первая: закройте её, когда ИИ впервые подвинет карточки сам.",
        "checklist": WELCOME_STEPS, "comment": "Пульт создан"})
    invite = db.create_invite(me, pid, {"days": 7, "note": "первый вход"})
    login = os.path.join(out, f"login-{pid}.txt")
    write_private(login, url + "/login#i=" + invite["token"] + "\n")
    write_private(os.path.splitext(login)[0] + ".url", "[InternetShortcut]\nURL=" + url + "/login#i=" + invite["token"] + "\n")
    print(f"Пульт создан: {args.db}")
    print(f"  {args.name} ({pid}) — владелец; ссылка для первого входа (одна, на 7 дней): {login} (и .url рядом)")
    if aid:
        key = db.create_key(me, aid, {"label": f"{aid}-1"})
        token_file = os.path.join(out, f"{aid}.token")
        write_private(token_file, key["token"] + "\n")
        print(f"  {args.ai} ({aid}) — модель ИИ, хозяин — {args.name}; ключ: {token_file}")
        cfg_path = os.path.join(out, "config.json")
        cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        if cfg.get("url", url).rstrip("/") != url:
            print(f"  {cfg_path} смотрит на другой Пульт ({cfg['url']}) — не трогаю; "
                  f"подключить: python pult.py connect {url} --token-file {token_file}")
        else:
            cfg.update(url=url, token_file=f"{aid}.token")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"  pult.py настроен: {cfg_path}")
    print(f"  первая карточка: {task['key']} «{task['title']}»")
    if urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost"):
        print(f"Дальше: python server/pult_server.py serve --plain-http --db {args.db} — и откройте ссылку для входа")
    else:
        print("Дальше: запустите службу (deploy/install.sh) и откройте ссылку для входа")


def main(argv=None):
    p = argparse.ArgumentParser(description="Пульт — доска дел для людей и их ИИ-агентов")
    sub = p.add_subparsers(dest="mode", required=True)
    i = sub.add_parser("init", help="новый Пульт: владелец, его ИИ, раздел, проект и первая карточка")
    i.add_argument("--db", required=True)
    i.add_argument("--name", required=True, help="ваше имя, как его показывать на странице")
    i.add_argument("--ai", default="Claude", help="имя модели ИИ, которой выдать ключ (по умолчанию Claude)")
    i.add_argument("--no-ai", action="store_true", help="без модели: ключ выдадите потом на странице")
    i.add_argument("--url", default="http://127.0.0.1:9447", help="адрес страницы для ссылки входа")
    i.add_argument("--prefix", default="P", help="префикс номеров задач первого раздела (P → P-1)")
    i.add_argument("--out", help="папка для ссылки входа, ключа и config.json; по умолчанию .local/ рядом с pult.py")
    s = sub.add_parser("serve")
    s.add_argument("--db", default="/var/lib/pult/pult.db")
    s.add_argument("--static", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
    s.add_argument("--host", help="по умолчанию 0.0.0.0, а с --plain-http — 127.0.0.1")
    s.add_argument("--port", type=int, default=9447)
    s.add_argument("--cert")
    s.add_argument("--key")
    s.add_argument("--backup-dir")
    s.add_argument("--plain-http", action="store_true", help="без TLS, только 127.0.0.1: Пульт на своём компьютере")
    s.add_argument("--proxy", action="store_true",
                   help="за обратным прокси с HTTPS (Caddy, nginx): без TLS, только 127.0.0.1, куки Secure, "
                        "адрес клиента — из X-Forwarded-For")
    s.add_argument("--dev-actor", help="с --plain-http: без входа считать запросы от этого участника")
    s.add_argument("--files-dir", help="папка вложений; по умолчанию files/ рядом с базой")
    s.add_argument("--file-max-mb", type=float, default=FILE_MAX / (1 << 20))
    s.add_argument("--files-quota-mb", type=float, default=FILES_QUOTA / (1 << 20))
    dg = sub.add_parser("digest", help="утренняя сводка: напечатать или отправить в Telegram")
    dg.add_argument("--db", default="/var/lib/pult/pult.db")
    dg.add_argument("--url", required=True, help="адрес страницы для ссылок, https://…:9447")
    dg.add_argument("--actor", help="только этому участнику")
    dg.add_argument("--send", action="store_true")
    dg.add_argument("--force", action="store_true", help="отправить, даже если сегодня уже отправляли")
    dg.add_argument("--token-file", help="по умолчанию $CREDENTIALS_DIRECTORY/tg")
    dg.add_argument("--api", default=TELEGRAM_API)
    a = sub.add_parser("admin")
    a.add_argument("--db", default="/var/lib/pult/pult.db")
    asub = a.add_subparsers(dest="cmd", required=True)
    dt_ = asub.add_parser("digest-to")
    dt_.add_argument("actor")
    dt_.add_argument("chat")
    asub.add_parser("digest-off").add_argument("actor")
    asub.add_parser("digest-list")
    asub.add_parser("gc-files").add_argument("--files-dir")
    lk = asub.add_parser("load-keys")
    lk.add_argument("file")
    asub.add_parser("keys")
    asub.add_parser("sessions")
    rv = asub.add_parser("revoke")
    rv.add_argument("hash")
    ro = asub.add_parser("role")
    ro.add_argument("actor")
    ro.add_argument("role", choices=ROLES)
    asub.add_parser("stats")
    wt = asub.add_parser("wipe-tasks")
    wt.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    if args.mode == "init":
        init_cmd(args)
    elif args.mode == "serve":
        serve(args)
    elif args.mode == "digest":
        return digest_cmd(args)
    else:
        admin(args)
    return 0


if __name__ == "__main__":
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
