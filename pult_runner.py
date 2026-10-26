#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Исполнитель Пульта: забирает заявки «Выполнить» своей модели и запускает по ним сессии Claude Code
на этом компьютере — там, где лежат файлы. Только стандартная библиотека, Python 3.9+.

    pult_runner.py run                  работать: ждать заявки и запускать сессии (Ctrl+C — выход)
    pult_runner.py check                проверить адрес и ключ, программу claude и папки проектов
    pult_runner.py autostart on|off     Windows: запуск при входе в систему (ярлык в «Автозагрузке»)
    pult_runner.py statusline ФАЙЛ      служебная: строка состояния сессии сохраняет данные в ФАЙЛ

Настройки — .local/runner.json рядом с программой (все поля необязательны):
    {"url": "https://…:9447", "token_file": "claude-runner.token",   ключ модели; иначе — из .local/config.json
     "base": "C:\\work",                 от этой папки считается поле «папка» проекта; по умолчанию — папка
                                         над папкой Пульта
     "folders": {"site": "D:\\site"},    своя папка для отдельного проекта
     "claude": "claude", "python": "python", "permission_mode": "auto", "poll": 25}

Как запускает: claude --bg в папке проекта с моделью, усилием и режимом разрешений из заявки; продолжение —
claude --resume <сессия> --bg. Состояние — claude agents --json. Расход недели — из строки состояния самой
сессии (rate_limits.seven_day.used_percentage, только при входе по подписке). Это расход всего аккаунта:
в бюджет запуска попадает и работа человека в то же время. Вышло время, бюджет или потолок недели —
сессия останавливается (claude stop), разговор сохраняется, продолжить можно ответом.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, ".local")
WORK = os.environ.get("PULT_RUNNER_DIR") or os.path.join(LOCAL, "runner")  # журнал и файлы сессий
VERSION = "2026.10.27"
LOCK_PORT = 47447            # второй исполнитель на этом же компьютере не стартует
USAGE_FRESH_SEC = 6 * 3600   # расход недели старше этого для проверки потолка перед запуском не годится
START_GRACE_SEC = 60         # столько ждём, пока новая сессия появится в claude agents
WATCH_SEC = float(os.environ.get("PULT_RUNNER_TICK") or 10)  # как часто смотреть на идущую сессию
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg):
    line = f"{dt.datetime.now():%d.%m %H:%M:%S} {msg}"
    print(line, flush=True)  # под pythonw вывода нет — print молча ничего не делает
    try:
        os.makedirs(WORK, exist_ok=True)
        path = os.path.join(WORK, "runner.log")
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fwd(path):
    """Путь для команды строки состояния: её выполняет оболочка, и обратная косая черта там — экранирование."""
    return path.replace("\\", "/")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def question_of(text):
    """«ВОПРОС: …» в последних строках ответа — сессия ждёт человека."""
    lines = (text or "").strip().splitlines()
    for i in range(len(lines) - 1, max(-1, len(lines) - 9), -1):
        s = lines[i].strip().lstrip("*_ ")
        if s.upper().startswith("ВОПРОС:"):
            return "\n".join([s[len("ВОПРОС:"):].lstrip("*_ ").strip()] + lines[i + 1:]).strip()
    return ""


def last_text(path):
    """Последний текст модели из журнала сессии (.jsonl Claude Code)."""
    text = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                c = (e.get("message") or {}).get("content")
                parts = [c] if isinstance(c, str) else [x.get("text", "") for x in c or []
                                                        if isinstance(x, dict) and x.get("type") == "text"]
                t = "\n".join(p for p in parts if p).strip()
                if t:
                    text = t
    except OSError:
        pass
    return text


