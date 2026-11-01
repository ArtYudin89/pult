#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Исполнитель Пульта: забирает заявки «Выполнить» своей модели и запускает по ним сессии Claude Code
или Codex на этом компьютере — там, где лежат файлы. Только стандартная библиотека, Python 3.9+.

    pult_runner.py run                  работать: ждать заявки и запускать сессии (Ctrl+C — выход)
    pult_runner.py check                проверить адрес и ключ, программу claude или codex и папки проектов
    pult_runner.py trust [--yes]        доверить claude папки проектов разом — «Yes, proceed» за каждую
    pult_runner.py autostart on|off     Windows: запуск при входе в систему (ярлык в «Автозагрузке»)
    pult_runner.py statusline ФАЙЛ      служебная: строка состояния сессии сохраняет данные в ФАЙЛ
    pult_runner.py --config ФАЙЛ …      другие настройки, например второго исполнителя — для Codex

Настройки — .local/runner.json рядом с программой (все поля необязательны):
    {"url": "https://…:9447", "token_file": "claude-runner.token",   ключ модели; иначе — из .local/config.json
     "engine": "claude",                 чьи сессии запускать: claude или codex
     "base": "C:\\work",                 от этой папки считается поле «папка» проекта; по умолчанию — папка
                                         над папкой Пульта
     "folders": {"site": "D:\\site"},    своя папка для отдельного проекта
     "claude": "claude", "codex": "codex", "python": "python", "permission_mode": "auto", "poll": 25}

Claude: claude --bg в папке проекта с моделью, усилием и режимом разрешений из заявки; продолжение —
claude --resume <сессия> --bg. Состояние — claude agents --json. Расход недели — из строки состояния самой
сессии (rate_limits.seven_day.used_percentage, только при входе по подписке). Это расход всего аккаунта:
в бюджет запуска попадает и работа человека в то же время. Вышло время, бюджет или потолок недели —
сессия останавливается (claude stop), разговор сохраняется, продолжить можно ответом. Серверы MCP проекта
(.mcp.json в папке и выше), о которых папка не ответила, сессия отклоняет — иначе встала бы на окне о них.

Codex: codex exec --json в папке проекта, продолжение — codex exec resume <сессия>; процесс держит сам
исполнитель, песочница и разрешения — из ~/.codex/config.toml, подтверждений не спрашивает (спросить некого).
Расхода недели у Codex нет — бюджет запуска только по времени. Модель Claude из уровней Пульта Codex не
передаётся — берётся его модель по умолчанию; усилие max — это xhigh. Без «codex» в настройках — codex.exe
из расширения VS Code, иначе codex из PATH. Второй исполнитель на том же компьютере — свой файл настроек со
своим ключом модели: pult_runner.py --config runner-codex.json run.
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
VERSION = "2026.11.01"
LOCK_PORTS = {"claude": 47447, "codex": 47448}  # второй исполнитель той же программы на этом компьютере не стартует
LOG_NAMES = {"claude": "runner.log", "codex": "runner-codex.log"}
LOG_NAME = LOG_NAMES["claude"]
CODEX_EFFORT = {"max": "xhigh"}  # у Codex усилия minimal, low, medium, high, xhigh
CLAUDE_MODEL = re.compile(r"(claude|opus|sonnet|haiku|fable)\b", re.I)
USAGE_FRESH_SEC = 6 * 3600   # расход недели старше этого для проверки потолка перед запуском не годится
START_GRACE_SEC = 60         # столько ждём, пока новая сессия появится в claude agents
CONFIG_LOCK_SEC = 10         # столько ждём, пока Claude Code допишет ~/.claude.json
WATCH_SEC = float(os.environ.get("PULT_RUNNER_TICK") or 10)  # как часто смотреть на идущую сессию
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg):
    line = f"{dt.datetime.now():%d.%m %H:%M:%S} {msg}"
    print(line, flush=True)  # под pythonw вывода нет — print молча ничего не делает
    try:
        os.makedirs(WORK, exist_ok=True)
        path = os.path.join(WORK, LOG_NAME)
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


def config_path(name):
    """Файл настроек: как указан или, если такого нет, — в .local/."""
    if not name:
        return os.path.join(LOCAL, "runner.json")
    return name if os.path.isabs(name) or os.path.exists(name) else os.path.join(LOCAL, name)


