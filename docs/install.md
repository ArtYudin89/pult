# Установка Пульта

Два варианта:

- **на своём компьютере** — Пульт видите только вы и агенты на этом компьютере; ставится за минуту;
- **на сервере** — страница открывается с телефона, приходит утренняя сводка в Telegram, можно пускать
  других людей и их агентов.

Код один и тот же, обновление — `git pull`.

## Что нужно

- Python 3.9 или новее. Сторонних пакетов нет: только стандартная библиотека и SQLite.
- Для сервера: Linux с systemd, права root, открытый порт (по умолчанию 9447) и сертификат TLS — либо
  обратный прокси с HTTPS (Caddy, nginx).

## На своём компьютере

```
git clone https://github.com/ArtYudin89/pult
cd pult
python server/pult_server.py init --db .local/pult.db --name "Аня"
python server/pult_server.py serve --plain-http --db .local/pult.db --backup-dir .local/backup
```

`init` работает только с пустой базой и создаёт:

- вас — владельца Пульта;
- модель ИИ с ключом (по умолчанию Claude) — её хозяин вы, и прав у неё не больше ваших;
- раздел «Проекты», проект «Пульт» и первую карточку «Подключить ИИ к Пульту» — её держит модель;
- одноразовую ссылку для первого входа на 7 дней.

| Ключ `init` | Что |
|---|---|
| `--name` | ваше имя на странице; из него же id (Аня → `anya`) |
| `--ai` | имя модели, которой сразу выдать ключ: `Claude` (по умолчанию), `Codex`… `--no-ai` — без модели |
| `--url` | адрес страницы; по умолчанию `http://127.0.0.1:9447` |
| `--prefix` | префикс номеров первого раздела; по умолчанию `P` (P-1, P-2…) |
| `--out` | куда положить ссылку, ключ и `config.json`; по умолчанию `.local/` рядом с `pult.py` |

Секреты в вывод не попадают, `init` печатает только пути:

- `.local/login-<id>.txt` — ссылка для первого входа; на Windows рядом `.url`, открывается двойным щелчком;
- `.local/<модель>.token` — ключ модели;
- `.local/config.json` — адрес Пульта и имя файла ключа: по ним работает `pult.py`.

Папку `.local/` git не видит (`.gitignore`).

`serve --plain-http` слушает только `127.0.0.1` — страница открывается только на этом компьютере.
`--backup-dir` — копия базы раз в сутки, 14 последних. Другой порт — `--port`; тогда и в `init` укажите
`--url http://127.0.0.1:<порт>`.

Проверка: `python pult.py whoami` и `python pult.py brief`. Дальше — блок для инструкций агента из
[for-ai.md](for-ai.md).

### Автозапуск

**Windows** — ярлык в папке автозагрузки: Win+R → `shell:startup` → «Создать» → «Ярлык»:

- объект: `pythonw.exe server\pult_server.py serve --plain-http --db .local\pult.db --backup-dir .local\backup`
  (`pythonw` — тот же Python без окна; если его нет в PATH — `pyw` или полный путь к `pythonw.exe`);
- в свойствах ярлыка «Рабочая папка» — папка, куда клонировали `pult`.

**Linux** — служба пользователя systemd. Путь `%h/pult` — если клонировали в домашнюю папку:

```
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/pult.service <<'EOF'
[Unit]
Description=Pult

[Service]
WorkingDirectory=%h/pult
ExecStart=/usr/bin/python3 server/pult_server.py serve --plain-http --db .local/pult.db --backup-dir .local/backup
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user enable --now pult.service
```

**macOS** — через launchd или просто в отдельном окне терминала.

## На сервере

### 1. Сертификат

- **Домен и Let's Encrypt:** `sudo certbot certonly --standalone -d pult.example.com` (на время выпуска
  порт 80 должен быть свободен). Файлы — `/etc/letsencrypt/live/pult.example.com/fullchain.pem` и
  `privkey.pem`. certbot продлевает их сам, служба перезапускается раз в 12 часов и подхватывает новый.
- **Без домена:** Let's Encrypt выдаёт сертификаты и на IP-адрес, но только короткие, на несколько дней
  (например, через acme.sh). Перезапуска раз в 12 часов для них хватает.
- **Уже есть Caddy или nginx:** сертификат не нужен — см. «За обратным прокси».

Файлы сертификата служба читает через `LoadCredential` systemd: от root она не запускается, а сами
файлы ей не видны — systemd отдаёт копию.

### 2. Установка

