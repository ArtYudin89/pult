# -*- coding: utf-8 -*-
"""Поддельный claude для проверки исполнителя Пульта: --bg, agents --json --all, stop, logs, --version.

Сессии лежат файлами в FAKE_CLAUDE_DIR. Что делает сессия, решает метка в тексте поручения:
    [done]      поработала и закончила ответом (по умолчанию)
    [question]  закончила вопросом «ВОПРОС: …»
    [slow]      работает, пока не остановят; каждый agents прибавляет 0,5% недели
    [block]     ждёт разрешения на компьютере
    [noauto]    то же, но на экране «auto mode unavailable for this model»
    [stuck]     не дошла до работы: застряла на окне при запуске (доверие к папке)
Строку состояния пишет командой из --settings, как настоящий Claude Code, — так проверяется и
pult_runner.py statusline. Все вызовы дописываются в FAKE_CLAUDE_DIR/calls.jsonl.
"""
import json
import os
import subprocess
import sys
import time
import uuid

DIR = os.environ["FAKE_CLAUDE_DIR"]
os.makedirs(DIR, exist_ok=True)


def path(sid):
    return os.path.join(DIR, sid + ".json")


def load(sid):
    with open(path(sid), encoding="utf-8") as f:
        return json.load(f)


def save(s):
    with open(path(s["id"]), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def statusline(s):
    """Как Claude Code: выполнить команду строки состояния и дать ей JSON на вход."""
    if not s.get("statusline"):
        return
    data = {"session_id": s["sessionId"], "session_name": s["name"], "transcript_path": s["transcript"],
            "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": 1793049600},
                            "seven_day": {"used_percentage": s["week"], "resets_at": 1793260800}}}
    subprocess.run(s["statusline"], shell=True, input=json.dumps(data).encode("utf-8"), capture_output=True)


def transcript(s, text):
    with open(s["transcript"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                           ensure_ascii=False) + "\n")


def main(argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
    if argv[:1] == ["--version"]:
        print("2.1.267 (Claude Code) — поддельный")
        return 0
    if argv[:1] == ["agents"]:
        out = []
        for name in sorted(os.listdir(DIR)):
            if not name.endswith(".json"):
                continue
            s = load(name[:-5])
            age = time.time() - s["started"]
            if s["state"] == "working":
                if s["kind"] == "slow":
                    s["week"] = round(s["week"] + 0.5, 2)
                    statusline(s)
                elif age > 0.6 and s["kind"] in ("block", "noauto"):
                    s["state"] = "blocked"
                elif age > 0.6:
                    text = ("Нужно уточнить цвет.\nВОПРОС: Какого цвета кнопка?" if s["kind"] == "question"
                            else "Сделано: поле добавлено, тесты прошли.")
                    transcript(s, text)
                    s["week"] = round(s["week"] + 0.3, 2)
                    statusline(s)
                    s["state"] = "done"
                save(s)
            rec = {"id": s["id"], "sessionId": s["sessionId"], "cwd": s["cwd"], "kind": "background",
                   "name": s["name"], "state": s["state"],
                   "status": {"working": "busy", "blocked": "waiting"}.get(s["state"], "idle")}
            if s["state"] == "blocked" and s["kind"] != "stuck":
                rec["waitingFor"] = "permission prompt"
            out.append(rec)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if argv[:1] in (["stop"], ["logs"]):
        s = load(argv[1])
        if argv[0] == "stop":
            s["state"] = "stopped" if s["state"] != "done" else "done"
            save(s)
            print(f"stopped {s['id']}")
        else:
            print("auto mode unavailable for this model" if s["kind"] == "noauto" else "❯ ждёт разрешения")
        return 0
    if "--bg" in argv:
        opts, i = {}, 0
        while i < len(argv) - 1:
            if argv[i].startswith("-") and argv[i] != "--bg":
                opts[argv[i]] = argv[i + 1]
                i += 2
            else:
                i += 1
        prompt = argv[-1]
        kind = next((k for k in ("question", "slow", "block", "noauto", "stuck") if f"[{k}]" in prompt), "done")
        sid = opts.get("--resume") or str(uuid.uuid4())
        settings = json.load(open(opts["--settings"], encoding="utf-8")) if opts.get("--settings") else {}
        s = {"id": uuid.uuid4().hex[:8], "sessionId": sid, "name": opts.get("-n", ""), "cwd": os.getcwd(),
             "kind": kind, "state": "blocked" if kind == "stuck" else "working", "started": time.time(),
             "week": 50.0, "prompt": prompt,
             "statusline": (settings.get("statusLine") or {}).get("command"),
             "transcript": os.path.join(DIR, sid + ".jsonl")}
        save(s)
        statusline(s)
        print(f"Starting background service…\nbackgrounded · {s['id']} · {s['name']}")
        return 0
    print("неизвестная команда", file=sys.stderr)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
