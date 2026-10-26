# -*- coding: utf-8 -*-
"""Старый и новый pult.py против одного сервера на копии боевой базы: команды чтения должны печатать одно и то же.
Нужен, когда меняется сама обёртка (на этапе 3 — отправка запросов); ab_compare.py гоняет одну обёртку против двух серверов.

    python -X utf8 tests/cli_compare.py <копия базы.db> server/pult_server.py server/static <старый pult.py> pult.py
"""
import contextlib
import hashlib
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time


def pick_actor(db_path, prefer):
    """Кого проверять: PULT_CHECK_ACTOR или первый владелец в копии базы (prefer — model или person)."""
    if os.environ.get("PULT_CHECK_ACTOR"):
        return os.environ["PULT_CHECK_ACTOR"]
    with contextlib.closing(sqlite3.connect(db_path)) as c:
        return c.execute("SELECT id FROM actors WHERE role = 'owner' ORDER BY kind = ? DESC, id",
                         (prefer,)).fetchone()[0]

src, server, static, old_cli, new_cli = sys.argv[1:6]
tmp = tempfile.mkdtemp(prefix="pult_cli_")
db = os.path.join(tmp, "c.db")
shutil.copyfile(src, db)
TOKEN = "cli-compare-" + "z" * 40
with contextlib.closing(sqlite3.connect(db)) as c:
    c.execute("INSERT INTO keys(hash, actor_id, label, created_at) VALUES (?, ?, 'cmp', '2026-10-26T00:00:00+03:00')",
              (hashlib.sha256(TOKEN.encode()).hexdigest(),
               pick_actor(db, "model")))
    c.commit()
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
proc = subprocess.Popen([sys.executable, "-X", "utf8", server, "serve", "--db", db, "--static", static,
                         "--host", "127.0.0.1", "--port", str(port), "--plain-http"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
for _ in range(100):
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
        break
    time.sleep(0.1)
env = dict(os.environ, PULT_URL=f"http://127.0.0.1:{port}", PULT_TOKEN=TOKEN, PULT_CONFIG=os.path.join(tmp, "none.json"),
           PULT_SESSION="", PULT_SEEN_DIR=os.path.join(tmp, "seen"), PULT_SECRETS_DIR=os.path.join(tmp, "sec"))
env.pop("CLAUDE_CODE_SESSION_ID", None)
diffs = 0
try:
    for argv in (["brief"], ["brief", "--project", "eltan"], ["projects"], ["ls", "--all"], ["show", "SR-40"],
                 ["show", "INF-30", "--journal", "100"], ["log", "--since", "30d", "--limit", "300"], ["people"],
                 ["find", "эльтан"], ["whoami"], ["url", "SR-1"], ["show", "NOPE-1"]):
        outs = [subprocess.run([sys.executable, "-X", "utf8", cli] + argv, capture_output=True, text=True,
                               encoding="utf-8", env=env) for cli in (old_cli, new_cli)]
        same = all(getattr(outs[0], k) == getattr(outs[1], k) for k in ("stdout", "stderr", "returncode"))
        diffs += not same
        print(f"{' '.join(argv)}: {'same' if same else 'DIFFERENT'} ({len(outs[0].stdout.splitlines())} lines, rc {outs[0].returncode})")
        if not same:
            import difflib
            for k in ("stdout", "stderr"):
                print("".join(list(difflib.unified_diff(getattr(outs[0], k).splitlines(True),
                                                        getattr(outs[1], k).splitlines(True), "old", "new"))[:30]))
finally:
    proc.terminate()
    proc.wait(10)
    proc.stderr.close()
    shutil.rmtree(tmp, ignore_errors=True)
print("RESULT:", "no differences" if not diffs else f"{diffs} different")