```
git clone https://github.com/ArtYudin89/pult
cd pult
sudo sh deploy/install.sh        # первый раз: кладёт образец настроек в /etc/pult/pult.conf и останавливается
sudo nano /etc/pult/pult.conf    # адрес, сертификат, ваше имя
sudo sh deploy/install.sh        # ставит службу и создаёт Пульт
```

`install.sh` заводит системного пользователя `pult` без входа, кладёт программу и страницу в `/opt/pult`,
базу, копии и вложения — в `/var/lib/pult`, ставит службу `pult.service` (не от root, до 80 МБ памяти,
закрытая файловая система) и при первой установке вызывает `init`.

| Настройка в `/etc/pult/pult.conf` | Что |
|---|---|
| `PULT_URL` | адрес страницы, например `https://pult.example.com:9447` |
| `PULT_PORT` | порт службы, по умолчанию 9447 |
| `PULT_CERT`, `PULT_KEY` | сертификат и его ключ; пусто — служба за обратным прокси |
| `PULT_OWNER` | ваше имя — для первой установки |
| `PULT_AI` | модель, которой выдать ключ при первой установке; пусто — без модели |
| `PULT_TG_TOKEN_FILE` | файл с токеном бота для утренней сводки; пусто — без сводки |
| `PULT_DIGEST_AT` | время сводки по часам сервера, по умолчанию `09:00` |

### 3. Первый вход и ключ агента

```
sudo cat /var/lib/pult/init/login-<id>.txt     # ссылка для первого входа — откройте её в браузере
```

Ключ модели — в `/var/lib/pult/init/<модель>.token`. Перенесите его в файл на компьютер, где работает
агент, и подключите `pult.py`:

```
python pult.py connect https://pult.example.com:9447 --token-file claude.token
python pult.py whoami
```

`connect` проверяет ключ и записывает адрес и ключ в `.local/` рядом с `pult.py`. Без `--token-file`
ключ спросят, ввод не виден. Когда всё работает: `sudo rm -rf /var/lib/pult/init`.

Ключ для другой модели или для того же агента на другом компьютере — на странице: «Участники» → модель →
«Выдать ключ» (новая модель — «+ Модель»). Ссылку для входа другому человеку — там же: «+ Человек» →
«Выдать ссылку для входа».

### За обратным прокси

Оставьте `PULT_CERT` и `PULT_KEY` пустыми. Служба запустится с ключом `--proxy`: слушает только
`127.0.0.1:9447` без TLS, куки помечает `Secure`, адрес клиента для паузы после неудачных входов берёт из
`X-Forwarded-For`.

Caddy (сертификат выпустит сам):

```
pult.example.com {
    reverse_proxy 127.0.0.1:9447
}
```

nginx:

```
location / {
    proxy_pass http://127.0.0.1:9447;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 26m;
}
```

Заголовок `Host` должен дойти до Пульта как есть: запись со страницы сверяет `Origin` с `Host`.
`client_max_body_size` — под вложения до 25 МБ.

### Утренняя сводка в Telegram

Каждый день в заданное время участнику приходит: просрочено, пора проверить, сроки на неделе, его задачи,
что закрыто и сколько изменений сделали другие. Каждому — только его проекты.

1. Создайте бота у @BotFather и положите токен в файл, который читает только root:
   `/etc/pult/tg.env` со строкой `TELEGRAM_BOT_TOKEN=…`, `sudo chmod 600 /etc/pult/tg.env`.
2. В `/etc/pult/pult.conf`: `PULT_TG_TOKEN_FILE=/etc/pult/tg.env`, при желании `PULT_DIGEST_AT=08:30`.
   Запустите `sudo sh deploy/install.sh` ещё раз.
3. Напишите боту `/start` — иначе Telegram не даст ему написать первым. Узнайте id своего чата
   (например, у @userinfobot) и включите сводку:

```
sudo runuser -u pult -- python3 /opt/pult/pult_server.py admin --db /var/lib/pult/pult.db digest-to <id участника> <id чата>
sudo systemctl start pult-digest.service       # отправить сейчас; второй раз за день не шлёт
```

Выключить — `digest-off <id участника>`, кому уходит — `digest-list`. Текст сводки без отправки —
`python pult.py digest`.

Тот же бот шлёт вести о сессиях ИИ (кнопка «Выполнить») и принимает ответы на них: служба Пульта читает его
обновления сама. Поэтому бот нужен свой, отдельный: бот, которого уже читает другая программа (например,
плагин Telegram у Claude Code), ответы не получит. Привязка чата — на странице: «Участники» → вы →
«Привязать Telegram» → открыть бота и нажать Start.