def load_cfg():
    sys.path.insert(0, HERE)
    import pult as cli  # адрес, ключ и запросы — как у pult.py
    path = os.path.join(LOCAL, "runner.json")
    cfg = read_json(path) or {}
    try:
        pult = cli.load_config()
    except cli.PultError:
        pult = {"url": "", "token": ""}
    if cfg.get("url"):
        pult["url"] = cfg["url"].rstrip("/")
    if cfg.get("token_file"):
        tf = cfg["token_file"] if os.path.isabs(cfg["token_file"]) else os.path.join(LOCAL, cfg["token_file"])
        with open(tf, encoding="utf-8") as f:
            pult["token"] = f.read().strip()
    if not pult.get("url") or not pult.get("token"):
        raise SystemExit("нет адреса или ключа Пульта: .local/runner.json (url, token_file) или .local/config.json")
    pult["session"] = ""
    return {"cli": cli, "pult": pult, "base": cfg.get("base") or os.path.dirname(HERE),
            "folders": cfg.get("folders") or {}, "claude": cfg.get("claude") or "claude",
            "python": cfg.get("python") or "python", "permission_mode": cfg.get("permission_mode") or "auto",
            "poll": int(cfg.get("poll") or 25)}


def folder_for(cfg, run):
    f = cfg["folders"].get(run.get("project") or "")
    if not f:
        rel = run.get("project_path") or ""
        f = os.path.join(cfg["base"], rel) if rel else cfg["base"]
    return os.path.normpath(f)


