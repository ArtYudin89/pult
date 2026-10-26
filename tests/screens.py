# -*- coding: utf-8 -*-
"""Снимки страницы для README на выдуманных данных: новый Пульт через init, проекты и задачи — от лица ИИ
(Claude и Codex, у каждого своя сессия), потом страница в настоящем Chrome глазами владельца.

    python -X utf8 tests/screens.py docs/img

Нужны Playwright и Chrome (channel="chrome"). Даты считаются от сегодняшнего дня, чтобы на доске были
«просрочено» и «пора проверить». В конце печатается сводка pult.py brief — пример для README.
"""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest.mock

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pult as cli  # noqa: E402

out = os.path.abspath(sys.argv[1])
os.makedirs(out, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="pult_screens_")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
base = f"http://127.0.0.1:{port}"
server = os.path.join(ROOT, "server", "pult_server.py")
db = os.path.join(tmp, "demo.db")
keys = os.path.join(tmp, "out")
subprocess.run([sys.executable, "-X", "utf8", server, "init", "--db", db, "--name", "Аня", "--url", base,
                "--out", keys], check=True, stdout=subprocess.DEVNULL)
proc = subprocess.Popen([sys.executable, "-X", "utf8", server, "serve", "--db", db, "--port", str(port), "--plain-http"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
for _ in range(100):
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
        break
    time.sleep(0.1)

today = dt.date.today()
day = lambda n: (today + dt.timedelta(days=n)).isoformat()  # noqa: E731
env = {"PULT_CONFIG": os.path.join(keys, "config.json"), "PULT_SEEN_DIR": os.path.join(tmp, "seen"),
       "PULT_SECRETS_DIR": os.path.join(tmp, "secrets"), "PULT_URL": "", "PULT_TOKEN": ""}


def run(*argv, session, token=None):
    buf = io.StringIO()
    extra = {"PULT_SESSION": session}
    if token:
        extra["PULT_TOKEN"] = token
    with unittest.mock.patch.dict(os.environ, dict(env, **extra)), contextlib.redirect_stdout(buf), \
            contextlib.redirect_stderr(buf):
        rc = cli.main(list(argv))
    if rc:
        raise SystemExit(f"pult.py {' '.join(argv)}: {buf.getvalue()}")
    return buf.getvalue()


def apply(ops, session, token=None):
    path = os.path.join(tmp, f"ops-{time.time_ns()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ops, f, ensure_ascii=False)
    return run("apply", path, session=session, token=token)


try:
    claude = "3f2a1b9c"
    apply([
        {"op": "area", "id": "work", "name": "Работа", "prefix": "W", "sort": 1},
        {"op": "area", "id": "home", "name": "Дом", "prefix": "H", "closed": True, "sort": 2},
        {"op": "project", "id": "site", "area": "work", "name": "Сайт клиники", "note": "заявки, хостинг"},
        {"op": "project", "id": "crm", "area": "work", "name": "Интеграция с CRM"},
        {"op": "project", "id": "notes", "area": "main", "name": "Бот для заметок"},
        {"op": "project", "id": "repair", "area": "home", "name": "Ремонт"},
    ], claude)
    run("person", "codex", "--name", "Codex", "--kind", "model", "--owner", "anya", session=claude)
    run("grant", "codex", "area:work", "edit", session=claude)
    run("key", "codex", "--label", "codex-1", session=claude)
    with open(os.path.join(tmp, "secrets", "codex-1.token"), encoding="utf-8") as f:
        codex_key = f.read().strip()
    run("person", "misha", "--name", "Миша", "--note", "верстальщик", session=claude)
    run("grant", "misha", "site", "edit", session=claude)
    apply([
        {"op": "add", "project": "site", "title": "Форма записи на приём", "status": "doing", "holder": "me",
         "next": "проверить отправку заявок в Telegram",
         "notes": "Клиника хочет получать заявки сразу в рабочий чат. Форма — на странице «Врачи».\n\n"
                  "План: поля формы и подсказки, проверка телефона, отправка в Telegram, письмо-подтверждение.",
         "comment": "завёл по разговору с Аней"},
        {"op": "add", "project": "site", "title": "Перенести сайт на новый хостинг", "status": "wait", "holder": "anya",
         "wait": "доступ к панели хостинга от администратора клиники", "check": day(0)},
        {"op": "add", "project": "site", "title": "Капча на форме записи", "status": "next", "holder": "misha",
         "next": "выбрать: своя или от Яндекса"},
        {"op": "add", "project": "crm", "title": "Синхронизация контактов с CRM", "status": "next",
         "next": "сопоставить поля карточки клиента"},
        {"op": "add", "project": "crm", "title": "Отчёт по оплатам за месяц", "status": "inbox"},
        {"op": "add", "project": "notes", "title": "Экспорт заметок в Markdown", "status": "next", "holder": "me",
         "next": "вложения — отдельными файлами рядом с заметкой", "due": day(5)},
        {"op": "add", "project": "notes", "title": "Голосовые заметки → текст", "status": "inbox"},
        {"op": "add", "project": "pult", "title": "Продлить домен", "status": "next", "holder": "anya",
         "next": "оплатить у регистратора", "due": day(-1)},
        {"op": "add", "project": "repair", "title": "Выбрать плитку в ванную", "status": "wait", "holder": "anya",
         "wait": "образцы из магазина", "check": day(3)},
    ], claude)
    apply([
        {"op": "note", "key": "W-1", "text": "Маска телефона готова; бот для чата клиники заведён, жду токен от Ани"},
        {"op": "set", "key": "P-1", "prompt": "-", "comment": "проекты и задачи заведены"},
        {"op": "done", "key": "P-1", "result": "читаю сводку в начале сессии и двигаю карточки в конце"},
    ], claude)
    codex = "b71e04d2"
    apply([
        {"op": "set", "key": "W-4", "status": "doing", "holder": "me", "next": "сопоставить поля карточки клиента",
         "comment": "беру себе"},
    ], codex, token=codex_key)
    # Аня ответила в чате — Claude отмечает это в следующей сессии
    apply([
        {"op": "set", "key": "W-1", "next": "проверить отправку заявок в рабочий чат клиники", "check": day(1),
         "comment": "токен бота получен"},
    ], "c90d55e1")

    with open(os.path.join(keys, "login-anya.txt"), encoding="utf-8") as f:
        login = f.read().strip()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 860}, locale="ru-RU", color_scheme="light",
                                  device_scale_factor=1)
        page = ctx.new_page()
        page.goto(login)
        page.wait_for_url(base + "/#all")
        page.wait_for_selector(".trow")
        # Аня пишет поручение ИИ прямо в карточке — со страницы, под своим входом
        r = ctx.request.patch(base + "/api/task/W-1", headers={"X-Pult": "1"}, data={
            "prompt": "Токен бота — в чате клиники. Проверь, что заявки доходят, и напиши в журнал, если что-то не так."})
        assert r.ok, r.text()
        page.reload()
        page.wait_for_selector(".trow")
        page.goto(base + "/#board")
        page.wait_for_selector(".bcol")
        time.sleep(0.5)
        page.screenshot(path=os.path.join(out, "board.png"))
        page.goto(base + "/#p=site&t=W-1")
        page.wait_for_selector(".task h2")
        time.sleep(0.5)
        # карточка целиком, с журналом: окно по высоте содержимого, снимок — только панель карточки
        height = page.locator("#drawer").evaluate("e => e.scrollHeight")
        page.set_viewport_size({"width": 1440, "height": height})
        time.sleep(0.3)
        box = page.locator("#drawer").bounding_box()
        page.screenshot(path=os.path.join(out, "card.png"), clip=box)
        browser.close()
    print(run("brief", session=claude))
finally:
    proc.terminate()
    proc.wait(10)
    proc.stderr.close()
    shutil.rmtree(tmp, ignore_errors=True)
