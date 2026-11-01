# -*- coding: utf-8 -*-
"""Поддельный codex для проверки исполнителя Пульта: exec и exec resume с --json и -o, --version.

Что делает сессия, решает метка в поручении (оно приходит со стандартного ввода):
    [done]      ответила и закончила ход (по умолчанию)
    [question]  закончила вопросом «ВОПРОС: …»
    [slow]      работает, пока не остановят
    [fail]      ход оборвался ошибкой
Сессии лежат файлами в FAKE_CODEX_DIR, все вызовы дописываются в FAKE_CODEX_DIR/calls.jsonl.
"""
import json
import os
import sys
import time
import uuid

DIR = os.environ["FAKE_CODEX_DIR"]
os.makedirs(DIR, exist_ok=True)


def event(e):
    sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv):
    if argv[:1] == ["--version"]:
        print("codex-cli 0.0.0-fake")
        return 0
    prompt = sys.stdin.buffer.read().decode("utf-8") if argv[-1:] == ["-"] else ""
    with open(os.path.join(DIR, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": prompt}, ensure_ascii=False) + "\n")
    if argv[:1] != ["exec"]:
        print("fake codex: only exec", file=sys.stderr)
        return 2
    sid = argv[2] if argv[1:2] == ["resume"] else str(uuid.uuid4())
    last = argv[argv.index("-o") + 1]
    event({"type": "thread.started", "thread_id": sid})
    event({"type": "turn.started"})
    if "[slow]" in prompt:
        with open(os.path.join(DIR, sid + ".pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        while True:
            time.sleep(0.1)
    if "[fail]" in prompt:
        event({"type": "error", "message": "stream disconnected before completion"})
        event({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
        return 1
    text = "Какого цвета кнопка?\n\nВОПРОС: Какого цвета кнопка?" if "[question]" in prompt else "Сделано: поле добавлено"
    event({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    with open(last, "w", encoding="utf-8") as f:
        f.write(text)
    event({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
