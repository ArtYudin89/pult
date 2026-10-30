# -*- coding: utf-8 -*-
"""Страница на копии боевой базы в настоящем Chrome (Playwright, channel="chrome"): доска — перенос мышью и
пальцем, фильтры, поиск; вложения — выбор файла, Ctrl+V, перетаскивание на карточку, удаление; телефон 400 px;
тёмная тема. Снимки — в <папку>: смотреть глазами, строки OK про вид ничего не говорят.

    python -X utf8 tests/ui_check.py <копия базы.db> server/pult_server.py server/static <папка для снимков>

Свежая копия базы — как в docs/install.md («Обновление»). Ждать через wait_for_url/wait_for_selector:
wait_for_function со строкой страница не пропустит — в её CSP нет 'unsafe-eval'.
"""
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib

from playwright.sync_api import sync_playwright


def pick_actor(db_path, prefer):
    """Кого проверять: PULT_CHECK_ACTOR или первый владелец в копии базы (prefer — model или person)."""
    if os.environ.get("PULT_CHECK_ACTOR"):
        return os.environ["PULT_CHECK_ACTOR"]
    with contextlib.closing(sqlite3.connect(db_path)) as c:
        return c.execute("SELECT id FROM actors WHERE role = 'owner' ORDER BY kind = ? DESC, id",
                         (prefer,)).fetchone()[0]

