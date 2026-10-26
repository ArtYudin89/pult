# -*- coding: utf-8 -*-
"""Старая и новая версии сервера на одинаковых копиях боевой базы: ответы владельцу должны совпасть.

Сравнивает для первого владельца (модель раньше человека; другого — переменной PULT_CHECK_ACTOR):
/api/state, каждую /api/task/<номер> с журналом, /api/journal и вывод pult.py brief / projects / ls --all.
Поля, которых у старой версии не было (NEW_* ниже), из сравнения убираются — для следующей версии дописать
в них свои. В копии базы заводится проверочный ключ; настоящие ключи не нужны.

    python -X utf8 tests/ab_compare.py <копия базы.db> <старый pult_server.py> server/pult_server.py server/static .

Копия базы — свежий снимок боевой (docs/install.md, «Обновление»), старый сервер — /opt/pult/pult_server.py
с сервера (scp до выкладки) или из резервной копии.
"""
import contextlib
import io
import json
import os
import sqlite3
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

prod, old_srv, new_srv, static, cli_dir = sys.argv[1:6]
sys.path.insert(0, cli_dir)
import pult as cli  # noqa: E402

# этап 3: в /api/state — limits, у задачи — число вложений, у карточки — их список.
# Схема 4: у задачи — сообщение для ИИ, модель и усилие, у карточки — ещё ai (что получилось по уровням),
# у проекта и раздела — модель и усилие, в /api/state — ai всего Пульта. Поля «Шаги» нет: при переходе его
# пункты дописаны в конец описания, версия карточки и rev выросли на 1 — as_after_move() делает то же
# со старым ответом, а остальное должно совпасть.
NEW_TOP = {"limits", "ai"}
NEW_ACTOR = set()
NEW_AREA = {"model", "effort"}
NEW_PROJECT = {"model", "effort"}
NEW_JOURNAL = set()
NEW_TASK = {"files", "prompt", "model", "effort"}
NEW_CARD = {"files"}
NEW_CARD_TASK = {"ai"}
GONE_TASK = {"checklist"}


