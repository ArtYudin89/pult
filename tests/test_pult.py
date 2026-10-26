# -*- coding: utf-8 -*-
"""Проверки Пульта: сервер поднимается на 127.0.0.1 (без TLS и с самоподписанным сертификатом),
по нему гоняются API, вход по ссылке, защита записи по куки и команды pult.py.

    python -X utf8 tests/test_pult.py -v
"""
import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server", "pult_server.py")
STATIC = os.path.join(ROOT, "server", "static")
SEED = os.path.join(ROOT, "tests", "seed.json")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))
import pult as cli  # noqa: E402
import pult_server as srv  # noqa: E402


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, proc, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError("сервер не поднялся: " + proc.stderr.read().decode("utf-8", "replace"))
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return
        time.sleep(0.1)
    raise RuntimeError("сервер не открыл порт")


class Base(unittest.TestCase):
    tls = False
    serve_args = ()

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pult_test_")
        # обёртка не должна писать в настоящий .local/ и брать метку сессии Claude, в которой идут проверки
        cls.env = unittest.mock.patch.dict(os.environ, {
            "PULT_SESSION": "", "PULT_SEEN_DIR": os.path.join(cls.tmp, "seen"),
            "PULT_SECRETS_DIR": os.path.join(cls.tmp, "secrets")})
        cls.env.start()
        cls.db = os.path.join(cls.tmp, "t.db")
        cls.tok = {"artem": secrets.token_urlsafe(32), "claude": secrets.token_urlsafe(32)}
        with open(SEED, encoding="utf-8") as f:
            seed = json.load(f)
        keys = {"actors": seed["actors"],
                "keys": [{"actor": a, "hash": sha(t), "label": a + "-test"} for a, t in cls.tok.items()]}
        kf = os.path.join(cls.tmp, "keys.json")
        with open(kf, "w", encoding="utf-8") as f:
            json.dump(keys, f, ensure_ascii=False)
        out = subprocess.run([sys.executable, "-X", "utf8", SERVER, "admin", "--db", cls.db, "load-keys", kf],
                             capture_output=True, text=True, encoding="utf-8")
        assert out.returncode == 0, out.stderr
        cls.port = free_port()
        args = [sys.executable, "-X", "utf8", SERVER, "serve", "--db", cls.db, "--static", STATIC,
                "--host", "127.0.0.1", "--port", str(cls.port), *cls.serve_args]
        if cls.tls:
            cls.make_cert()
            args += ["--cert", cls.cert, "--key", cls.key]
        else:
            args += ["--plain-http"]
        cls.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        wait_port(cls.port, cls.proc)
        scheme = "https" if cls.tls else "http"
        cls.url = f"{scheme}://127.0.0.1:{cls.port}"
        cls.cfg = {"url": cls.url, "token": cls.tok["claude"]}
        cls.cfg_artem = {"url": cls.url, "token": cls.tok["artem"]}

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.proc.stderr.close()
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def seed_tasks(cls, tasks):
        with open(SEED, encoding="utf-8") as f:
            seed = json.load(f)
        bundle = {"areas": seed["areas"], "projects": seed["projects"], "tasks": tasks}
        for i, a in enumerate(bundle["areas"]):
            a["sort"] = i
        for i, p in enumerate(bundle["projects"]):
            p["sort"] = i
        return cli.request(cls.cfg, "POST", "/api/import", bundle)["tasks"]

    def web(self, method, path, body=None, cookie=""):
        """Запрос со страницы: куки сессии, свой заголовок и свой Origin."""
        r, payload = self.raw(method, path, body, headers={"Cookie": cookie, "X-Pult": "1", "Origin": self.url})
        return r.status, (json.loads(payload) if payload else {})

    def login_invite(self, token):
        r, body = self.raw("POST", "/api/login", {"invite": token})
        cookie = (r.getheader("Set-Cookie") or "").split(";")[0]
        return r.status, cookie

    def run_cli(self, *argv, **env):
        buf = io.StringIO()
        base = {"PULT_URL": self.url, "PULT_TOKEN": self.tok["claude"], "PULT_CONFIG": os.path.join(self.tmp, "no.json")}
        with unittest.mock.patch.dict(os.environ, dict(base, **env)), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def conn(self):
        if self.tls:
            ctx = ssl.create_default_context(cafile=self.cert)
            return http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10, context=ctx)
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def raw(self, method, path, body=None, headers=None):
        c = self.conn()
        data = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
        h = dict(headers or {})
        if isinstance(body, (dict, list)):
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r, payload

    def api(self, method, path, body=None, who="claude"):
        return cli.request({"url": self.url, "token": self.tok[who]}, method, path, body)