src, server, static, out = sys.argv[1:5]
os.makedirs(out, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="pult_ui3_")
db = os.path.join(tmp, "ui.db")
shutil.copyfile(src, db)
KEY = "ui3-owner-" + "w" * 40
with contextlib.closing(sqlite3.connect(db)) as c:
    c.execute("INSERT INTO keys(hash, actor_id, label, created_at) VALUES (?, ?, 'ui-test', '2026-10-26T00:00:00+03:00')",
              (hashlib.sha256(KEY.encode()).hexdigest(), pick_actor(db, "person")))
    c.commit()
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
base = f"http://127.0.0.1:{port}"
proc = subprocess.Popen([sys.executable, "-X", "utf8", server, "serve", "--db", db, "--static", static,
                         "--host", "127.0.0.1", "--port", str(port), "--plain-http"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
for _ in range(100):
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
        break
    time.sleep(0.1)


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG_PATH = os.path.join(tmp, "Схема доски.png")
with open(PNG_PATH, "wb") as f:
    f.write(png(320, 200, (46, 78, 208)))
TXT_PATH = os.path.join(tmp, "crash.log")
with open(TXT_PATH, "wb") as f:
    f.write("Ошибка доступа\r\n".encode("cp1251"))


def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
        return json.loads(r.read().decode())


notes, errors = [], []


def check(cond, what):
    notes.append(("OK  " if cond else "FAIL") + " " + what)
    return cond


def watch(page, who):
    page.on("console", lambda m: errors.append(f"{who} console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"{who} pageerror: {e}"))


def task(key):
    return call("GET", "/api/task/" + key)["task"]


def center(box):
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU")
        page = ctx.new_page()
        watch(page, "desk")
        page.goto(base + "/login#k=" + KEY)
        page.wait_for_url(base + "/#all")
        page.wait_for_selector(".trow")

        # ---------- доска ----------
        page.click('[data-nav="board"]')
        page.wait_for_selector(".bcol")
        check(page.url.endswith("#board"), f"пункт «Доска» слева ведёт на #board ({page.url[-12:]})")
        check(page.locator(".bcol").count() == 5, "пять колонок")
        check("wide" in (page.get_attribute("#main", "class") or ""), "доска во всю ширину")
        st = call("GET", "/api/state")
        open_by = {k: sum(1 for t in st["tasks"] if t["status"] == k) for k in ("inbox", "next", "doing", "wait")}
        shown = {k: page.locator(f'.bcol[data-col="{k}"] .bcard').count() for k in open_by}
        check(shown == open_by, f"в колонках все открытые задачи: {shown}")
        page.screenshot(path=os.path.join(out, "01_board.png"))

        # мышью: первая карточка «Входящих» → «В работе»
        card = page.locator('.bcol[data-col="inbox"] .bcard').first
        key = card.get_attribute("data-task")
        x0, y0 = center(card.bounding_box())
        x1, y1 = center(page.locator('.bcol[data-col="doing"] .bhead').bounding_box())
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x0 + 20, y0 + 5, steps=3)
        check(page.locator(".bghost").count() == 1, "мышь: карточка поднялась после сдвига")
        page.mouse.move(x1, y1 + 40, steps=12)
        check(page.locator('.bcol[data-col="doing"].over').count() == 1, "мышь: колонка под курсором подсвечена")
        page.screenshot(path=os.path.join(out, "02_dragging.png"))
        page.mouse.up()
        page.wait_for_selector(".toast:not([hidden])")
        check(task(key)["status"] == "doing", f"мышь: {key} перенесена в «В работе»")
        check(page.locator('.bcol[data-col="doing"] .bcard[data-task="' + key + '"]').count() == 1, "мышь: карточка в новой колонке")
        check(page.locator("#drawer").is_hidden(), "после переноса карточка задачи не открылась")
        j = call("GET", "/api/task/" + key)["journal"][0]
        check("Статус: Входящие → В работе" in " ".join(j["changes"]), "журнал: смена статуса записана")

        # в «Ждёт» без записанного ожидания → форма с полем «Ждём»
        nxt = [t for t in st["tasks"] if t["status"] == "next" and not t["wait"]]
        k2 = nxt[0]["key"]
        c2 = page.locator(f'.bcard[data-task="{k2}"]')
        c2.scroll_into_view_if_needed()
        x0, y0 = center(c2.bounding_box())
        x1 = center(page.locator('.bcol[data-col="wait"]').bounding_box())[0]
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x1, y0, steps=15)
        page.mouse.up()
        page.wait_for_selector('#drawer form[data-form="edit"]')
        focused = page.evaluate("document.activeElement && document.activeElement.name")
        check(focused == "wait" and task(k2)["status"] == "wait", f"в «Ждёт»: статус сменён, открыта форма, фокус на «Ждём» ({focused})")
        page.screenshot(path=os.path.join(out, "03_wait_form.png"))
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_selector("#drawer", state="hidden")

        # обычный щелчок открывает карточку
        page.locator('.bcol[data-col="doing"] .bcard').first.click()
        page.wait_for_selector("#drawer .fields")
        check("#board" in page.url and "&t=" in page.url, "щелчок по карточке открывает задачу поверх доски")
        page.keyboard.press("Escape")
        page.wait_for_selector("#drawer", state="hidden")

        # фильтры
        page.select_option("select[data-bscope]", "p:lab")
        page.wait_for_url(base + "/#board=p:lab")
        lab = sum(1 for t in st["tasks"] if t["project"] == "lab" and t["status"] in open_by)
        check(page.locator(".bcard:not(.closed)").count() == lab and page.locator(".bc-p").count() == 0,
              f"фильтр «sr-lab»: {lab} открытых, без подписи проекта")
        page.select_option("select[data-bscope]", "")
        page.select_option("select[data-bwho]", "me")
        mine = page.locator(".bcard").count()
        check(mine > 0 and all(page.locator(".bcard .av").nth(i).get_attribute("title").startswith("Артём")
                               for i in range(mine)), f"фильтр «Держу я»: {mine} карточек, все у Артёма")
        page.select_option("select[data-bwho]", "")
        page.fill("#q", "эльтан")
        page.wait_for_selector(".qbar")
        found = page.locator(".bcard").count()
        check(0 < found < 20 and page.locator(".bcard mark").count() > 0, f"поиск по доске: {found} карточек, совпадения подсвечены")
        page.fill("#q", "")
        page.goto(base + "/#p=lab")
        page.wait_for_selector('[data-board="p:lab"]')
        page.click('[data-board="p:lab"]')
        page.wait_for_url(base + "/#board=p:lab")
        check(True, "кнопка «Доской» в проекте ведёт на доску проекта")

        # ---------- вложения ----------
        # у INF-30 на боевой копии уже есть свои вложения (их файлов в копии нет — отсюда 404 в консоли):
        # проверки считают прибавку к ним
        before = call("GET", "/api/task/INF-30")["files"]
        n0, imgs0 = len(before), sum(1 for f in before if f["image"])
        page.goto(base + "/#t=INF-30")
        page.wait_for_selector("#drawer [data-attach]")
        page.set_input_files("#file-input", [PNG_PATH, TXT_PATH])
        page.wait_for_selector('#drawer .thumb img[alt="Схема доски.png"]')
        page.wait_for_selector("#drawer .flist li")
        nw = 0
        for _ in range(50):
            nw = page.evaluate("() => { const i = document.querySelector('#drawer .thumb img[alt=\"Схема доски.png\"]'); return i ? i.naturalWidth : 0; }")
            if nw == 320:
                break
            time.sleep(0.1)
        check(nw == 320, f"картинка прикреплена и показана, CSP не мешает (ширина {nw})")
        files = call("GET", "/api/task/INF-30")["files"]
        check([f["name"] for f in files][n0:] == ["Схема доски.png", "crash.log"], f"на сервере: {[f['name'] for f in files]}")
        page.locator("#drawer").screenshot(path=os.path.join(out, "04_card_files.png"))
        log_id = next(f["id"] for f in files if f["name"] == "crash.log")
        resp = page.request.get(base + f"/api/file/{log_id}?dl=1")
        check(resp.headers.get("content-disposition", "").startswith("attachment") and resp.body() == open(TXT_PATH, "rb").read(),
              "лог отдаётся на скачивание, байты целы (cp1251)")
        # удалить лог: два нажатия
        page.click("#drawer .flist [data-fdel]")
        check(page.locator("#drawer .flist [data-fdel]").inner_text() == "Точно?", "удаление спрашивает подтверждение")
        page.click("#drawer .flist [data-fdel]")
        page.wait_for_selector("#drawer .flist", state="detached")
        check(len(call("GET", "/api/task/INF-30")["files"]) == n0 + 1, "лог удалён")
        # вставка скриншота из буфера
        page.evaluate("""() => { const b = new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82]);
            const dt = new DataTransfer(); dt.items.add(new File([b], 'image.png', {type: 'image/png'}));
            document.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""")
        page.wait_for_selector(f"#drawer .thumb >> nth={imgs0 + 1}")
        names = [f["name"] for f in call("GET", "/api/task/INF-30")["files"]]
        check(names[-1].startswith("скриншот ") and names[-1].endswith(".png"), f"Ctrl+V: скриншот с датой в имени ({names[-1]})")
        # файл, брошенный на карточку
        dt = page.evaluate_handle("() => { const d = new DataTransfer(); d.items.add(new File(['hello'], 'бросил.txt', {type: 'text/plain'})); return d; }")
        page.dispatch_event("#drawer", "dragenter", {"dataTransfer": dt})
        check("dropping" in (page.get_attribute("#drawer", "class") or ""), "перетаскивание файла подсвечивает карточку")
        page.dispatch_event("#drawer", "drop", {"dataTransfer": dt})
        page.wait_for_selector("#drawer .flist li")
        check("бросил.txt" in page.locator("#drawer .flist").inner_text(), "файл, брошенный на карточку, прикреплён")
        jr = page.locator("#drawer .journal").inner_text()
        check("Вложение: Схема доски.png" in jr and "Вложение удалено: crash.log" in jr, "журнал карточки: вложения записаны")
        page.locator("#drawer").screenshot(path=os.path.join(out, "05_card_files_after.png"))
        page.keyboard.press("Escape")
        page.goto(base + "/#all")
        page.fill("#q", "INF-30")
        page.wait_for_selector(".qbar")
        check(f"{n0 + 3} вложени" in page.locator(".trow").first.inner_text(), f"в списке: «{n0 + 3} вложения» у задачи")
        page.fill("#q", "")
        page.goto(base + "/#board")
        page.wait_for_selector(".bcol")
        bc = page.locator('.bcard[data-task="INF-30"]')
        check(bc.count() == 1 and str(n0 + 3) in bc.locator(".bc-foot").inner_text(), "на доске: скрепка с числом вложений")

        # ---------- сообщение для ИИ, модель и усилие по уровням ----------
        page.goto(base + "/#all")
        page.wait_for_selector('[data-aiedit="pult"]')
        page.click('[data-aiedit="pult"]')
        page.fill('form[data-form="ai"] input[name="model"]', "claude-opus-5-5")
        page.fill('form[data-form="ai"] input[name="effort"]', "high")
        page.click('form[data-form="ai"] button[type="submit"]')
        page.wait_for_selector('.ai-line:has-text("claude-opus-5-5")')
        ai_now = call("GET", "/api/state")["ai"]
        check((ai_now["model"], ai_now["effort"]) == ("claude-opus-5-5", "high"),
              "«Все задачи» → «изменить»: модель и усилие для всего Пульта сохранены")
        proj = task("INF-30")["project"]
        page.goto(base + "/#p=" + proj)
        page.wait_for_selector(f'[data-aiedit="p:{proj}"]')
        page.click(f'[data-aiedit="p:{proj}"]')
        ph = page.get_attribute('form[data-form="ai"] input[name="model"]', "placeholder")
        check(ph == "как у раздела: claude-opus-5-5", f"в проекте подсказка — что достанется сверху ({ph})")
        page.fill('form[data-form="ai"] input[name="effort"]', "max")
        page.click('form[data-form="ai"] button[type="submit"]')
        page.wait_for_selector('.ai-line:has-text("из проекта")')
        line = page.locator(".ai-line").inner_text()
        check("claude-opus-5-5 для всего Пульта" in line and "max из проекта" in line, f"строка ИИ проекта: {line!r}")
        page.screenshot(path=os.path.join(out, "11_project_ai.png"))
        page.goto(base + f"/#p={proj}&t=INF-30")
        page.wait_for_selector("#drawer .fields")
        dd = page.locator('#drawer .fields dt:text-is("ИИ") + dd').inner_text()
        check("claude-opus-5-5" in dd and "max" in dd and "из проекта" in dd, f"карточка: строка «ИИ» — {dd!r}")
        # сообщение и своё усилие карточки — через окно «Выполнить», кнопкой «Только оставить сообщение»
        page.click("#drawer [data-run]")
        page.wait_for_selector('#drawer form[data-form="run"]')
        val = page.input_value('#drawer form[data-form="run"] input[name="effort"]')
        check(val == "max", f"в окне «Выполнить» усилие подставлено по уровням — с проекта ({val})")
        page.fill('#drawer form[data-form="run"] textarea[name="message"]', "Проверь доску на телефоне и запиши, что неудобно")
        page.fill('#drawer form[data-form="run"] input[name="effort"]', "low")
        page.locator("#drawer").screenshot(path=os.path.join(out, "12a_run_dialog_first.png"))
        page.click("#drawer [data-askonly]")
        page.wait_for_selector("#drawer .ask")
        t30 = task("INF-30")
        check((t30["prompt"], t30["effort"], t30["ai"]["effort_from"]) == ("Проверь доску на телефоне и запиши, что неудобно", "low", "task"),
              "сообщение для ИИ и своё усилие карточки сохранены")
        check("Проверь доску на телефоне" in page.locator("#drawer .ask").inner_text(), "блок «Сообщение для ИИ» в карточке")
        check("Сообщение для ИИ: «Проверь доску на телефоне и запиши, что неудобно»" in page.locator("#drawer .journal").inner_text(),
              "поручение в журнале целиком")
        page.locator("#drawer").screenshot(path=os.path.join(out, "12_card_prompt.png"))
        page.keyboard.press("Escape")
        page.goto(base + "/#board")
        page.wait_for_selector(".bcol")
        check("ИИ" in page.locator('.bcard[data-task="INF-30"] .bc-foot').inner_text(), "на доске: значок сообщения для ИИ")

        # ---------- шаги: вернулись из описания (схема 6), флажки нажимаются ----------
        page.goto(base + f"/#p={proj}&t=INF-30")
        page.wait_for_selector("#drawer .checklist")
        steps0 = task("INF-30")["checklist"]
        check(len(steps0) >= 1 and "Шаги (раньше были отдельным полем)" not in (task("INF-30")["notes"] or ""),
              f"шаги снова отдельным полем, в описании их нет ({len(steps0)})")
        head = page.locator("#drawer h3", has_text="Шаги").text_content()  # inner_text — заглавными, как на экране
        check(f"{sum(c['on'] for c in steps0)} из {len(steps0)}" in head, f"заголовок блока: {head!r}")
        page.click('#drawer [data-step="0"]')
        page.wait_for_selector('#drawer [data-step="0"][aria-pressed="' + str(not steps0[0]["on"]).lower() + '"]')
        check(task("INF-30")["checklist"][0]["on"] is not steps0[0]["on"], "щелчок по флажку меняет шаг на сервере")
        check(f"Шаг «{steps0[0]['t'][:20]}" in page.locator("#drawer .journal").inner_text(), "журнал: шаг отмечен")
        page.fill('#drawer form[data-form="addstep"] input[name="t"]', "проверить на телефоне")
        page.click('#drawer form[data-form="addstep"] button[type="submit"]')
        page.wait_for_selector('#drawer [data-step="' + str(len(steps0)) + '"]')
        check(task("INF-30")["checklist"][-1] == {"t": "проверить на телефоне", "on": False}, "«Новый шаг» добавлен в конец")
        page.click("#drawer [data-edit]")
        page.wait_for_selector('#drawer form[data-form="edit"] .steps-edit')
        check(page.locator("#drawer .steps-edit li").count() == len(steps0) + 1, "в «Изменить» — шаги списком")
        page.click("#drawer [data-addrow]")
        page.fill('#drawer .steps-edit li:last-child input[name="s_t"]', "из формы")
        page.check('#drawer .steps-edit li:last-child input[name="s_on"]')
        page.click('#drawer form[data-form="edit"] button[type="submit"]')
        page.wait_for_selector("#drawer .checklist")
        steps1 = task("INF-30")["checklist"]
        check(steps1[-1] == {"t": "из формы", "on": True} and len(steps1) == len(steps0) + 2, "шаг из формы сохранён")
        page.locator("#drawer").screenshot(path=os.path.join(out, "12b_card_steps.png"))
        frac = f"{sum(c['on'] for c in steps1)}/{len(steps1)}"
        page.keyboard.press("Escape")
        page.goto(base + "/#board")
        page.wait_for_selector(".bcol")
        check(frac in page.locator('.bcard[data-task="INF-30"] .bc-foot').inner_text(), f"на доске: шаги {frac}")

        # ---------- «Выполнить»: окно передачи в работу, блок сессии, отмена ----------
        page.goto(base + f"/#p={proj}&t=INF-30")
        page.wait_for_selector("#drawer [data-run]")
        page.click("#drawer [data-edit]")
        page.wait_for_selector('#drawer form[data-form="edit"]')
        check(page.locator('#drawer form[data-form="edit"] textarea[name="prompt"]').count() == 0,
              "в «Изменить» поля сообщения для ИИ больше нет")
        page.click('#drawer form[data-form="edit"] [data-cancel]')
        page.click("#drawer [data-run]")
        page.wait_for_selector('#drawer form[data-form="run"]')
        msg = page.input_value('#drawer form[data-form="run"] textarea[name="message"]')
        check(msg == "Проверь доску на телефоне и запиши, что неудобно", f"окно подставило сообщение для ИИ ({msg[:30]!r})")
        check(page.input_value('#drawer input[name="budget"]') == "3" and page.input_value('#drawer input[name="minutes"]') == "60",
              "пределы по умолчанию: 3% недели и 60 минут")
        check(page.input_value('#drawer form[data-form="run"] input[name="effort"]') == "low", "усилие подставлено из карточки")
        week = page.locator("#run-week").inner_text()
        check("исполнитель" in week and "потолок 80%" in week, f"строка про исполнителя и неделю: {week!r}")
        page.locator("#drawer").screenshot(path=os.path.join(out, "15_run_dialog.png"))
        page.click('#drawer form[data-form="run"] button[type="submit"]')
        page.wait_for_selector("#drawer .runbox")
        t30 = call("GET", "/api/task/INF-30")
        check(t30["runs"][0]["status"] == "queued" and t30["task"]["prompt"] == "" and t30["task"]["status"] == "doing",
              "заявка в очереди, сообщение ушло в заявку, карточка «В работе»")
        check("в очереди" in page.locator("#drawer .runbox").inner_text(), "в карточке — блок сессии «в очереди»")
        page.locator("#drawer").screenshot(path=os.path.join(out, "16_card_run_queued.png"))
        page.click("#drawer [data-runstop]")
        page.wait_for_selector("#drawer .runbox.run-cancelled")
        check(call("GET", "/api/task/INF-30")["runs"][0]["status"] == "cancelled", "«Отменить» снимает заявку из очереди")
        page.goto(base + "/#all")
        page.wait_for_selector('[data-aiedit="pult"]')
        page.click('[data-aiedit="pult"]')
        page.fill('form[data-form="ai"] input[name="ceiling"]', "75")
        page.click('form[data-form="ai"] button[type="submit"]')
        page.wait_for_selector('.ai-line:has-text("потолок")')
        check(call("GET", "/api/state")["ai"]["ceiling"] == 75 and "75%" in page.locator(".ai-line").inner_text(),
              "потолок недели меняется в шапке «Все задачи»")

        # ---------- тёмная тема ----------
        dark = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU", color_scheme="dark")
        dp = dark.new_page()
        watch(dp, "dark")
        dp.goto(base + "/login#k=" + KEY)
        dp.wait_for_url(base + "/#all")
        dp.goto(base + "/#board")
        dp.wait_for_selector(".bcol")
        dp.screenshot(path=os.path.join(out, "06_board_dark.png"))
        dp.goto(base + "/#board&t=INF-30")
        dp.wait_for_selector("#drawer .thumb img")
        dp.wait_for_timeout(300)
        dp.screenshot(path=os.path.join(out, "07_card_dark.png"))

        # ---------- телефон ----------
        phone = browser.new_context(viewport={"width": 400, "height": 860}, locale="ru-RU", device_scale_factor=2,
                                    is_mobile=True, has_touch=True)
        pp = phone.new_page()
        watch(pp, "phone")
        pp.goto(base + "/login#k=" + KEY)
        pp.wait_for_url(base + "/#all")
        pp.goto(base + "/#board")
        pp.wait_for_selector(".bcol")
        w = pp.evaluate("document.documentElement.scrollWidth")
        bw = pp.evaluate("[document.getElementById('board').scrollWidth, document.getElementById('board').clientWidth]")
        check(w <= 400 and bw[0] > bw[1], f"телефон: страница без сдвига вбок ({w}px), колонки листаются внутри доски {bw}")
        check(pp.locator(".touch-only").first.is_visible(), "телефон: подсказка про долгое нажатие видна")
        pp.screenshot(path=os.path.join(out, "08_phone_board.png"))
        cdp = phone.new_cdp_session(pp)

        def touch(kind, x=None, y=None):
            cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if x is None else [{"x": x, "y": y}]})

        # короткое движение пальцем — прокрутка, а не перенос
        first = pp.locator('.bcol[data-col="inbox"] .bcard').first
        k3 = first.get_attribute("data-task")
        x0, y0 = center(first.bounding_box())
        before = pp.evaluate("document.getElementById('board').scrollLeft")
        touch("touchStart", x0, y0)
        for i in range(1, 9):
            touch("touchMove", x0 - i * 25, y0)
            pp.wait_for_timeout(16)
        touch("touchEnd")
        pp.wait_for_timeout(400)
        after = pp.evaluate("document.getElementById('board').scrollLeft")
        check(task(k3)["status"] == "inbox" and pp.locator(".bghost").count() == 0,
              f"телефон: быстрый свайп не переносит карточку (прокрутка {before}→{after})")
        pp.evaluate("document.getElementById('board').scrollLeft = 0")
        pp.wait_for_timeout(300)
        # долгое нажатие и перенос в соседнюю колонку
        x0, y0 = center(first.bounding_box())
        touch("touchStart", x0, y0)
        pp.wait_for_timeout(600)
        lifted = pp.locator(".bghost").count() == 1
        for i in range(1, 13):
            touch("touchMove", x0 + i * 15, y0 + i * 2)
            pp.wait_for_timeout(30)
        target = pp.evaluate("(document.querySelector('.bcol.over') || {}).dataset ? document.querySelector('.bcol.over').dataset.col : null")
        pp.screenshot(path=os.path.join(out, "09_phone_dragging.png"))
        touch("touchEnd")
        pp.wait_for_timeout(800)
        st3 = task(k3)["status"]
        check(lifted and target not in (None, "inbox") and st3 == target,
              f"телефон: долгое нажатие поднимает карточку, перенос в «{target}» → статус {st3}")
        pp.goto(base + "/#t=INF-30")
        pp.wait_for_selector("#drawer .thumb img", state="attached")  # картинки грузятся по мере прокрутки
        dw = pp.evaluate("document.getElementById('drawer').scrollWidth")
        check(dw <= 400, f"телефон: карточка с вложениями по ширине ({dw}px)")
        pp.locator("#drawer [data-attach]").scroll_into_view_if_needed()
        pp.screenshot(path=os.path.join(out, "10_phone_card_files.png"))
        pp.locator("#drawer .runbox").scroll_into_view_if_needed()
        pp.screenshot(path=os.path.join(out, "13_phone_card_run.png"))
        pp.click("#drawer [data-run]")
        pp.wait_for_selector('#drawer form[data-form="run"]')
        dw = pp.evaluate("document.getElementById('drawer').scrollWidth")
        check(dw <= 400, f"телефон: окно «Выполнить» по ширине ({dw}px)")
        pp.screenshot(path=os.path.join(out, "13a_phone_run_dialog.png"))
        pp.click('#drawer form[data-form="run"] [data-cancel]')
        pp.keyboard.press("Escape")
        pp.goto(base + "/#p=" + proj)
        pp.wait_for_selector(".ai-line")
        mw = pp.evaluate("document.getElementById('main').scrollWidth")
        check(mw <= 400, f"телефон: шапка проекта со строкой ИИ по ширине ({mw}px)")
        pp.screenshot(path=os.path.join(out, "14_phone_project_ai.png"))
        browser.close()
finally:
    proc.terminate()
    proc.wait(10)
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.stderr.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n".join(notes))
    print("console/page errors:", errors or "нет")
    print("server stderr:", err.strip()[-2000:] or "пусто")