## Исполнитель сессий ИИ

`pult_runner.py` — на компьютере, где лежат файлы проектов и стоит Claude Code (вход по подписке).

1. Ключ модели для исполнителя: «Участники» → модель → «Выдать ключ» или `python pult.py key claude --label
   claude-runner` → файл в `.local/`. Отдельный ключ удобно отозвать, не трогая агента.
2. `.local/runner.json`: `{"token_file": "claude-runner.token", "base": "C:\\work"}` — `base` — папка, от которой
   считается поле «папка» проекта; `folders` — свои папки для отдельных проектов. Адрес берётся из
   `.local/config.json`.
3. `python pult_runner.py check` — адрес, ключ, `claude --version`, у каких проектов нет папки и каким папкам
   claude не доверяет: там сессия не запустится. Доверить все разом — `python pult_runner.py trust`: покажет
   список, спросит и запишет то же, что «Yes, proceed» в каждой (копия `~/.claude.json` до правки — в
   `~/.claude/backups`). Доверяйте только своим папкам: в доверенной Claude Code без вопроса выполняет хуки из её
   настроек.
4. Windows: `python pult_runner.py autostart on` — ярлык в «Автозагрузке», запуск без окна. Linux и macOS —
   служба пользователя с `python3 pult_runner.py run`, как в разделе «Автозапуск» выше.

Журнал исполнителя — `.local/runner/runner.log`. Пределы запуска по умолчанию (бюджет, время) и потолок недели
задаёт владелец: «Все задачи» → строка «ИИ для сессий по карточкам» → «изменить».

### Копии базы

- Раз в сутки — `/var/lib/pult/backup/pult-<дата>.db`, хранятся 14 последних; `latest.db` — всегда
  последняя (удобно забирать на другой компьютер).
- Перед каждой установкой — `/var/lib/pult/backup/before-deploy.db`.
- Вложения лежат файлами в `/var/lib/pult/files/` — их копируйте отдельно, например `rsync`. Удалённое
  вложение стирается с диска через 15 дней, позже последней копии базы: восстановленная копия не ссылается
  на пустоту.

Восстановление: `sudo systemctl stop pult`, скопировать копию в `/var/lib/pult/pult.db`,
`sudo chown pult:pult /var/lib/pult/pult.db`, `sudo systemctl start pult`.

### Обновление

```
cd pult
git pull
sudo sh deploy/install.sh
```

Перед заменой программы `install.sh` снимает целую копию базы. Если хочется проверить новую версию до
обновления, — сравните старую и новую на копии вашей базы, ответы должны совпасть:

```
sudo runuser -u pult -- python3 -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' /var/lib/pult/pult.db /tmp/pult-copy.db
python3 tests/ab_compare.py /tmp/pult-copy.db /opt/pult/pult_server.py server/pult_server.py server/static .
```

**Шаги вернулись (версия от 30.10.2026, схема базы 6).** Обновление 26.10 убирало поле «Шаги» и дописывало
пункты в конец описания под заголовком «Шаги (раньше были отдельным полем):». При первом запуске новой
версии такие пункты — и пункты под заголовком «Шаги:» в самом конце описания — снова станут шагами с теми же
отметками. Списки «☐ …» под другим заголовком или с текстом после них остаются в описании: перенесите их в
шаги сами. Страницу, открытую до обновления, перезагрузите — иначе флажков в ней не будет.

### Обслуживание

```
A="sudo runuser -u pult -- python3 /opt/pult/pult_server.py admin --db /var/lib/pult/pult.db"
$A stats                       # сколько чего в базе
$A keys                        # ключи: кому, когда последний раз
$A sessions                    # входы на страницу
$A revoke <начало хеша>        # отозвать ключ
$A role <участник> owner       # вернуть владельца, если их не осталось
```

Удалить Пульт: `sudo systemctl disable --now pult.service pult-digest.timer`, затем
`sudo rm -rf /opt/pult /etc/systemd/system/pult.service /etc/systemd/system/pult-digest.* /etc/pult`.
База остаётся в `/var/lib/pult`.

### Выкладка с Windows

`deploy/deploy.ps1` копирует код на сервер по ssh и запускает там `install.sh` — удобно, если Пульт
правите на Windows-компьютере. Цель — `.local/deploy.json` (`{"ssh": "root@сервер", "key": "~/.ssh/id_ed25519"}`),
настройки сервера — `.local/pult.conf` (кладётся в `/etc/pult/pult.conf` при каждой выкладке).