def pick_actor(db_path, prefer):
    """Кого проверять: PULT_CHECK_ACTOR или первый владелец в копии базы (prefer — model или person)."""
    if os.environ.get("PULT_CHECK_ACTOR"):
        return os.environ["PULT_CHECK_ACTOR"]
    with contextlib.closing(sqlite3.connect(db_path)) as c:
        return c.execute("SELECT id FROM actors WHERE role = 'owner' ORDER BY kind = ? DESC, id",
                         (prefer,)).fetchone()[0]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(server, db, port):
    p = subprocess.Popen([sys.executable, "-X", "utf8", server, "serve", "--db", db, "--static", static,
                          "--host", "127.0.0.1", "--port", str(port), "--plain-http", "--dev-actor", ACTOR],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return p
        time.sleep(0.1)
    raise SystemExit("server did not start: " + p.stderr.read().decode())


def get(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def strip(obj, kind):
    if kind == "state":
        obj = {k: v for k, v in obj.items() if k not in NEW_TOP}
        obj["me"] = {k: v for k, v in obj["me"].items() if k not in NEW_ACTOR}
        obj["actors"] = [{k: v for k, v in a.items() if k not in NEW_ACTOR} for a in obj["actors"]]
        obj["areas"] = [{k: v for k, v in a.items() if k not in NEW_AREA} for a in obj["areas"]]
        obj["projects"] = [{k: v for k, v in p.items() if k not in NEW_PROJECT} for p in obj["projects"]]
        obj["tasks"] = [{k: v for k, v in t.items() if k not in NEW_TASK | GONE_TASK} for t in obj["tasks"]]
    elif kind == "journal":
        obj = [{k: v for k, v in e.items() if k not in NEW_JOURNAL} for e in obj]
    elif kind == "card":
        obj = {k: v for k, v in obj.items() if k not in NEW_CARD}
        obj["task"] = {k: v for k, v in obj["task"].items() if k not in NEW_TASK | NEW_CARD_TASK | GONE_TASK}
        obj["journal"] = strip(obj["journal"], "journal")
    return obj


def as_after_move(obj, kind):
    """Старый ответ таким, каким его должен сделать переход на схему 4: «Шаги» — в конце описания, версия +1."""
    def fix(t, full):
        items = t.get("checklist") or []
        if not items:
            return t
        t = dict(t, version=t["version"] + 1, has_notes=True)
        if full:
            t["notes"] = (t["notes"] + "\n\n" if t["notes"] else "") + "Шаги (раньше были отдельным полем):\n" + \
                "\n".join(("☑ " if i["on"] else "☐ ") + i["t"].strip() for i in items)
        return t
    if kind == "state":
        moved = any(t.get("checklist") for t in obj["tasks"])
        return dict(obj, tasks=[fix(t, False) for t in obj["tasks"]], rev=obj["rev"] + moved)
    return dict(obj, task=fix(obj["task"], True))


def cli_out(port, *argv):
    env = {"PULT_URL": f"http://127.0.0.1:{port}", "PULT_TOKEN": TOKEN, "PULT_CONFIG": os.devnull + "_none",
           "PULT_SESSION": "", "PULT_SEEN_DIR": tmp}
    buf = io.StringIO()
    saved = dict(os.environ)
    os.environ.update(env)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.main(list(argv))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return buf.getvalue().replace(env["PULT_URL"], "<адрес>")  # два сервера — два порта в шапке brief


tmp = tempfile.mkdtemp(prefix="pult_ab_")
TOKEN = "ab-compare-" + "q" * 40
ACTOR = pick_actor(prod, "model")
dbs = {}
for side in ("old", "new"):
    dbs[side] = os.path.join(tmp, side + ".db")
    shutil.copyfile(prod, dbs[side])
    import hashlib
    import sqlite3
    with contextlib.closing(sqlite3.connect(dbs[side])) as c:
        c.execute("INSERT INTO keys(hash, actor_id, label, created_at) VALUES (?, ?, 'ab', '2026-10-26T00:00:00+03:00')",
                  (hashlib.sha256(TOKEN.encode()).hexdigest(), ACTOR))
        c.commit()
ports = {"old": free_port(), "new": free_port()}
procs = {side: start(srv, dbs[side], ports[side]) for side, srv in (("old", old_srv), ("new", new_srv))}
diffs = 0
try:
    st = {s: get(ports[s], "/api/state") for s in ports}
    extra = sorted(set(st["new"]) - set(st["old"]))
    print("state: new top-level fields:", extra)
    print("state: roles after migration:", [(a["id"], a["role"], a["owner"]) for a in st["new"]["actors"]])
    print("state: access entries:", len(st["new"]["access"]), "projects:", len(st["new"]["projects"]),
          "writers of eltan:", next(p["writers"] for p in st["new"]["projects"] if p["id"] == "eltan"))
    moved = [t["key"] for t in st["old"]["tasks"] if t.get("checklist")]
    print(f"state: cards with steps moved into notes: {len(moved)}")
    a, b = strip(as_after_move(st["old"], "state"), "state"), strip(st["new"], "state")
    if a != b:
        diffs += 1
        for k in a:
            if a[k] != b.get(k):
                print("  STATE DIFF in", k)
    else:
        print("state: identical apart from new fields (tasks %d, projects %d, areas %d, actors %d)" % (
            len(a["tasks"]), len(a["projects"]), len(a["areas"]), len(a["actors"])))
    bad = []
    for t in st["old"]["tasks"]:
        o = get(ports["old"], f"/api/task/{urllib.request.quote(t['key'])}?journal=1000")
        n = get(ports["new"], f"/api/task/{urllib.request.quote(t['key'])}?journal=1000")
        if strip(as_after_move(o, "card"), "card") != strip(n, "card"):
            bad.append(t["key"])
    print(f"cards: {len(st['old']['tasks'])} compared, differ: {bad or 'none'}")
    diffs += bool(bad)
    jo = get(ports["old"], "/api/journal?limit=500")["journal"]
    jn = get(ports["new"], "/api/journal?limit=500")["journal"]
    same = strip(jo, "journal") == strip(jn, "journal")
    print(f"journal: {len(jo)} entries, identical apart from new fields: {same}")
    diffs += not same
    for argv in (("brief",), ("projects",), ("brief", "--project", "pult"), ("ls", "--all")):
        o, n = cli_out(ports["old"], *argv), cli_out(ports["new"], *argv)
        print(f"pult.py {' '.join(argv)}: {'same' if o == n else 'DIFFERENT'} ({len(o.splitlines())} lines)")
        if o != n:
            diffs += 1
            import difflib
            print("".join(list(difflib.unified_diff(o.splitlines(True), n.splitlines(True), "old", "new"))[:40]))
finally:
    for p in procs.values():
        p.terminate()
        p.wait(10)
        p.stderr.close()
    shutil.rmtree(tmp, ignore_errors=True)
print("RESULT:", "no differences" if not diffs else f"{diffs} difference group(s)")