class PlainHttp(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(SEED, encoding="utf-8") as f:
            seed = json.load(f)
        bundle = {"areas": seed["areas"], "projects": seed["projects"], "tasks": [
            {"project": "lab", "title": "Первая задача", "status": "next", "holder": "claude",
             "next": "сделать шаг", "source": "memory/project_sr_lab.md"},
            {"project": "ms", "title": "T-1: ждём конвейер", "status": "wait", "wait": "конвейер по MR",
             "check": "2026-10-27", "checklist": [{"t": "открыть MR", "on": True}, {"t": "дождаться", "on": False}],
             "journal": [{"actor": "artem", "at": "2026-10-20T10:00:00+03:00", "text": "Старая запись"}]},
        ]}
        for i, a in enumerate(bundle["areas"]):
            a["sort"] = i
        for i, p in enumerate(bundle["projects"]):
            p["sort"] = i
        cls.imported = cli.request(cls.cfg, "POST", "/api/import", bundle)["tasks"]

    def test_01_unauthorized(self):
        r, body = self.raw("GET", "/api/state")
        self.assertEqual(r.status, 401)
        self.assertIn("вход", json.loads(body)["error"])
        r, _ = self.raw("GET", "/api/state", headers={"Authorization": "Bearer " + "x" * 43})
        self.assertEqual(r.status, 401)

    def test_02_import_and_state(self):
        self.assertEqual(self.imported, ["SR-1", "1C-1"])
        st = self.api("GET", "/api/state")
        self.assertEqual(st["me"]["id"], "claude")
        self.assertEqual([a["id"] for a in st["areas"]][:2], ["sr", "1c"])
        self.assertEqual(st["projects"][0]["id"], "agg")
        t = {x["key"]: x for x in st["tasks"]}
        self.assertNotIn("notes", t["SR-1"])
        self.assertTrue(t["SR-1"]["has_notes"])
        full = self.api("GET", "/api/task/SR-1")
        self.assertIn("Источник: memory/project_sr_lab.md", full["task"]["notes"])
        j = self.api("GET", "/api/task/1C-1")["journal"]
        self.assertEqual([e["kind"] for e in j], ["comment", "create"])
        self.assertEqual(j[0]["actor"], "artem")
        self.assertEqual(j[0]["at"], "2026-10-20T10:00:00+03:00")

    def test_03_patch_and_journal(self):
        r = self.api("PATCH", "/api/task/SR-1", {"status": "doing", "next": "второй шаг", "comment": "взял"})
        self.assertEqual(r["task"]["status"], "doing")
        e = self.api("GET", "/api/task/SR-1")["journal"][0]
        self.assertEqual(e["text"], "взял")
        self.assertIn("Статус: Следующие → В работе", e["changes"])
        self.assertIn("Следующий шаг: «второй шаг»", e["changes"])
        self.assertEqual(e["via"], "claude-test")
        same = self.api("PATCH", "/api/task/SR-1", {"status": "doing"})
        self.assertTrue(same.get("unchanged"))

    def test_04_version_conflict(self):
        t = self.api("GET", "/api/task/SR-1")["task"]
        self.api("PATCH", "/api/task/SR-1", {"next": "кто-то успел", "version": t["version"]}, who="artem")
        with self.assertRaises(cli.PultError) as ctx:
            self.api("PATCH", "/api/task/SR-1", {"next": "устаревшая правка", "version": t["version"]})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.payload["task"]["next"], "кто-то успел")

    def test_05_checklist_journal(self):
        t = self.api("GET", "/api/task/1C-1")["task"]
        items = [{"t": "открыть MR", "on": True}, {"t": "дождаться", "on": True}, {"t": "влить", "on": False}]
        self.api("PATCH", "/api/task/1C-1", {"checklist": items, "version": t["version"]})
        e = self.api("GET", "/api/task/1C-1")["journal"][0]
        self.assertEqual(e["changes"], ["Шаг «дождаться» — выполнен", "Добавлен шаг «влить»"])
        self.assertEqual(srv.checklist_changes([{"t": "a", "on": False}, {"t": "b", "on": False}],
                                               [{"t": "b", "on": False}, {"t": "a", "on": False}]),
                         ["Шаги переставлены"])
        self.assertEqual(srv.checklist_changes([{"t": "a", "on": False}], []), ["Убран шаг «a»"])

    def test_06_done_reopen(self):
        r = self.api("PATCH", "/api/task/SR-1", {"status": "done"})
        self.assertEqual(r["task"]["done_on"], time.strftime("%Y-%m-%d"))
        changes = self.api("GET", "/api/task/SR-1")["journal"][0]["changes"]
        self.assertEqual(changes, ["Статус: В работе → Готово"])
        r = self.api("PATCH", "/api/task/SR-1", {"status": "next"})
        self.assertIsNone(r["task"]["done_on"])

    def test_07_comment_and_validation(self):
        self.api("POST", "/api/task/SR-1/comment", {"text": "просто заметка"})
        e = self.api("GET", "/api/task/SR-1")["journal"][0]
        self.assertEqual((e["kind"], e["text"], e["changes"]), ("comment", "просто заметка", []))
        for bad in ({"status": "busy"}, {"holder": "nobody"}, {"due": "31.10"}, {"project": "nope"},
                    {"title": ""}, {"checklist": "не список"}):
            with self.assertRaises(cli.PultError, msg=bad) as ctx:
                self.api("PATCH", "/api/task/SR-1", bad)
            self.assertEqual(ctx.exception.status, 400, bad)

    def test_08_cookie_login_and_csrf(self):
        r, body = self.raw("POST", "/api/login", {"key": self.tok["artem"]})
        self.assertEqual(r.status, 200, body)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        sid = cookie.split(";")[0]
        r, body = self.raw("GET", "/api/state", headers={"Cookie": sid})
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body)["me"]["id"], "artem")
        r, _ = self.raw("PATCH", "/api/task/SR-1", {"next": "по куки"}, headers={"Cookie": sid})
        self.assertEqual(r.status, 403, "без X-Pult запись по куки не проходит")
        r, _ = self.raw("PATCH", "/api/task/SR-1", {"next": "по куки"},
                        headers={"Cookie": sid, "X-Pult": "1", "Origin": "https://evil.example"})
        self.assertEqual(r.status, 403, "чужой Origin")
        r, body = self.raw("PATCH", "/api/task/SR-1", {"next": "по куки"},
                           headers={"Cookie": sid, "X-Pult": "1", "Origin": self.url})
        self.assertEqual(r.status, 200, body)
        r, _ = self.raw("POST", "/api/task", b"project=lab&title=x",
                        headers={"Cookie": sid, "X-Pult": "1", "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 415)
        r, _ = self.raw("POST", "/api/logout", {}, headers={"Cookie": sid, "X-Pult": "1"})
        self.assertEqual(r.status, 200)
        r, _ = self.raw("GET", "/api/state", headers={"Cookie": sid})
        self.assertEqual(r.status, 401, "после выхода сессия не действует")

    def test_09_lookalike_key(self):
        t = self.api("GET", "/api/task/" + cli.q("1С-1"))["task"]  # русская «С»
        self.assertEqual(t["key"], "1C-1")
        self.assertEqual(cli.norm_key(" sr-1 "), "SR-1")

    def test_10_create_and_delete(self):
        t = self.api("POST", "/api/task", {"project": "tow", "title": "Удалить меня", "status": "dropped"})["task"]
        self.assertEqual(t["key"], "SR-2")
        self.assertEqual(t["done_on"], time.strftime("%Y-%m-%d"))
        self.api("DELETE", "/api/task/SR-2")
        with self.assertRaises(cli.PultError) as ctx:
            self.api("GET", "/api/task/SR-2")
        self.assertEqual(ctx.exception.status, 404)
        e = self.api("GET", "/api/journal?task=SR-2")["journal"][0]
        self.assertEqual((e["kind"], e["gone"]), ("delete", True))
        nxt = self.api("POST", "/api/task", {"project": "tow", "title": "После удаления"})["task"]
        self.assertEqual(nxt["key"], "SR-3", "номер удалённой задачи повторно не выдаётся")

    def test_11_static_and_etag(self):
        r, body = self.raw("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertIn(b"/static/app.js", body)
        r, _ = self.raw("GET", "/static/app.js")
        etag = r.getheader("ETag")
        r, _ = self.raw("GET", "/static/app.js", headers={"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        r, _ = self.raw("GET", "/static/../pult_server.py")
        self.assertEqual(r.status, 404)
        r, _ = self.raw("GET", "/login")
        self.assertEqual(r.status, 200)
        r, _ = self.raw("GET", "/api/state", headers={"Authorization": "Bearer " + self.tok["claude"]})
        etag = r.getheader("ETag")
        r, _ = self.raw("GET", "/api/state", headers={"Authorization": "Bearer " + self.tok["claude"],
                                                     "If-None-Match": etag})
        self.assertEqual(r.status, 304)

    def test_12_body_limits(self):
        c = self.conn()
        c.putrequest("POST", "/api/task")
        c.putheader("Authorization", "Bearer " + self.tok["claude"])
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(2 << 20))
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        self.assertEqual(r.getheader("Connection"), "close")
        c.close()
        r, _ = self.raw("POST", "/api/task", b"{not json", headers={"Authorization": "Bearer " + self.tok["claude"],
                                                                    "Content-Type": "application/json"})
        self.assertEqual(r.status, 400)

    def test_13_cli(self):
        env = {"PULT_URL": self.url, "PULT_TOKEN": self.tok["claude"], "PULT_CONFIG": os.path.join(self.tmp, "no.json")}
        ops = [
            {"op": "add", "project": "kit", "title": "Задача из пакета", "status": "next", "holder": "me",
             "next": "первый шаг", "check": "+3", "checklist": ["раз", "два"], "comment": "создал пакетом"},
            {"op": "set", "key": "SR-1", "status": "ждёт", "wait": "ответ", "check": "завтра"},
            {"op": "note", "key": "SR-1", "text": "заметка из пакета"},
            {"op": "step", "key": "1C-1", "check": "влить"},
            {"op": "done", "key": "SR-3", "result": "сделано пакетом"},
            {"op": "set", "key": "SR-999", "status": "done"},
        ]
        pf = os.path.join(self.tmp, "ops.json")
        with open(pf, "w", encoding="utf-8") as f:
            json.dump(ops, f, ensure_ascii=False)

        def run(*argv):
            buf = io.StringIO()
            with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                rc = cli.main(list(argv))
            return rc, buf.getvalue()

        rc, out = run("apply", pf)
        self.assertEqual(rc, 1, out)
        self.assertIn("SR-4: создана", out)
        self.assertIn("ОШИБКА", out)
        self.assertIn("выполнено 5 из 6", out)
        t = self.api("GET", "/api/task/SR-4")["task"]
        self.assertEqual((t["holder"], len(t["checklist"])), ("claude", 2))
        self.assertEqual(self.api("GET", "/api/task/SR-1")["task"]["status"], "wait")
        self.assertTrue(self.api("GET", "/api/task/1C-1")["task"]["checklist"][2]["on"])
        done = self.api("GET", "/api/task/SR-3")["task"]
        self.assertEqual((done["status"], done["next"]), ("done", "сделано пакетом"))
        for argv, needle in ((("brief",), "Пульт ·"), (("ls", "--project", "lab"), "SR-1"),
                             (("find", "конвейер"), "1C-1"), (("find", "1С-1"), "1C-1"),
                             (("show", "SR-1"), "Журнал"), (("log", "--since", "1d"), "записей"),
                             (("projects",), "Space Rangers"), (("whoami",), "Claude")):
            rc, out = run(*argv)
            self.assertEqual(rc, 0, out)
            self.assertIn(needle, out, argv)
        self.assertNotIn(self.tok["claude"], out)

    def test_14_parse_date(self):
        today = __import__("datetime").date(2026, 10, 26)
        self.assertEqual(cli.parse_date("27.10", today), "2026-10-27")
        self.assertEqual(cli.parse_date("1.1.27", today), "2027-01-01")
        self.assertEqual(cli.parse_date("+2w", today), "2026-11-09")
        self.assertEqual(cli.parse_date("+3", today), "2026-10-29")
        self.assertIsNone(cli.parse_date("-", today))
        with self.assertRaises(ValueError):
            cli.parse_date("31.02", today)

    def test_15_backup(self):
        bdir = os.path.join(self.tmp, "backup")
        dst = srv.backup_once(self.db, bdir)
        self.assertTrue(os.path.exists(dst))
        with contextlib.closing(sqlite3.connect(os.path.join(bdir, "latest.db"))) as c:
            self.assertGreater(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertIsNone(srv.backup_once(self.db, bdir), "второй раз за день копию не делает")

    def test_99_login_limiter(self):
        # промах ключа из test_01 уже посчитан, поэтому 429 придёт на 10-й попытке или раньше
        codes = [self.raw("POST", "/api/login", {"key": secrets.token_urlsafe(32)})[0].status for _ in range(11)]
        self.assertTrue(set(codes) <= {403, 429}, codes)
        self.assertEqual(codes[-1], 429, codes)
        self.assertGreaterEqual(codes.count(403), 9, codes)
        r, _ = self.raw("POST", "/api/login", {"key": self.tok["artem"]})
        self.assertEqual(r.status, 429, "после 10 промахов вход закрыт и для верного ключа")


class Access(Base):
    """Права этапа 2: приглашения, доступ к проектам, закрытые разделы, модели участников."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        titles = {"eltan": "Совместная задача", "lab": "Задача лаборатории", "health": "Задача здоровья",
                  "ms": "Задача МС", "trade": "Задача торговли"}
        cls.seed_tasks([{"project": p, "title": t, "status": "next", "holder": "artem"} for p, t in titles.items()])
        st = cli.request(cls.cfg, "GET", "/api/state")
        cls.k = {t["project"]: t["key"] for t in st["tasks"]}

    def test_a1_invite_and_visibility(self):
        r = self.api("POST", "/api/actor", {"name": "Гость", "grants": [
            {"kind": "project", "target": "eltan", "level": "edit"}]}, who="artem")
        self.assertEqual(r["id"], "gost", "id из имени латиницей")
        inv = self.api("POST", "/api/actor/gost/invite", {"days": 3, "note": "телефон"}, who="artem")
        status, cookie = self.login_invite(inv["token"])
        self.assertEqual(status, 200)
        self.assertEqual(self.login_invite(inv["token"])[0], 403, "по ссылке входят один раз")
        code, st = self.web("GET", "/api/state", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual((st["me"]["id"], st["owner"], st["access"]), ("gost", False, {"eltan": "edit"}))
        self.assertEqual([p["id"] for p in st["projects"]], ["eltan"])
        self.assertEqual([a["id"] for a in st["areas"]], ["sr"])
        self.assertEqual({t["project"] for t in st["tasks"]}, {"eltan"})
        self.assertNotIn("guest", {a["id"] for a in st["actors"]}, "лишних участников не видно")
        self.assertEqual(set(st["projects"][0]["writers"]), {"artem", "claude", "gost"})
        self.assertEqual(self.web("GET", "/api/task/" + self.k["lab"], cookie=cookie)[0], 404,
                         "чужая задача — как несуществующая")
        self.assertEqual(self.web("GET", "/api/task/" + self.k["eltan"], cookie=cookie)[0], 200)
        code, j = self.web("GET", "/api/journal?limit=500", cookie=cookie)
        self.assertTrue(j["journal"])
        self.assertEqual({e["project"] for e in j["journal"]}, {"eltan"})
        code, t = self.web("POST", "/api/task", {"project": "eltan", "title": "От гостя", "holder": "gost"}, cookie)
        self.assertEqual(code, 201, t)
        self.assertEqual(self.web("POST", "/api/task", {"project": "lab", "title": "Мимо"}, cookie)[0], 400)
        code, body = self.web("PATCH", "/api/task/" + self.k["eltan"], {"holder": "guest"}, cookie)
        self.assertEqual(code, 400, "держать задачу может только тот, кто правит проект")
        self.assertEqual(self.web("PATCH", "/api/task/" + self.k["eltan"], {"holder": "artem"}, cookie)[0], 200)
        self.assertEqual(self.web("DELETE", "/api/task/" + t["task"]["key"], cookie=cookie)[0], 403)
        self.assertEqual(self.web("POST", "/api/project", {"id": "x", "area": "sr", "name": "X"}, cookie)[0], 403)
        code, people = self.web("GET", "/api/people", cookie=cookie)
        self.assertEqual([u["id"] for u in people["people"]], ["gost"])
        key = people["people"][0]["keys"][0]
        self.assertTrue(key["label"].startswith("телефон") and key["current"])
        j = self.api("GET", "/api/journal?limit=30", who="artem")["journal"]
        self.assertTrue(any("вход по ссылке-приглашению" in " ".join(e["changes"]) for e in j))

    def test_a2_closed_not_in_wide_grants(self):
        self.api("POST", "/api/actor", {"id": "wide", "name": "Широкий", "grants": [
            {"kind": "all", "level": "view"}]}, who="artem")

        def acc():
            return next(u for u in self.api("GET", "/api/people")["people"] if u["id"] == "wide")["access"]
        a = acc()
        self.assertIn("lab", a)
        self.assertIn("trade", a)
        self.assertNotIn("ms", a, "закрытый раздел не входит во «все проекты»")
        self.assertNotIn("health", a, "закрытый проект не входит во «все проекты»")
        self.api("POST", "/api/actor/wide/grant", {"kind": "area", "target": "me", "level": "edit"}, who="artem")
        a = acc()
        self.assertEqual(a["trade"], "edit")
        self.assertNotIn("health", a, "доступ к разделу не открывает закрытый проект в нём")
        self.api("POST", "/api/actor/wide/grant", {"kind": "area", "target": "1c", "level": "view"}, who="artem")
        self.assertEqual(acc()["ms"], "view", "к закрытому разделу — своим доступом")
        self.api("POST", "/api/actor/wide/grant", {"kind": "project", "target": "health", "level": "view"}, who="artem")
        self.assertEqual(acc()["health"], "view")
        self.api("POST", "/api/actor/wide/grant", {"kind": "all", "level": None}, who="artem")
        self.assertNotIn("lab", acc())

    def test_a3_view_only(self):
        self.api("POST", "/api/actor", {"id": "reader", "name": "Читатель", "grants": [
            {"kind": "project", "target": "lab", "level": "view"}]}, who="artem")
        status, cookie = self.login_invite(self.api("POST", "/api/actor/reader/invite", {}, who="artem")["token"])
        self.assertEqual(status, 200)
        key = self.k["lab"]
        self.assertEqual(self.web("GET", "/api/task/" + key, cookie=cookie)[0], 200)
        code, body = self.web("PATCH", "/api/task/" + key, {"next": "нельзя"}, cookie)
        self.assertEqual(code, 403)
        self.assertIn("только на чтение", body["error"])
        self.assertEqual(self.web("POST", f"/api/task/{key}/comment", {"text": "нельзя"}, cookie)[0], 403)
        code, st = self.web("GET", "/api/state", cookie=cookie)
        self.assertEqual(st["access"], {"lab": "view"})
        self.assertNotIn("reader", st["projects"][0]["writers"])

    def test_a4_member_model(self):
        self.api("POST", "/api/actor", {"id": "partner", "name": "Партнёр", "grants": [
            {"kind": "project", "target": "eltan", "level": "edit"}]}, who="artem")
        status, cookie = self.login_invite(self.api("POST", "/api/actor/partner/invite", {}, who="artem")["token"])
        code, r = self.web("POST", "/api/actor", {"name": "Codex"}, cookie)
        self.assertEqual(code, 200, r)
        mid = r["id"]
        self.assertEqual(self.web("POST", "/api/actor", {"name": "Человек", "kind": "person"}, cookie)[0], 403)
        code, k = self.web("POST", f"/api/actor/{mid}/key", {"label": "codex-1"}, cookie)
        self.assertEqual(code, 200, k)
        model = {"url": self.url, "token": k["token"]}
        st = cli.request(model, "GET", "/api/state")
        self.assertEqual((st["me"]["owner"], st["access"]), ("partner", {"eltan": "edit"}))
        cli.request(model, "PATCH", "/api/task/" + self.k["eltan"], {"next": "от модели партнёра"})
        code, _ = self.web("POST", f"/api/actor/{mid}/grant", {"kind": "project", "target": "lab", "level": "view"},
                           cookie)
        self.assertEqual(code, 403, "модели нельзя выдать больше, чем у хозяина")
        self.assertEqual(self.web("POST", "/api/actor/artem/grant", {"kind": "all", "level": "edit"}, cookie)[0], 403)
        self.api("POST", "/api/actor/partner/grant", {"kind": "project", "target": "eltan", "level": None}, who="artem")
        self.assertEqual(cli.request(model, "GET", "/api/state")["access"], {},
                         "у хозяина забрали доступ — у модели тоже, хотя её запись осталась")
        self.api("POST", "/api/actor/partner/grant", {"kind": "project", "target": "eltan", "level": "edit"}, who="artem")
        self.api("PATCH", "/api/actor/partner", {"active": False}, who="artem")
        with self.assertRaises(cli.PultError) as ctx:
            cli.request(model, "GET", "/api/me")
        self.assertEqual(ctx.exception.status, 401, "хозяин отключён — ключ модели не действует")
        self.assertEqual(self.web("GET", "/api/state", cookie=cookie)[0], 401, "входы отключённого закрыты")

    def test_a5_owner_guards(self):
        for who in ("artem", "claude"):
            with self.assertRaises(cli.PultError, msg=who) as ctx:
                self.api("PATCH", "/api/actor/artem", {"role": "member"}, who=who)
            self.assertEqual(ctx.exception.status, 400, "последнего владельца-человека не разжаловать")
        for path, why in (("/api/actor/claude/invite", "ссылка для входа — только человеку"),
                          ("/api/actor/artem/key", "постоянный ключ — только модели")):
            with self.assertRaises(cli.PultError) as ctx:
                self.api("POST", path, {}, who="artem")
            self.assertEqual(ctx.exception.status, 400, why)
        self.api("POST", "/api/actor", {"id": "late", "name": "Опоздавший"}, who="artem")
        old = self.api("POST", "/api/actor/late/invite", {"days": 1}, who="artem")
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.execute("UPDATE invites SET expires_at = '2026-01-01T00:00:00+03:00' WHERE id = ?", (old["invite"]["id"],))
            c.commit()
        self.assertEqual(self.login_invite(old["token"])[0], 403, "просроченная ссылка не пускает")
        fresh = self.api("POST", "/api/actor/late/invite", {}, who="artem")
        self.api("POST", f"/api/invite/{fresh['invite']['id']}/revoke", {}, who="artem")
        self.assertEqual(self.login_invite(fresh["token"])[0], 403, "отозванная ссылка не пускает")
        late = next(u for u in self.api("GET", "/api/people")["people"] if u["id"] == "late")
        self.assertEqual({i["id"]: i["state"] for i in late["invites"]},
                         {old["invite"]["id"]: "expired", fresh["invite"]["id"]: "revoked"})

    def test_a6_cli_people(self):
        rc, out = self.run_cli("person", "masha", "--name", "Маша", "--note", "planner")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("grant", "masha", "planner", "edit")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("invite", "masha", "--days", "2")
        self.assertEqual(rc, 0, out)
        with open(out.strip().rsplit(": ", 1)[1], encoding="utf-8") as f:
            token = f.read().strip().split("#i=")[1]
        self.assertNotIn(token, out, "секрет не печатается")
        status, cookie = self.login_invite(token)
        self.assertEqual(status, 200)
        self.assertEqual(self.web("GET", "/api/state", cookie=cookie)[1]["access"], {"planner": "edit"},
                         "закрытый проект — по отдельному доступу")
        rc, out = self.run_cli("person", "masha-bot", "--name", "Бот Маши", "--kind", "model", "--owner", "masha")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("key", "masha-bot", "--label", "masha-bot-1")
        self.assertEqual(rc, 0, out)
        kid = re.search(r"\[([0-9a-f]{12})\]", out).group(1)
        rc, out = self.run_cli("people")
        self.assertIn("Маша (masha) — человек, участник", out)
        self.assertIn("проект «Planner» — правит", out)
        self.assertIn(kid, out)
        rc, out = self.run_cli("revoke", kid)
        self.assertIn("отозван", out)


class Concurrency(Base):
    """Одновременная правка: чужие правки тех же полей не затираются, разные поля сливаются."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.keys = cls.seed_tasks([
            {"project": "lab", "title": "Общая задача", "status": "next", "checklist": ["раз", "два"]},
            {"project": "lab", "title": "Удаляемая", "status": "inbox"},
            {"project": "lab", "title": "Для сессий", "status": "next", "checklist": ["альфа", "бета"]},
        ])

    def sess(self, who, session):
        return {"url": self.url, "token": self.tok[who], "session": session}

    def version(self, key):
        return self.api("GET", f"/api/task/{key}")["task"]["version"]

    def test_c1_merge_and_conflict(self):
        key = self.keys[0]
        v = self.version(key)
        self.api("PATCH", f"/api/task/{key}", {"next": "от Артёма", "version": v}, who="artem")
        r = self.api("PATCH", f"/api/task/{key}", {"status": "doing", "version": v})
        self.assertEqual((r["task"]["status"], r["task"]["next"]), ("doing", "от Артёма"), "разные поля сливаются")
        with self.assertRaises(cli.PultError) as ctx:
            self.api("PATCH", f"/api/task/{key}", {"next": "устаревшее", "version": v})
        e = ctx.exception
        self.assertEqual(e.status, 409)
        self.assertIn("следующий шаг", str(e))
        self.assertEqual(e.payload["task"]["next"], "от Артёма")
        self.assertTrue(any(x["actor"] == "artem" for x in e.payload["conflicts"]))
        self.assertTrue(self.api("PATCH", f"/api/task/{key}", {"next": "от Артёма", "version": v}).get("unchanged"),
                        "то же значение — не конфликт")

    def test_c2_own_session_is_not_foreign(self):
        key = self.keys[0]
        v = self.version(key)
        s1, s2 = self.sess("claude", "s1"), self.sess("claude", "s2")
        cli.request(s1, "PATCH", f"/api/task/{key}", {"wait": "первое", "version": v})
        cli.request(s1, "PATCH", f"/api/task/{key}", {"wait": "второе", "version": v})
        with self.assertRaises(cli.PultError) as ctx:
            cli.request(s2, "PATCH", f"/api/task/{key}", {"wait": "третье", "version": v})
        self.assertEqual(ctx.exception.status, 409, "другая сессия того же участника — чужая правка")
        e = self.api("GET", f"/api/task/{key}")["journal"][0]
        self.assertEqual((e["session"], e["via"]), ("s1", "claude-test"))

    def test_c3_take_race(self):
        key = self.keys[0]
        v = self.version(key)
        cli.request(self.sess("claude", "m1"), "PATCH", f"/api/task/{key}", {"holder": "claude", "version": v})
        with self.assertRaises(cli.PultError) as ctx:
            self.api("PATCH", f"/api/task/{key}", {"holder": "artem", "version": v}, who="artem")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("держит", str(ctx.exception))

    def test_c4_delete_stale(self):
        key = self.keys[1]
        v = self.version(key)
        self.api("PATCH", f"/api/task/{key}", {"next": "передумали"}, who="artem")
        with self.assertRaises(cli.PultError) as ctx:
            self.api("DELETE", f"/api/task/{key}?version={v}", who="artem")
        self.assertEqual(ctx.exception.status, 409)
        self.api("DELETE", f"/api/task/{key}?version={v + 1}", who="artem")

    def test_c5_cli_session_memory(self):
        key, env = self.keys[2], {"PULT_SESSION": "cli-a"}
        rc, out = self.run_cli("show", key, **env)
        self.assertEqual(rc, 0, out)
        self.api("PATCH", f"/api/task/{key}", {"status": "wait", "wait": "ответ"}, who="artem")
        rc, out = self.run_cli("done", key, "--result", "по старому знанию", **env)
        self.assertEqual(rc, 1, out)
        self.assertIn("КОНФЛИКТ", out)
        self.assertIn("Артём", out)
        self.assertEqual(self.api("GET", f"/api/task/{key}")["task"]["status"], "wait")
        rc, out = self.run_cli("set", key, "--next", "дописал шаг", **env)
        self.assertEqual(rc, 0, "другое поле сливается: " + out)
        t = self.api("GET", f"/api/task/{key}")["task"]
        self.api("PATCH", f"/api/task/{key}", {"checklist": [{"t": "новый первый", "on": False}] + t["checklist"]},
                 who="artem")
        rc, out = self.run_cli("step", key, "check", "1", **env)
        self.assertEqual(rc, 1, "шаг по номеру после чужой правки шагов: " + out)
        rc, out = self.run_cli("step", key, "check", "бета", **env)
        self.assertEqual(rc, 0, out)
        self.assertEqual([c["on"] for c in self.api("GET", f"/api/task/{key}")["task"]["checklist"]],
                         [False, False, True])
        self.run_cli("show", key, **env)
        rc, out = self.run_cli("done", key, "--result", "закрыто после show", **env)
        self.assertEqual(rc, 0, "после show знание свежее: " + out)

    def test_c6_apply_from_two_sessions(self):
        key = self.keys[0]
        self.run_cli("brief", PULT_SESSION="pack-a")
        self.run_cli("brief", PULT_SESSION="pack-b")
        ops = os.path.join(self.tmp, "ops_c6.json")
        with open(ops, "w", encoding="utf-8") as f:
            json.dump([{"op": "set", "key": key, "status": "wait", "wait": "сессия A"}], f, ensure_ascii=False)
        rc, out = self.run_cli("apply", ops, PULT_SESSION="pack-a")
        self.assertEqual(rc, 0, out)
        with open(ops, "w", encoding="utf-8") as f:
            json.dump([{"op": "set", "key": key, "status": "doing"}, {"op": "note", "key": key, "text": "сессия B"}],
                      f, ensure_ascii=False)
        rc, out = self.run_cli("apply", ops, PULT_SESSION="pack-b")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"1. {key}: КОНФЛИКТ", out)
        self.assertIn("выполнено 1 из 2", out)
        self.assertEqual(self.api("GET", f"/api/task/{key}")["task"]["status"], "wait")
        rc, out = self.run_cli("brief", PULT_SESSION="pack-b")
        self.assertIn("От других сессий Claude за два дня", out)


# Схема этапа 1 — как в базе до перехода на этап 2
STAGE1_SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
INSERT INTO meta(k, v) VALUES ('schema', '1'), ('rev', '0');
CREATE TABLE actors(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, color TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE keys(
  hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id), label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT);
CREATE TABLE sessions(
  hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id), key_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '');
CREATE TABLE areas(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL UNIQUE, closed INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0, next_num INTEGER NOT NULL DEFAULT 1);
CREATE TABLE projects(
  id TEXT PRIMARY KEY, area_id TEXT NOT NULL REFERENCES areas(id), name TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', mem TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
  closed INTEGER NOT NULL DEFAULT 0, shared TEXT NOT NULL DEFAULT '', paused TEXT,
  archived INTEGER NOT NULL DEFAULT 0, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE tasks(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL, status TEXT NOT NULL, holder TEXT REFERENCES actors(id),
  next_step TEXT NOT NULL DEFAULT '', wait_for TEXT NOT NULL DEFAULT '',
  check_on TEXT, due_on TEXT, done_on TEXT, notes TEXT NOT NULL DEFAULT '',
  mem TEXT NOT NULL DEFAULT '', checklist TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1);
CREATE INDEX tasks_project ON tasks(project_id);
CREATE TABLE journal(
  id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  task_key TEXT, task_title TEXT, project_id TEXT, actor_id TEXT NOT NULL, at TEXT NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
  via TEXT NOT NULL DEFAULT '');
CREATE INDEX journal_task ON journal(task_id, id);
INSERT INTO actors(id, name, kind) VALUES ('artem', 'Артём', 'person'), ('claude', 'Claude', 'model'),
  ('guest', 'guest', 'person');
INSERT INTO keys(hash, actor_id, created_at) VALUES ('a', 'artem', '2026-10-26'), ('b', 'claude', '2026-10-26');
INSERT INTO areas(id, name, prefix, next_num) VALUES ('sr', 'Space Rangers', 'SR', 2);
INSERT INTO projects(id, area_id, name) VALUES ('lab', 'sr', 'sr-lab');
INSERT INTO tasks(key, project_id, title, status, created_at, created_by, updated_at, updated_by, version)
  VALUES ('SR-1', 'lab', 'Старая задача', 'next', '2026-10-26T14:31:00+03:00', 'claude', '2026-10-26T14:32:00+03:00',
          'claude', 2);
INSERT INTO journal(task_id, task_key, task_title, project_id, actor_id, at, kind, data) VALUES
  (1, 'SR-1', 'Старая задача', 'lab', 'claude', '2026-10-26T14:31:00+03:00', 'create', '{}'),
  (1, 'SR-1', 'Старая задача', 'lab', 'claude', '2026-10-26T14:32:00+03:00', 'change',
   '{"changes": ["Статус: Входящие → Следующие"]}');
"""


class Migration(unittest.TestCase):
    def test_stage1_database(self):
        tmp = tempfile.mkdtemp(prefix="pult_mig_")
        try:
            path = os.path.join(tmp, "stage1.db")
            with contextlib.closing(sqlite3.connect(path)) as c:
                c.executescript(STAGE1_SCHEMA)
            db = srv.DB(path)
            self.assertEqual(db.q1("SELECT v FROM meta WHERE k = 'schema'")[0], str(srv.SCHEMA_VERSION))
            self.assertEqual({r["id"]: r["role"] for r in db.q("SELECT id, role FROM actors")},
                             {"artem": "owner", "claude": "owner", "guest": "member"},
                             "кто мог войти на этапе 1, тот остался владельцем")
            st = db.state(db.actor("guest"))
            self.assertEqual((st["projects"], st["tasks"], st["areas"]), ([], [], []))
            claude = dict(db.actor("claude"), via="проверка", session="")
            self.assertEqual(len(db.state(claude)["tasks"]), 1)
            with self.assertRaises(srv.ApiError) as ctx:
                db.update_task(claude, "SR-1", {"next": "по версии 1", "version": 1})
            self.assertEqual(ctx.exception.status, 409, "правка до этапа 2 без списка полей — считаем, что задето всё")
            db.update_task(claude, "SR-1", {"next": "по версии 2", "version": 2})
            db.conn.close()
            srv.DB(path).conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stage2_database(self):
        """Переход 2 → 3 не повторяет шаг этапа 1: модель участника с ключом не становится владельцем."""
        tmp = tempfile.mkdtemp(prefix="pult_mig_")
        try:
            path = os.path.join(tmp, "stage2.db")
            db = srv.DB(path)
            with db.tx() as c:
                c.execute("DROP TABLE files")
                c.execute("UPDATE meta SET v = '2' WHERE k = 'schema'")
                c.execute("INSERT INTO actors(id, name, kind, role) VALUES ('artem', 'Артём', 'person', 'owner'), "
                          "('codex', 'Codex', 'model', 'member')")
                c.execute("INSERT INTO keys(hash, actor_id, created_at) VALUES ('k1', 'codex', '2026-10-26')")
            db.conn.close()
            db = srv.DB(path)
            self.assertEqual(db.q1("SELECT v FROM meta WHERE k = 'schema'")[0], "3")
            self.assertEqual(db.actor("codex")["role"], "member")
            self.assertEqual(db.q1("SELECT COUNT(*) FROM files")[0], 0, "таблица вложений заведена")
            db.conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(200))


class Files(Base):
    """Вложения этапа 3: приём, выдача, пределы, права, удаление и уборка, сверка версий, pult.py."""
    serve_args = ("--file-max-mb", "0.05", "--files-quota-mb", "0.15")  # 51 КБ на файл, 153 КБ всего

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files_dir = os.path.join(cls.tmp, "files")
        cls.keys = cls.seed_tasks([
            {"project": "lab", "title": "С картинкой", "status": "next"},
            {"project": "lab", "title": "Для пределов", "status": "next"},
            {"project": "eltan", "title": "Общая с гостем", "status": "next"},
            {"project": "lab", "title": "Для версий", "status": "next"},
            {"project": "lab", "title": "Для pult.py", "status": "next"},
        ])

    def up(self, key, data, name="файл.bin", who="claude", headers=None):
        h = {"Authorization": "Bearer " + self.tok[who], "X-File-Name": urllib.parse.quote(name),
             "Content-Type": "application/octet-stream"}
        h.update(headers or {})
        r, body = self.raw("POST", f"/api/task/{key}/files", data, headers=h)
        return r.status, (json.loads(body) if body else {})

    def get_file(self, fid, query="", who="claude", method="GET", headers=None):
        h = {"Authorization": "Bearer " + self.tok[who]}
        h.update(headers or {})
        return self.raw(method, f"/api/file/{fid}{query}", headers=h)

    def blobs(self):
        return sorted(n for _b, _d, names in os.walk(self.files_dir) for n in names if len(n) == 64)

    def test_f1_upload_and_card(self):
        key = self.keys[0]
        v = self.api("GET", f"/api/task/{key}")["task"]["version"]
        code, r = self.up(key, PNG, "Скриншот ошибки.png")
        self.assertEqual(code, 201, r)
        self.assertEqual((r["file"]["name"], r["file"]["size"], r["file"]["image"]), ("Скриншот ошибки.png", len(PNG), True))
        self.assertEqual((r["task"]["files"], r["task"]["version"]), (1, v + 1), "вложение — новая версия карточки")
        card = self.api("GET", f"/api/task/{key}")
        self.assertEqual([f["name"] for f in card["files"]], ["Скриншот ошибки.png"])
        self.assertEqual(card["journal"][0]["kind"], "file")
        self.assertIn("Вложение: Скриншот ошибки.png", card["journal"][0]["changes"][0])
        st = self.api("GET", "/api/state")
        self.assertEqual(next(t for t in st["tasks"] if t["key"] == key)["files"], 1)
        self.assertEqual(st["limits"]["file"], int(0.05 * (1 << 20)))

    def test_f2_download_headers(self):
        key = self.keys[0]
        _, r = self.up(key, PNG, "картинка.png")
        fid = r["file"]["id"]
        resp, body = self.get_file(fid)
        self.assertEqual((resp.status, body), (200, PNG))
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        self.assertTrue(resp.getheader("Content-Disposition").startswith("inline;"))
        self.assertIn("filename*=UTF-8''" + urllib.parse.quote("картинка.png"), resp.getheader("Content-Disposition"))
        self.assertIn("sandbox", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        resp, body = self.get_file(fid, "?dl=1")
        self.assertEqual(resp.getheader("Content-Type"), "application/octet-stream")
        self.assertTrue(resp.getheader("Content-Disposition").startswith("attachment;"))
        resp, _ = self.get_file(fid, headers={"If-None-Match": resp.getheader("ETag")})
        self.assertEqual(resp.status, 304)
        resp, body = self.get_file(fid, method="HEAD")
        self.assertEqual((resp.status, body, resp.getheader("Content-Length")), (200, b"", str(len(PNG))))
        # HTML с картинкой в имени — всё равно только на скачивание: на странице показываем лишь узнанные картинки
        _, r = self.up(key, b"<html><script>alert(1)</script></html>", "x.png")
        resp, _ = self.get_file(r["file"]["id"])
        self.assertEqual(resp.getheader("Content-Type"), "application/octet-stream")
        self.assertTrue(resp.getheader("Content-Disposition").startswith("attachment;"))
        self.assertFalse(r["file"]["image"])

    def test_f3_limits_and_names(self):
        key = self.keys[1]
        code, r = self.up(key, b"x" * (60 << 10))
        self.assertEqual(code, 413, r)
        self.assertEqual(self.up(key, b"")[0], 400, "пустой файл")
        code, r = self.up(key, b"text", "..\\..\\etc/passwd")
        self.assertEqual((code, r["file"]["name"]), (201, "passwd"), "имя без пути")
        code, r = self.up(key, b"text2", "")
        self.assertEqual(r["file"]["name"], "файл")
        sizes = []
        for i in range(10):
            code, r = self.up(key, secrets.token_bytes(40 << 10), f"big{i}.bin")
            sizes.append(code)
            if code != 201:
                break
        self.assertEqual(sizes[-1], 507, f"место кончилось: {sizes}")
        self.assertIn("место для вложений кончилось", r["error"])

    def test_f4_access_and_csrf(self):
        key = self.keys[2]
        self.api("POST", "/api/actor", {"id": "fguest", "name": "Гость вложений", "grants": [
            {"kind": "project", "target": "eltan", "level": "view"}]}, who="artem")
        status, cookie = self.login_invite(self.api("POST", "/api/actor/fguest/invite", {}, who="artem")["token"])
        self.assertEqual(status, 200)
        _, r = self.up(key, PNG, "общая.png")
        fid = r["file"]["id"]
        web = {"Cookie": cookie, "X-Pult": "1", "Origin": self.url}
        resp, body = self.raw("GET", f"/api/file/{fid}", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 200, "читает — может смотреть вложения")
        resp, _ = self.raw("POST", f"/api/task/{key}/files", b"data", headers=dict(web, **{"X-File-Name": "a.txt"}))
        self.assertEqual(resp.status, 403, "читает — не прикрепляет")
        resp, _ = self.raw("DELETE", f"/api/file/{fid}", headers=web)
        self.assertEqual(resp.status, 403, "читает — не удаляет")
        lab_file = self.up(self.keys[0], b"lab", "lab.txt")[1]["file"]["id"]
        resp, _ = self.raw("GET", f"/api/file/{lab_file}", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 404, "вложение чужого проекта — как несуществующее")
        resp, _ = self.raw("POST", f"/api/task/{self.keys[0]}/files", b"data", headers=dict(web, **{"X-File-Name": "a"}))
        self.assertEqual(resp.status, 404)
        self.api("POST", "/api/actor/fguest/grant", {"kind": "project", "target": "eltan", "level": "edit"}, who="artem")
        resp, _ = self.raw("POST", f"/api/task/{key}/files", b"data", headers={"Cookie": cookie, "X-File-Name": "a"})
        self.assertEqual(resp.status, 403, "по куки — только со своей страницы")
        resp, body = self.raw("POST", f"/api/task/{key}/files", b"data", headers=dict(web, **{"X-File-Name": "a"}))
        self.assertEqual(resp.status, 201, body)

    def test_f5_delete_dedupe_gc(self):
        key = self.keys[0]
        data = b"same content " * 100
        a = self.up(key, data, "один.txt")[1]["file"]["id"]
        b = self.up(key, data, "второй.txt")[1]["file"]["id"]
        sha = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.blobs().count(sha), 1, "одинаковое содержимое хранится один раз")
        r = self.api("DELETE", f"/api/file/{a}")
        self.assertTrue(r["ok"])
        self.assertEqual(self.get_file(a)[0].status, 404)
        self.assertEqual(self.get_file(b)[0].status, 200, "второе вложение с тем же содержимым цело")
        j = self.api("GET", f"/api/task/{key}")["journal"][0]
        self.assertIn("Вложение удалено: один.txt", j["changes"][0])
        db = srv.DB(self.db)
        try:
            srv.gc_files(db, self.files_dir, keep_days=-1)  # граница в будущем: годится всё удалённое
            self.assertIn(sha, self.blobs(), "пока есть живое вложение — файл на месте")
            self.api("DELETE", f"/api/file/{b}")
            srv.gc_files(db, self.files_dir, keep_days=1)
            self.assertIn(sha, self.blobs(), "удалённое живёт на диске keep_days")
            srv.gc_files(db, self.files_dir, keep_days=-1)
            self.assertNotIn(sha, self.blobs())
        finally:
            db.conn.close()

    def test_f6_versions(self):
        key = self.keys[3]
        v = self.api("GET", f"/api/task/{key}")["task"]["version"]
        self.up(key, b"report", "отчёт.txt", who="artem")
        r = self.api("PATCH", f"/api/task/{key}", {"next": "по старой версии", "version": v})
        self.assertEqual(r["task"]["next"], "по старой версии", "вложение не мешает правке полей")
        with self.assertRaises(cli.PultError) as ctx:
            self.api("DELETE", f"/api/task/{key}?version={v}", who="artem")
        self.assertEqual(ctx.exception.status, 409, "удаление по версии до вложения — конфликт")
        self.assertIn("вложения", str(ctx.exception))

    def test_f7_cli(self):
        key = self.keys[4]
        src = os.path.join(self.tmp, "заметка.txt")
        with open(src, "wb") as f:
            f.write("строка лога\n".encode("cp1251"))
        rc, out = self.run_cli("attach", key, src, "-m", "приложил лог")
        self.assertEqual(rc, 0, out)
        fid = re.search(r"вложение #(\d+)", out).group(1)
        rc, out = self.run_cli("show", key)
        self.assertIn(f"#{fid} заметка.txt", out)
        dst = os.path.join(self.tmp, "fetched")
        os.makedirs(dst)
        rc, out = self.run_cli("fetch", fid, "-o", dst)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(dst, "заметка.txt"), "rb") as f:
            self.assertEqual(f.read(), "строка лога\n".encode("cp1251"))
        big = os.path.join(self.tmp, "big.bin")
        with open(big, "wb") as f:
            f.write(b"0" * (60 << 10))
        rc, out = self.run_cli("attach", key, big)
        self.assertEqual(rc, 2, out)
        self.assertIn("больше предела", out)
        ops = os.path.join(self.tmp, "ops_attach.json")
        with open(ops, "w", encoding="utf-8") as f:
            json.dump([{"op": "attach", "key": key, "file": src, "name": "из пакета.txt", "comment": "из пакета"}],
                      f, ensure_ascii=False)
        rc, out = self.run_cli("apply", ops)
        self.assertEqual(rc, 0, out)
        self.assertIn("из пакета.txt", out)
        rc, out = self.run_cli("detach", fid)
        self.assertEqual(rc, 0, out)
        self.assertEqual([f["name"] for f in self.api("GET", f"/api/task/{key}")["files"]], ["из пакета.txt"])


class FakeTelegram(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeTelegram.got.append((self.path, urllib.parse.parse_qs(body.decode("utf-8"))))
        out = b'{"ok": true, "result": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class Digest(Base):
    """Утренняя сводка: разделы, видимость, отправка в Telegram раз в день."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import datetime as dt
        today = dt.date.today()
        day = lambda n: (today + dt.timedelta(days=n)).isoformat()  # noqa: E731
        cls.keys = cls.seed_tasks([
            {"project": "lab", "title": "Просроченная <b>&", "status": "doing", "holder": "artem", "due": day(-2)},
            {"project": "lab", "title": "Ждём ответа", "status": "wait", "holder": "claude", "wait": "ответ guest",
             "check": day(0)},
            {"project": "trade", "title": "Срок скоро", "status": "next", "due": day(3)},
            {"project": "lab", "title": "Моя в работе", "status": "doing", "holder": "claude", "next": "дописать"},
            {"project": "health", "title": "Закрытый проект", "status": "next", "holder": "artem", "due": day(-1)},
            {"project": "lab", "title": "Закрыта вчера", "status": "done", "done_on": day(-1)},
        ])

    def test_d1_sections(self):
        d = self.api("GET", "/api/digest")
        text, html_ = d["text"], d["html"]
        self.assertTrue(text.startswith("Пульт · "))
        for part in ("🔴 Просрочено · 2", "🟡 Пора проверить · 1", "ждём: ответ guest · вы", "📅 Сроки на неделе · 1",
                     "🔵 В работе у вас · 1", "Моя в работе — дописать", "Закрыто 1: " + self.keys[5]):
            self.assertIn(part, text)
        self.assertIn("Просроченная &lt;b&gt;&amp;", html_, "название экранировано для Telegram")
        self.assertIn(f'<a href="{self.url}/#t={self.keys[0]}">{self.keys[0]}</a>', html_)
        self.assertNotIn("<b>&", html_)
        self.assertEqual(d["counts"]["late"], 2)

    def test_d2_member_sees_own_projects(self):
        self.api("POST", "/api/actor", {"id": "dguest", "name": "Гость сводки", "grants": [
            {"kind": "project", "target": "trade", "level": "edit"}]}, who="artem")
        db = srv.DB(self.db)
        try:
            d = db.digest("dguest", "https://x")
        finally:
            db.conn.close()
        self.assertIn("Срок скоро", d["text"])
        self.assertNotIn("Просроченная", d["text"])
        self.assertNotIn("Закрытый проект", d["text"])

    def test_d3_send_once_a_day(self):
        tg = http.server.HTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=tg.serve_forever, daemon=True).start()
        token_file = os.path.join(self.tmp, "tg.env")
        with open(token_file, "w", encoding="utf-8") as f:
            f.write("TELEGRAM_BOT_TOKEN=123456:" + "A" * 30 + "\n")
        base = [sys.executable, "-X", "utf8", SERVER]

        def run(*argv):
            return subprocess.run(base + list(argv), capture_output=True, text=True, encoding="utf-8")
        try:
            out = run("digest", "--db", self.db, "--url", self.url, "--send", "--token-file", token_file)
            self.assertIn("сводку слать некому", out.stdout)
            self.assertEqual(run("admin", "--db", self.db, "digest-to", "artem", "1234567").returncode, 0)
            self.assertIn("…567", run("admin", "--db", self.db, "digest-list").stdout)
            api = f"http://127.0.0.1:{tg.server_port}"
            send = ("digest", "--db", self.db, "--url", self.url, "--send", "--token-file", token_file, "--api", api)
            out = run(*send)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(len(FakeTelegram.got), 1)
            path, form = FakeTelegram.got[0]
            self.assertEqual(path, "/bot123456:" + "A" * 30 + "/sendMessage")
            self.assertEqual((form["chat_id"], form["parse_mode"]), (["1234567"], ["HTML"]))
            self.assertIn("<b>Пульт · ", form["text"][0])
            self.assertIn("сегодня уже отправлено", run(*send).stdout)
            self.assertEqual(len(FakeTelegram.got), 1, "второй раз за день не шлём")
            self.assertEqual(run(*send, "--force").returncode, 0)
            self.assertEqual(len(FakeTelegram.got), 2)
            self.assertNotIn("A" * 30, out.stdout + out.stderr, "токен не печатается")
        finally:
            tg.shutdown()
            tg.server_close()


@unittest.skipUnless(shutil.which("openssl"), "нет openssl")
class Tls(Base):
    tls = True

    @classmethod
    def make_cert(cls):
        cls.cert, cls.key = os.path.join(cls.tmp, "cert.pem"), os.path.join(cls.tmp, "key.pem")
        out = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                              "-nodes", "-keyout", cls.key, "-out", cls.cert, "-days", "2", "-subj", "/CN=127.0.0.1",
                              "-addext", "subjectAltName=IP:127.0.0.1"],
                             capture_output=True, text=True, env=dict(os.environ, MSYS_NO_PATHCONV="1"))
        assert out.returncode == 0, out.stderr

    def test_tls_and_slow_client(self):
        idle = socket.create_connection(("127.0.0.1", self.port))  # подключился и молчит, рукопожатия нет
        try:
            t0 = time.time()
            r, body = self.raw("GET", "/api/me", headers={"Authorization": "Bearer " + self.tok["claude"]})
            self.assertEqual(r.status, 200, body)
            self.assertLess(time.time() - t0, 3, "молчащий клиент не должен держать остальных")
        finally:
            idle.close()
        r, _ = self.raw("POST", "/api/login", {"key": self.tok["artem"]})
        self.assertIn("Secure", r.getheader("Set-Cookie"))