def find_codex():
    """codex.exe из расширения VS Code (самое новое) или codex из PATH."""
    found = sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".vscode", "extensions", "openai.chatgpt-*",
                                          "bin", "windows-x86_64", "codex.exe")))
    return found[-1] if found else "codex"


def load_cfg(path=None):
    sys.path.insert(0, HERE)
    import pult as cli  # адрес, ключ и запросы — как у pult.py
    path = config_path(path)
    if not os.path.exists(path) and path != config_path(None):
        raise SystemExit(f"нет файла настроек {path}")
    cfg = read_json(path) or {}
    engine = cfg.get("engine") or "claude"
    if engine not in LOCK_PORTS:
        raise SystemExit(f"{path}: engine — claude или codex, а не {engine!r}")
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
    return {"cli": cli, "pult": pult, "engine": engine, "path": path, "base": cfg.get("base") or os.path.dirname(HERE),
            "folders": cfg.get("folders") or {}, "claude": cfg.get("claude") or "claude",
            "codex": cfg.get("codex") or find_codex(),
            "python": cfg.get("python") or "python", "permission_mode": cfg.get("permission_mode") or "auto",
            "poll": int(cfg.get("poll") or 25)}


def folder_for(cfg, run):
    f = cfg["folders"].get(run.get("project") or "")
    if not f:
        rel = run.get("project_path") or ""
        f = os.path.join(cfg["base"], rel) if rel else cfg["base"]
    return os.path.normpath(f)


def child_env():
    # метки сессии, из которой запустили исполнителя (при проверке руками), сессиям не передаём
    return {k: v for k, v in os.environ.items()
            if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "PULT_SESSION",
                         "CODEX_THREAD_ID", "CODEX_SESSION_ID")}


def program(cfg, name):
    return cfg[name] if isinstance(cfg[name], list) else [cfg[name]]