def claude(cfg, *args, cwd=None, timeout=120):
    # метки сессии, из которой запустили исполнителя (при проверке руками), сессиям не передаём
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "PULT_SESSION")}
    program = cfg["claude"] if isinstance(cfg["claude"], list) else [cfg["claude"]]
    try:
        p = subprocess.run([*program, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def agents(cfg):
    """Сессии Claude Code на этом компьютере: {короткий id: запись}; None — список не получен."""
    rc, out = claude(cfg, "agents", "--json", "--all", timeout=60)
    try:
        items = json.loads(out[out.index("["):])
    except ValueError:
        return None
    return {a["id"]: a for a in items if isinstance(a, dict) and a.get("id")}


def git_root(folder):
    d = os.path.abspath(folder)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def trusted(folder, claude_json=None):
    """Доверяет ли claude папке. Правило Claude Code 2.1.308: вне git доверие наследуется от любой папки
    выше, а внутри git-репо — только до его корня; так что каждому репо нужно своё «Yes, proceed»."""
    data = read_json(claude_json or os.path.join(os.path.expanduser("~"), ".claude.json")) or {}
    ok = {k.replace("\\", "/").rstrip("/").lower() for k, v in (data.get("projects") or {}).items()
          if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}
    root, d = git_root(folder), os.path.abspath(folder)
    while True:
        if d.replace("\\", "/").rstrip("/").lower() in ok:
            return True
        up = os.path.dirname(d)
        if (root and os.path.normcase(d) == os.path.normcase(root)) or up == d:
            return False
        d = up


def job_state(bg):
    """Запись фоновой службы Claude Code о сессии: там видно, что сессия застряла на окне при запуске."""
    st = read_json(os.path.join(os.path.expanduser("~"), ".claude", "jobs", bg, "state.json"))
    return {k: str(v) for k, v in st.items() if isinstance(v, (str, int, float))} if isinstance(st, dict) else {}


def pct(value):
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def minutes_since(iso):
    try:
        return max(0.0, (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(iso)).total_seconds() / 60)
    except (TypeError, ValueError):
        return 0.0


class Runner:
    def __init__(self, cfg):
        self.cfg, self.cli = cfg, cfg["cli"]
        self.active = None   # сессия, которая идёт сейчас: по одной за раз
        self.usage = {}      # последний известный расход: week, five, *_resets, at
        self.host = socket.gethostname()

    def api(self, method, path, body=None, timeout=60):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        raw, _ = self.cli.request_raw(self.cfg["pult"], method, path, data,
                                      {"Accept": "application/json", "Content-Type": "application/json"},
                                      timeout=timeout)
        return json.loads(raw.decode("utf-8"))

    def report(self, run_id, **fields):
        try:
            return self.api("POST", f"/api/run/{run_id}/report", fields)
        except self.cli.PultError as e:
            log(f"#{run_id}: отчёт не принят: {e}")
            return {}

    def tick(self, hello=False):
        busy = self.active is not None
        body = {"host": self.host, "version": VERSION, "busy": busy, "hello": hello,
                "wait": 0 if busy or hello else self.cfg["poll"]}
        body.update({k: self.usage[k] for k in ("week", "five", "week_resets", "five_resets")
                     if self.usage.get(k) is not None})
        if self.usage.get("at"):
            body["usage_at"] = int(self.usage["at"])
        out = self.api("POST", "/api/runner/poll", body, timeout=self.cfg["poll"] + 30)
        for run in out.get("mine") or []:
            self.adopt(run)
        if self.active and self.active["run"]["id"] in (out.get("stop") or []):
            self.stop("остановлена по просьбе человека")
        if out.get("run"):
            try:
                self.start(out["run"])
            except Exception as e:  # заявка не должна повиснуть в «запускается»
                self.active = None
                self.report(out["run"]["id"], status="failed", reason=f"исполнитель: {type(e).__name__}: {e}")
        if self.active:
            self.watch()
            time.sleep(WATCH_SEC)

    def adopt(self, run):
        """После перезапуска исполнителя: сессия, которую он вёл, ещё идёт — следим дальше, иначе — сбой."""
        bg = run.get("bg") or ""
        alive = bg and not self.active and bg in (agents(self.cfg) or {})
        if not alive:
            return self.report(run["id"], status="failed", reason="исполнитель перезапускался, сессии уже нет")
        rid = run["id"]
        self.active = {"run": run, "bg": bg, "status_file": os.path.join(WORK, f"run-{rid}.status.json"),
                       "settings_file": os.path.join(WORK, f"run-{rid}.settings.json"),
                       "started": time.time() - minutes_since(run.get("started_at")) * 60,
                       "week_start": run.get("week_start"), "week": None, "session": run.get("session") or "",
                       "transcript": "", "state": "", "worked": True, "beat": 0.0, "missing": 0.0,
                       "folder": run.get("folder") or ""}
        log(f"#{rid} {run['task']}: подхватил сессию {bg} после перезапуска")

    def prompt(self, run):
        pult_py = fwd(os.path.join(HERE, "pult.py"))
        limits = (f"Пределы запуска: {run['minutes']} мин и {pct(run['budget'])} недельного лимита; "
                  f"дальше сессию остановят.")
        if run.get("resume_of") and run.get("session"):
            return f"Ответ от {run['requester']}:\n\n{run['message']}\n\n{limits}"
        return (f"Карточка Пульта {run['task']} «{run['title']}» ({run['area_name']} › {run['project_name']}). "
                f"Тебя запустил исполнитель Пульта по поручению {run['requester']}:\n\n{run['message']}\n\n"
                f"Начни с `python -X utf8 {pult_py} show {run['task']}` — описание и журнал карточки. Работай как "
                f"обычно; в конце, как всегда, запиши итог в Пульт пакетом apply.\n"
                f"Если без ответа человека дальше нельзя — последней строкой напиши «ВОПРОС: …» и закончи ход: "
                f"вопрос уйдёт человеку в Telegram, а его ответ продолжит эту же сессию.\n{limits}")

    def start(self, run):
        rid = run["id"]
        folder = folder_for(self.cfg, run)
        if not os.path.isdir(folder):
            log(f"#{rid} {run['task']}: нет папки {folder}")
            return self.report(rid, status="failed", reason=f"нет папки проекта на этом компьютере: {folder}")
        week = self.usage.get("week")
        fresh = time.time() - self.usage.get("at", 0) < USAGE_FRESH_SEC
        if week is not None and fresh and week >= run["ceiling"]:
            return self.report(rid, status="failed",
                               reason=f"неделя израсходована на {pct(week)} — выше потолка {pct(run['ceiling'])}")
        os.makedirs(WORK, exist_ok=True)
        status_file = os.path.join(WORK, f"run-{rid}.status.json")
        settings_file = os.path.join(WORK, f"run-{rid}.settings.json")
        command = f'{self.cfg["python"]} "{fwd(os.path.abspath(__file__))}" statusline "{fwd(status_file)}"'
        with open(settings_file, "w", encoding="utf-8") as f:
            # git-репозиторий фоновая сессия иначе правит в отдельной копии (.claude/worktrees), а не в папке
            json.dump({"statusLine": {"type": "command", "command": command},
                       "worktree": {"bgIsolation": "none"}}, f)
        args = ["--bg", "--disallowedTools", "AskUserQuestion", "-n", run["task"],
                "--permission-mode", self.cfg["permission_mode"], "--settings", settings_file]
        if run.get("model"):
            args += ["--model", run["model"]]
        if run.get("effort"):
            args += ["--effort", run["effort"]]
        if run.get("resume_of") and run.get("session"):
            args += ["--resume", run["session"]]
        args.append(self.prompt(run))
        rc, out = claude(self.cfg, *args, cwd=folder)
        m = re.search(r"backgrounded\W+([0-9a-f]{6,})", out)
        if not m:
            tail = " / ".join(out.strip().splitlines()[-3:])[:400]
            log(f"#{rid} {run['task']}: claude не запустил сессию: {tail}")
            return self.report(rid, status="failed", reason=f"claude не запустил сессию: {tail}")
        week0 = week if week is not None and time.time() - self.usage.get("at", 0) < 600 else None
        self.active = {"run": run, "bg": m.group(1), "status_file": status_file, "settings_file": settings_file,
                       "started": time.time(), "week_start": week0, "week": None, "session": run.get("session") or "",
                       "transcript": "", "state": "", "worked": False, "beat": 0.0, "missing": 0.0,
                       "folder": folder}
        log(f"#{rid} {run['task']}: запущена сессия {m.group(1)} в {folder}")
        fields = {"status": "working", "bg": m.group(1), "folder": folder}
        if week0 is not None:
            fields["week_start"] = week0
        self.report(rid, **fields)

    def text(self):
        a = self.active
        path = a["transcript"]
        if not path and a["session"]:
            hits = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", a["session"] + ".jsonl"))
            path = hits[0] if hits else ""
        return last_text(path) if path else ""

    def read_status(self):
        """Строка состояния сессии: расход недели, id сессии и её журнал."""
        a, run = self.active, self.active["run"]
        st = read_json(a["status_file"])
        if not st:
            return
        rl = st.get("rate_limits") or {}
        week, five = rl.get("seven_day") or {}, rl.get("five_hour") or {}
        if week.get("used_percentage") is not None:
            a["week"] = float(week["used_percentage"])
            self.usage = {"week": a["week"], "five": five.get("used_percentage"), "at": time.time(),
                          "week_resets": week.get("resets_at"), "five_resets": five.get("resets_at")}
            if a["week_start"] is None:
                a["week_start"] = a["week"]
                self.report(run["id"], week_start=a["week"])
        a["session"] = st.get("session_id") or a["session"]
        a["transcript"] = st.get("transcript_path") or a["transcript"]

    def finish(self, status, **fields):
        a = self.active
        self.read_status()  # строка состояния могла обновиться вместе с последним ответом
        fields.update(status=status, session=a["session"])
        if a["week"] is not None:
            fields["week_now"] = a["week"]
        for k in ("result", "question"):
            if k in fields:
                fields[k] = (fields[k] or "")[-4000 if k == "result" else -2000:]
        self.report(a["run"]["id"], **fields)
        log(f"#{a['run']['id']} {a['run']['task']}: {status}" + (f" — {fields['reason']}" if fields.get("reason") else ""))
        for f in (a["status_file"], a["settings_file"]):
            try:
                os.remove(f)
            except OSError:
                pass
        self.active = None

    def stop(self, reason, status="stopped"):
        claude(self.cfg, "stop", self.active["bg"], timeout=60)
        self.finish(status, reason=reason, result=self.text())

    def watch(self):
        a, run = self.active, self.active["run"]
        self.read_status()
        listing = agents(self.cfg)
        agent = (listing or {}).get(a["bg"])
        if agent and agent.get("sessionId"):
            a["session"] = agent["sessionId"]
        elapsed = time.time() - a["started"]
        if elapsed > run["minutes"] * 60:
            return self.stop(f"вышло время — {run['minutes']} мин")
        if a["week"] is not None and a["week_start"] is not None and a["week"] - a["week_start"] >= run["budget"]:
            return self.stop(f"исчерпан бюджет — {pct(run['budget'])} недели")
        if a["week"] is not None and a["week"] >= run["ceiling"]:
            return self.stop(f"неделя израсходована на {pct(a['week'])} — потолок {pct(run['ceiling'])}")
        if listing is None:
            return  # claude agents не ответил — посмотрим в следующий раз
        if agent is None:
            a["missing"] = a["missing"] or time.time()
            if time.time() - a["missing"] > START_GRACE_SEC:
                return self.finish("failed", reason="сессии нет в claude agents", result=self.text())
            return
        state = agent.get("state") or ""
        if state == "working" or agent.get("status") == "busy":
            a["worked"] = True
            if a["state"] == "blocked":
                self.report(run["id"], status="working")
        elif state == "blocked":
            if a["state"] != "blocked":
                job = job_state(a["bg"])
                if "startup dialog" in job.get("detail", "") or (not agent.get("waitingFor") and not a["worked"]):
                    return self.stop(f"Claude Code не запустился в {a['folder']}: ждёт подтверждения при старте — "
                                     f"обычно доверия к папке. Откройте один раз claude в этой папке или в папке выше, "
                                     f"ответьте «Yes, proceed» и выйдите; потом — «Продолжить»", status="failed")
                rc, screen = claude(self.cfg, "logs", a["bg"], timeout=60)
                if "auto mode unavailable" in screen:
                    return self.stop(f"у модели {run['model'] or 'по умолчанию'} нет режима auto — "
                                     f"выберите другую модель", status="failed")
                self.report(run["id"], status="blocked", session=a["session"],
                            question=f"ждёт: {agent.get('waitingFor') or 'ответа на том компьютере'}")
        elif state == "done" and (a["worked"] or elapsed > 45):
            text = self.text()
            claude(self.cfg, "stop", a["bg"], timeout=60)  # процесс больше не нужен, разговор сохраняется
            q = question_of(text)
            return self.finish("asked" if q else "done", question=q, result=text)
        elif state in ("failed", "stopped"):
            return self.finish("failed" if state == "failed" else "stopped",
                               reason=f"claude: сессия {state}", result=self.text())
        a["state"] = state
        if time.time() - a["beat"] > 60:  # раз в минуту — расход недели и сессия для карточки
            a["beat"] = time.time()
            fields = {"session": a["session"]}
            if a["week"] is not None:
                fields["week_now"] = a["week"]
            self.report(run["id"], **fields)

    def run_forever(self):
        log(f"исполнитель {VERSION}: {self.cfg['pult']['url']}, папки от {self.cfg['base']}")
        hello = True
        while True:
            try:
                self.tick(hello)
                hello = False
            except self.cli.PultError as e:
                log(f"Пульт: {e}")
                time.sleep(30)
            except Exception as e:  # исполнитель живёт долго: сбой одного шага не должен его ронять
                log(f"сбой: {type(e).__name__}: {e}")
                time.sleep(30)


def cmd_statusline(path):
    """Строка состояния сессии: Claude Code передаёт сюда JSON с rate_limits — исполнитель читает его из файла."""
    data = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    try:
        d = json.loads(data)
        week = ((d.get("rate_limits") or {}).get("seven_day") or {}).get("used_percentage")
        print(f"Пульт · {d.get('session_name') or ''}" + (f" · неделя {week:g}%" if week is not None else ""))
    except (ValueError, TypeError, AttributeError):
        print("Пульт")


def cmd_check(cfg):
    cli = cfg["cli"]
    me = cli.request(cfg["pult"], "GET", "/api/me")["me"]
    print(f"Пульт: {cfg['pult']['url']} — ключ модели «{me['name']}» ({me['id']})"
          + ("" if me["kind"] == "model" else " — НЕ модель: исполнителю нужен ключ модели"))
    rc, out = claude(cfg, "--version", timeout=60)
    print(f"claude: {out.strip() or 'не найден'}" + ("" if rc == 0 else f" (код {rc})"))
    print(f"папки проектов — от {cfg['base']}" + ("" if os.path.isdir(cfg["base"]) else " — ТАКОЙ ПАПКИ НЕТ"))
    st = cli.request(cfg["pult"], "GET", "/api/state")
    missing, untrusted = [], {}
    for p in st["projects"]:
        folder = folder_for(cfg, {"project": p["id"], "project_path": p["path"]})
        if not os.path.isdir(folder):
            missing.append(p["id"])
        elif not trusted(folder):
            untrusted.setdefault(git_root(folder) or folder, []).append(p["id"])
    print(f"проектов {len(st['projects'])}, папки нет у {len(missing)}" + (f": {', '.join(missing)}" if missing else ""))
    if untrusted:
        print(f"claude не доверяет {len(untrusted)} папкам — сессии там не запустятся. В каждой один раз: claude → "
              f"«Yes, proceed» → /exit (у git-репо — в его корне):")
        for folder, ids in sorted(untrusted.items()):
            print(f"  {folder}  ← {', '.join(ids)}")


def cmd_autostart(on):
    if os.name != "nt":
        raise SystemExit("autostart — только для Windows; на Linux и macOS — служба, см. docs/install.md")
    startup = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    lnk = os.path.join(startup, "Pult runner.lnk")
    if not on:
        if os.path.exists(lnk):
            os.remove(lnk)
        print("автозапуск снят")
        return
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    ps1 = os.path.join(WORK, "autostart.ps1")
    os.makedirs(WORK, exist_ok=True)
    with open(ps1, "w", encoding="utf-8-sig") as f:  # PowerShell 5.1 читает кириллицу в пути только с BOM
        f.write(f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}')\n"
                f"$s.TargetPath = '{pyw}'\n$s.Arguments = '\"{os.path.abspath(__file__)}\" run'\n"
                f"$s.WorkingDirectory = '{HERE}'\n$s.WindowStyle = 7\n$s.Description = 'Исполнитель Пульта'\n$s.Save()\n")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1], check=True,
                   creationflags=NO_WINDOW)
    os.remove(ps1)
    print(f"автозапуск: {lnk}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="pult_runner.py", description="Исполнитель Пульта: сессии ИИ по заявкам")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("check")
    au = sub.add_parser("autostart")
    au.add_argument("state", choices=("on", "off"))
    sl = sub.add_parser("statusline")
    sl.add_argument("file")
    args = p.parse_args(argv)
    if args.cmd == "statusline":
        return cmd_statusline(args.file)
    if args.cmd == "autostart":
        return cmd_autostart(args.state == "on")
    cfg = load_cfg()
    if args.cmd == "check":
        return cmd_check(cfg)
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        raise SystemExit(f"исполнитель уже работает (занят порт {LOCK_PORT})") from None
    try:
        Runner(cfg).run_forever()
    except KeyboardInterrupt:
        log("остановлен")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