class Proxy(Base):
    """За обратным прокси: куки Secure, хотя сам сервер без TLS; пауза после неудачных входов — по адресу клиента."""
    serve_args = ("--proxy",)

    def test_x1_secure_cookie_and_client_address(self):
        r, _ = self.raw("POST", "/api/login", {"key": self.tok["artem"]})
        self.assertEqual(r.status, 200)
        self.assertIn("Secure", r.getheader("Set-Cookie"))
        bad, there = {"key": "x" * 43}, {"X-Forwarded-For": "203.0.113.7"}
        for _ in range(10):
            self.assertEqual(self.raw("POST", "/api/login", bad, headers=there)[0].status, 403)
        self.assertEqual(self.raw("POST", "/api/login", bad, headers=there)[0].status, 429)
        r, _ = self.raw("POST", "/api/login", {"key": self.tok["artem"]},
                        headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.1"})
        self.assertEqual(r.status, 200, "пауза держит только тот адрес, что дописал прокси")


class FreshInstall(unittest.TestCase):
    """Новый Пульт с нуля: init, вход по ссылке, ключ ИИ, connect с другого места, раздел через apply."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pult_init_")
        cls.env = unittest.mock.patch.dict(os.environ, {
            "PULT_SESSION": "", "PULT_SEEN_DIR": os.path.join(cls.tmp, "seen"),
            "PULT_SECRETS_DIR": os.path.join(cls.tmp, "secrets"), "PULT_URL": "", "PULT_TOKEN": ""})
        cls.env.start()
        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.db = os.path.join(cls.tmp, "data", "pult.db")
        cls.out = os.path.join(cls.tmp, "out")
        cls.init = cls.run_init("--db", cls.db, "--name", "Аня", "--url", cls.url, "--out", cls.out)
        cls.proc = subprocess.Popen([sys.executable, "-X", "utf8", SERVER, "serve", "--db", cls.db, "--static", STATIC,
                                     "--port", str(cls.port), "--plain-http"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        wait_port(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.proc.stderr.close()
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def run_init(*args):
        return subprocess.run([sys.executable, "-X", "utf8", SERVER, "init", *args],
                              capture_output=True, text=True, encoding="utf-8")

    def cli(self, *argv, config):
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"PULT_CONFIG": config}), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read().strip()

    def test_i1_init_files_and_no_secrets_in_output(self):
        self.assertEqual(self.init.returncode, 0, self.init.stderr)
        login, token = self.read("login-anya.txt"), self.read("claude.token")
        self.assertTrue(login.startswith(self.url + "/login#i="))
        self.assertNotIn(login.split("#i=")[1], self.init.stdout + self.init.stderr, "ссылка входа не в выводе")
        self.assertNotIn(token, self.init.stdout + self.init.stderr, "ключ ИИ не в выводе")
        with open(os.path.join(self.out, "config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"url": self.url, "token_file": "claude.token"})
        again = self.run_init("--db", self.db, "--name", "Аня", "--out", os.path.join(self.tmp, "out2"))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("уже есть участники", again.stderr)

    def test_i2_ai_works_and_holds_first_card(self):
        config = os.path.join(self.out, "config.json")
        rc, out = self.cli("whoami", config=config)
        self.assertEqual(rc, 0, out)
        self.assertIn("вы — Claude (claude), владелец Пульта", out)
        rc, out = self.cli("brief", config=config)
        self.assertIn("P-1", out)
        self.assertIn("Подключить ИИ к Пульту", out)
        task = cli.request({"url": self.url, "token": self.read("claude.token")}, "GET", "/api/task/P-1")["task"]
        self.assertEqual((task["holder"], task["status"], len(task["checklist"])), ("claude", "next", 4))

    def test_i3_owner_logs_in_once(self):
        token = self.read("login-anya.txt").split("#i=")[1]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", json.dumps({"invite": token}), {"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        self.assertIn("pult_s=", r.getheader("Set-Cookie") or "")
        conn.request("POST", "/api/login", json.dumps({"invite": token}), {"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 403, "ссылка первого входа — одноразовая")

    def test_i4_connect_from_elsewhere(self):
        key_file = os.path.join(self.tmp, "key-from-owner.txt")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(self.read("claude.token") + "\n")
        config = os.path.join(self.tmp, "pc", "config.json")
        rc, out = self.cli("connect", self.url, "--token-file", key_file, "--label", "claude-pc", config=config)
        self.assertEqual(rc, 0, out)
        self.assertIn("вы — Claude (claude)", out)
        self.assertNotIn(self.read("claude.token"), out)
        with open(config, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"url": self.url, "token_file": "claude-pc.token"})
        self.assertIn("вы — Claude", self.cli("whoami", config=config)[1])
        with open(key_file, "w", encoding="utf-8") as f:
            f.write("x" * 43 + "\n")
        with self.assertRaises(SystemExit) as e:
            self.cli("connect", self.url, "--token-file", key_file, config=os.path.join(self.tmp, "bad", "c.json"))
        self.assertIn("не принял ключ", str(e.exception))

    def test_i5_ai_creates_area_and_project(self):
        ops = os.path.join(self.tmp, "ops.json")
        with open(ops, "w", encoding="utf-8") as f:
            json.dump([{"op": "area", "id": "work", "name": "Работа", "prefix": "W"},
                       {"op": "project", "id": "site", "area": "work", "name": "Сайт"},
                       {"op": "add", "project": "site", "title": "Первая задача", "holder": "me"},
                       {"op": "area", "id": "work", "name": "Работа и учёба"}], f, ensure_ascii=False)
        rc, out = self.cli("apply", ops, config=os.path.join(self.out, "config.json"))
        self.assertEqual(rc, 0, out)
        for part in ("1. раздел work: создан", "2. проект site: создан", "W-1", "4. раздел work: изменён"):
            self.assertIn(part, out)
        rc, out = self.cli("projects", config=os.path.join(self.out, "config.json"))
        self.assertIn("Работа и учёба [work, номера W-…]", out)

    def test_i6_init_without_ai(self):
        out = os.path.join(self.tmp, "solo")
        r = self.run_init("--db", os.path.join(self.tmp, "solo.db"), "--name", "Solo", "--no-ai", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["login-solo.txt", "login-solo.url"])
        self.assertIn("P-1", r.stdout)


if __name__ == "__main__":
    unittest.main()