def claude(cfg, *args, cwd=None, timeout=120, name="claude"):
    try:
        p = subprocess.run([*program(cfg, name), *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW, env=child_env())
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


# Запись папки в ~/.claude.json, какой её создаёт Claude Code 2.1.308; «Yes, proceed» — она же с доверием
NEW_PROJECT = {"allowedTools": [], "mcpContextUris": [], "mcpServers": {}, "enabledMcpjsonServers": [],
               "disabledMcpjsonServers": [], "hasTrustDialogAccepted": False,
               "hasClaudeMdExternalIncludesApproved": False, "hasClaudeMdExternalIncludesWarningShown": False}


def write_private(path, data):
    """Файл только для владельца: в ~/.claude.json учётная запись и настройки MCP-серверов с ключами."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def trust(folders, claude_json=None):
    """«Yes, proceed» сразу для нескольких папок — так, как это записывает сам Claude Code. На время правки
    берём его блокировку (папка <файл>.lock: занятую он ждёт). Ключ — путь с «/»: и как его видит исполнитель,
    и настоящий путь на диске — по нему сверяет фоновая сессия. Возвращает путь копии файла до правки."""
    path = claude_json or os.path.join(os.path.expanduser("~"), ".claude.json")
    lock, deadline = path + ".lock", time.time() + CONFIG_LOCK_SEC
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 10:  # брошенная: так её считает и Claude Code
                    os.rmdir(lock)
            except OSError:
                pass
            if time.time() > deadline:
                raise SystemExit(f"{path} занят другой программой — повторите позже") from None
            time.sleep(0.1)
    try:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as e:
            raise SystemExit(f"{path} не прочитан: {e}") from None
        projects = data.setdefault("projects", {})
        for key in dict.fromkeys(fwd(p).rstrip("/") for f in folders for p in (f, os.path.realpath(f))):
            projects[key] = {**projects.get(key, NEW_PROJECT), "hasTrustDialogAccepted": True}
        backups = os.path.join(os.path.dirname(path), ".claude", "backups")
        os.makedirs(backups, exist_ok=True)
        backup = os.path.join(backups, f"{os.path.basename(path)}.before-trust-{dt.datetime.now():%Y%m%d-%H%M%S}")
        write_private(backup, raw)
        tmp = path + ".trust"
        write_private(tmp, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))  # как пишет Claude Code
        for _ in range(50):
            try:
                os.replace(tmp, path)
                return backup
            except PermissionError:  # Windows: файл на миг открыт другой программой
                time.sleep(0.1)
        os.remove(tmp)
        raise SystemExit(f"{path} держит другая программа — ничего не изменено")
    finally:
        os.rmdir(lock)


def job_state(bg):
    """Запись фоновой службы Claude Code о сессии: там видно, что сессия застряла на окне при запуске."""
    st = read_json(os.path.join(os.path.expanduser("~"), ".claude", "jobs", bg, "state.json"))
    return {k: str(v) for k, v in st.items() if isinstance(v, (str, int, float))} if isinstance(st, dict) else {}


def startup_reason(folder, detail):
    """Почему сессия не дошла до работы — по записи о ней (detail): какое окно при запуске ждёт ответа."""
    head = f"Claude Code не запустился в {folder}: "
    if "MCP" in detail:
        return (head + f"при старте спрашивает, включать ли MCP-серверы проекта ({detail}). Откройте один раз claude "
                "в этой папке, ответьте в окне серверов и выйдите; потом — «Продолжить»")
    return (head + "ждёт подтверждения при старте — обычно доверия к папке" + (f" ({detail})" if detail else "")
            + ". Откройте один раз claude в этой папке или в папке выше, ответьте «Yes, proceed» и выйдите; "
            "потом — «Продолжить»")


def unanswered_mcp(folder):
    """Серверы MCP проекта, о которых папка не ответила, — с ними сессия встаёт на окне «N new MCP servers found
    in this project». Claude Code 2.1.309 берёт .mcp.json и из папок выше, а ответ — только из
    .claude/settings.local.json и settings.json самой папки: ответ папки выше не действует (проверено 01.11)."""
    servers, d = set(), os.path.abspath(folder)
    while True:
        m = read_json(os.path.join(d, ".mcp.json"))
        if isinstance(m, dict) and isinstance(m.get("mcpServers"), dict):
            servers |= set(m["mcpServers"])
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    for name in ("settings.local.json", "settings.json"):
        s = read_json(os.path.join(folder, ".claude", name))
        if not isinstance(s, dict):
            continue
        if s.get("enableAllProjectMcpServers") is True:
            return []
        servers -= set(s.get("enabledMcpjsonServers") or []) | set(s.get("disabledMcpjsonServers") or [])
    return sorted(servers)


def pid_alive(pid, image=""):
    """Жив ли процесс сессии Codex после перезапуска исполнителя. На Windows сверяем и имя программы:
    номер процесса мог достаться другой."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000 | 0x100000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not h:
        return False
    try:
        if k.WaitForSingleObject(h, 0) != 0x102:  # не WAIT_TIMEOUT — процесс уже вышел
            return False
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        if image and k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower() == os.path.basename(image).lower()
        return True
    finally:
        k.CloseHandle(h)


def kill_tree(pid):
    """Снять процесс вместе с дочерними: Codex запускает команды своими процессами."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.killpg(pid, 15)  # SIGTERM группе: процесс запущен в своей
        except OSError:
            pass


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
        self.active["files"] = (self.active["status_file"], self.active["settings_file"])
        log(f"#{rid} {run['task']}: подхватил сессию {bg} после перезапуска")

    def limits(self, run):
        return f"Пределы запуска: {run['minutes']} мин и {pct(run['budget'])} недельного лимита; дальше сессию остановят."

    def prompt(self, run):
        pult_py = fwd(os.path.join(HERE, "pult.py"))
        limits = self.limits(run)
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
        # git-репозиторий фоновая сессия иначе правит в отдельной копии (.claude/worktrees), а не в папке
        settings = {"statusLine": {"type": "command", "command": command}, "worktree": {"bgIsolation": "none"}}
        unanswered = unanswered_mcp(folder)
        if unanswered:  # иначе сессия встанет на окне о серверах, а ответить некому; отклонить — как Esc в нём
            settings["disabledMcpjsonServers"] = unanswered
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f)
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
                       "folder": folder, "files": (status_file, settings_file)}
        log(f"#{rid} {run['task']}: запущена сессия {m.group(1)} в {folder}"
            + (f"; серверы MCP без ответа отклонены: {', '.join(unanswered)}" if unanswered else ""))
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
        for f in a["files"]:
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
                    return self.stop(startup_reason(a["folder"], job.get("detail", "")), status="failed")
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
        log(f"исполнитель {VERSION} ({self.cfg.get('engine', 'claude')}): {self.cfg['pult']['url']}, "
            f"папки от {self.cfg['base']}")
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


class CodexRunner(Runner):
    """Сессии Codex: codex exec в папке проекта. Фонового режима у него нет — процесс держит исполнитель,
    события сессии читает из её вывода (--json), последний ответ — из файла (-o)."""

    def limits(self, run):
        return f"Предел запуска: {run['minutes']} мин; дальше сессию остановят."

    def args(self, run, last_file):
        args = ["exec"]
        if run.get("resume_of") and run.get("session"):
            args += ["resume", run["session"]]
        args += ["--json", "--skip-git-repo-check", "-o", last_file, "-c", 'approval_policy="never"']
        model, effort = run.get("model") or "", run.get("effort") or ""
        if model and not CLAUDE_MODEL.match(model):  # уровни Пульта общие, там может стоять модель Claude
            args += ["-m", model]
        if effort:
            args += ["-c", f'model_reasoning_effort="{CODEX_EFFORT.get(effort, effort)}"']
        return args + ["-"]  # поручение — со стандартного ввода: в командной строке Windows кириллица ломается

    def start(self, run):
        rid = run["id"]
        folder = folder_for(self.cfg, run)
        if not os.path.isdir(folder):
            log(f"#{rid} {run['task']}: нет папки {folder}")
            return self.report(rid, status="failed", reason=f"нет папки проекта на этом компьютере: {folder}")
        os.makedirs(WORK, exist_ok=True)
        a = self.codex_state(run, 0, folder, time.time())
        with open(a["prompt"], "w", encoding="utf-8", newline="") as f:  # без \r\n в поручении
            f.write(self.prompt(run))
        if run.get("model") and CLAUDE_MODEL.match(run["model"]):
            log(f"#{rid} {run['task']}: модель {run['model']} — не для Codex, беру его модель по умолчанию")
        detach = {"creationflags": NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            with open(a["prompt"], "rb") as i, open(a["events"], "wb") as o, open(a["err"], "wb") as e:
                proc = subprocess.Popen([*program(self.cfg, "codex"), *self.args(run, a["last"])], cwd=folder,
                                        stdin=i, stdout=o, stderr=e, env=child_env(), **detach)
        except OSError as e:
            log(f"#{rid} {run['task']}: codex не запустился: {e}")
            return self.report(rid, status="failed", reason=f"codex не запустился: {e}")
        a.update(pid=proc.pid, proc=proc, bg=f"codex:{proc.pid}")
        self.active = a
        log(f"#{rid} {run['task']}: запущен codex (процесс {proc.pid}) в {folder}")
        self.report(rid, status="working", bg=a["bg"], folder=folder)

    def codex_state(self, run, pid, folder, started):
        stem = os.path.join(WORK, f"run-{run['id']}")
        a = {"run": run, "bg": f"codex:{pid}", "pid": pid, "proc": None, "folder": folder, "started": started,
             "session": run.get("session") or "", "week": None, "pos": 0, "text": "", "completed": False, "error": "",
             "beat": 0.0, "prompt": stem + ".prompt.txt", "events": stem + ".events.jsonl", "last": stem + ".last.txt",
             "err": stem + ".err.txt"}
        a["files"] = (a["prompt"], a["events"], a["last"], a["err"])
        return a

    def adopt(self, run):
        """После перезапуска исполнителя: процесс codex ещё идёт — следим дальше по номеру процесса."""
        bg = run.get("bg") or ""
        pid = int(bg[6:]) if bg.startswith("codex:") and bg[6:].isdigit() else 0
        if self.active or not pid or not pid_alive(pid, program(self.cfg, "codex")[0]):
            return self.report(run["id"], status="failed", reason="исполнитель перезапускался, сессии уже нет")
        self.active = self.codex_state(run, pid, run.get("folder") or "",
                                       time.time() - minutes_since(run.get("started_at")) * 60)
        log(f"#{run['id']} {run['task']}: подхватил codex (процесс {pid}) после перезапуска")

    def read_status(self):
        """Новые события сессии: id (для продолжения), последний ответ, конец хода, ошибка."""
        a = self.active
        try:
            with open(a["events"], "rb") as f:
                f.seek(a["pos"])
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1  # недописанную строку — в следующий раз
        a["pos"] += end
        for line in chunk[:end].splitlines():
            try:
                e = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            kind, item, err = e.get("type"), e.get("item") or {}, e.get("error")
            if kind == "thread.started" and e.get("thread_id"):
                a["session"] = e["thread_id"]
            elif kind == "item.completed" and item.get("type") == "agent_message":
                a["text"] = item.get("text") or a["text"]
            elif kind == "turn.completed":
                a["completed"] = True
            elif kind in ("turn.failed", "error"):
                a["error"] = str((err.get("message") if isinstance(err, dict) else err) or e.get("message") or kind)

    def text(self):
        try:
            with open(self.active["last"], encoding="utf-8", errors="replace") as f:
                return f.read().strip() or self.active["text"]
        except OSError:
            return self.active["text"]

    def alive(self):
        a = self.active
        if a["proc"] is not None:
            return a["proc"].poll() is None
        return pid_alive(a["pid"], program(self.cfg, "codex")[0])

    def stop(self, reason, status="stopped"):
        a = self.active
        kill_tree(a["pid"])
        if a["proc"] is not None:
            try:
                a["proc"].wait(10)
            except subprocess.TimeoutExpired:
                pass
        self.finish(status, reason=reason, result=self.text())

    def watch(self):
        a, run = self.active, self.active["run"]
        self.read_status()
        if self.alive():
            if time.time() - a["started"] > run["minutes"] * 60:
                return self.stop(f"вышло время — {run['minutes']} мин")
            if time.time() - a["beat"] > 60:  # раз в минуту — id сессии для карточки
                a["beat"] = time.time()
                self.report(run["id"], session=a["session"])
            return
        self.read_status()  # процесс мог дописать события перед выходом
        text = self.text()
        if a["completed"]:
            q = question_of(text)
            return self.finish("asked" if q else "done", question=q, result=text)
        try:
            with open(a["err"], encoding="utf-8", errors="replace") as f:
                err = " / ".join(f.read().strip().splitlines()[-3:])[:400]
        except OSError:
            err = ""
        self.finish("failed", reason=f"codex: {a['error'] or err or 'сессия кончилась без ответа'}", result=text)


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


def folders_state(cfg, projects, claude_json=None):
    """Проекты без папки на этом компьютере и папки, которым claude не доверяет: {папка: [проекты]};
    у git-репо — его корень, доверие принимается там."""
    missing, untrusted = [], {}
    for p in projects:
        folder = folder_for(cfg, {"project": p["id"], "project_path": p["path"]})
        if not os.path.isdir(folder):
            missing.append(p["id"])
        elif not trusted(folder, claude_json):
            untrusted.setdefault(git_root(folder) or folder, []).append(p["id"])
    return missing, untrusted


def cmd_check(cfg):
    cli = cfg["cli"]
    me = cli.request(cfg["pult"], "GET", "/api/me")["me"]
    print(f"Пульт: {cfg['pult']['url']} — ключ модели «{me['name']}» ({me['id']})"
          + ("" if me["kind"] == "model" else " — НЕ модель: исполнителю нужен ключ модели"))
    engine = cfg["engine"]
    rc, out = claude(cfg, "--version", timeout=60, name=engine)
    print(f"{engine}: {out.strip().splitlines()[-1] if out.strip() else 'не найден'}" + ("" if rc == 0 else f" (код {rc})"))
    print(f"папки проектов — от {cfg['base']}" + ("" if os.path.isdir(cfg["base"]) else " — ТАКОЙ ПАПКИ НЕТ"))
    st = cli.request(cfg["pult"], "GET", "/api/state")
    missing, untrusted = folders_state(cfg, st["projects"])
    print(f"проектов {len(st['projects'])}, папки нет у {len(missing)}" + (f": {', '.join(missing)}" if missing else ""))
    if untrusted and engine == "claude":  # доверие папкам — забота Claude Code; Codex исполнитель запускает без него
        print(f"claude не доверяет {len(untrusted)} папкам — сессии там не запустятся. Доверить разом — "
              f"pult_runner.py trust; или в каждой один раз: claude → «Yes, proceed» → /exit (у git-репо — в его корне):")
        for folder, ids in sorted(untrusted.items()):
            print(f"  {folder}  ← {', '.join(ids)}")
    mcp = {}
    for p in st["projects"] if engine == "claude" else ():
        folder = folder_for(cfg, {"project": p["id"], "project_path": p["path"]})
        names = unanswered_mcp(folder) if os.path.isdir(folder) else []
        if names:
            mcp.setdefault((folder, ", ".join(names)), []).append(p["id"])
    if mcp:
        print(f"у {len(mcp)} папок серверы MCP проекта без ответа — сессия отклоняет их на время работы; "
              f"включить насовсем — claude в папке → в окне серверов отметить нужные → /exit:")
        for (folder, names), ids in sorted(mcp.items()):
            print(f"  {folder}  ← {', '.join(ids)}: {names}")


def cmd_trust(cfg, yes, claude_json=None):
    st = cfg["cli"].request(cfg["pult"], "GET", "/api/state")
    untrusted = folders_state(cfg, st["projects"], claude_json)[1]
    if not untrusted:
        print("claude доверяет всем папкам проектов — делать нечего")
        return
    print(f"claude не доверяет {len(untrusted)} папкам (у git-репо — его корень):")
    for folder, ids in sorted(untrusted.items()):
        print(f"  {folder}  ← {', '.join(ids)}")
    print("Доверить — то же, что «Yes, proceed» в каждой: в доверенной папке Claude Code без вопроса выполняет "
          "команды из её настроек (хуки в .claude/settings.json). Доверяйте только своим папкам.")
    try:
        ok = yes or input("Доверить все? [y/N] ").strip().lower() in ("y", "yes", "д", "да")
    except EOFError:  # без терминала — как «нет»
        ok = False
    if not ok:
        print("ничего не изменено")
        return
    backup = trust(sorted(untrusted), claude_json)
    left = [f for f in sorted(untrusted) if not trusted(f, claude_json)]
    print(f"доверено папок: {len(untrusted) - len(left)}; копия файла до правки — {backup}"
          + (f"; не записалось (файл переписала другая программа — повторите): {', '.join(left)}" if left else ""))


def cmd_autostart(on, config=None):
    if os.name != "nt":
        raise SystemExit("autostart — только для Windows; на Linux и macOS — служба, см. docs/install.md")
    startup = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    name = os.path.splitext(os.path.basename(config))[0] if config else ""
    lnk = os.path.join(startup, f"Pult runner ({name}).lnk" if name else "Pult runner.lnk")
    run_args = (f'--config "{os.path.abspath(config_path(config))}" ' if config else "") + "run"
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
                f"$s.TargetPath = '{pyw}'\n$s.Arguments = '\"{os.path.abspath(__file__)}\" {run_args}'\n"
                f"$s.WorkingDirectory = '{HERE}'\n$s.WindowStyle = 7\n$s.Description = 'Исполнитель Пульта'\n$s.Save()\n")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1], check=True,
                   creationflags=NO_WINDOW)
    os.remove(ps1)
    print(f"автозапуск: {lnk}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="pult_runner.py", description="Исполнитель Пульта: сессии ИИ по заявкам")
    p.add_argument("--config", help="файл настроек вместо .local/runner.json (путь или имя в .local/)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("check")
    tr = sub.add_parser("trust")
    tr.add_argument("--yes", action="store_true")
    au = sub.add_parser("autostart")
    au.add_argument("state", choices=("on", "off"))
    sl = sub.add_parser("statusline")
    sl.add_argument("file")
    args = p.parse_args(argv)
    if args.cmd == "statusline":
        return cmd_statusline(args.file)
    if args.cmd == "autostart":
        return cmd_autostart(args.state == "on", args.config)
    cfg = load_cfg(args.config)
    if args.cmd == "check":
        return cmd_check(cfg)
    if args.cmd == "trust":
        if cfg["engine"] != "claude":
            raise SystemExit("trust — доверие папкам для claude; исполнителю Codex оно не нужно")
        return cmd_trust(cfg, args.yes)
    global LOG_NAME
    LOG_NAME, port = LOG_NAMES[cfg["engine"]], LOCK_PORTS[cfg["engine"]]
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(f"исполнитель {cfg['engine']} уже работает (занят порт {port})") from None
    try:
        (CodexRunner if cfg["engine"] == "codex" else Runner)(cfg).run_forever()
    except KeyboardInterrupt:
        log("остановлен")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
