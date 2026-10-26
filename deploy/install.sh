#!/bin/sh
# Установка и обновление Пульта на сервере Linux с systemd. Запускать от root из папки с кодом:
#     sudo sh deploy/install.sh
# Настройки — /etc/pult/pult.conf: первый запуск кладёт туда образец и останавливается. Повторный запуск
# безопасен: обновляет программу, страницу и службы, перед заменой программы снимает копию базы.
# Трогает только /opt/pult, /var/lib/pult, /etc/pult и службы pult.service, pult-digest.*.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
CONF=${PULT_CONF:-/etc/pult/pult.conf}
APP=/opt/pult
DATA=/var/lib/pult
DB=$DATA/pult.db

if [ ! -f "$CONF" ]; then
    install -d -m 0755 "$(dirname "$CONF")"
    install -m 0644 "$SRC/deploy/pult.conf.example" "$CONF"
    echo "Образец настроек — в $CONF: впишите адрес, сертификат и своё имя, потом запустите установку снова."
    exit 1
fi
PULT_PORT=9447 PULT_CERT= PULT_KEY= PULT_OWNER= PULT_AI= PULT_TG_TOKEN_FILE= PULT_DIGEST_AT=09:00
. "$CONF"
: "${PULT_URL:?в $CONF не задан PULT_URL}"
case "$PULT_URL" in *example.com*) echo "в $CONF адрес из образца — впишите свой"; exit 1 ;; esac
if [ -n "$PULT_CERT" ] && { [ ! -f "$PULT_CERT" ] || [ ! -f "$PULT_KEY" ]; }; then
    echo "нет файла сертификата или ключа: $PULT_CERT, $PULT_KEY"
    exit 1
fi
if [ -n "$PULT_TG_TOKEN_FILE" ] && [ ! -f "$PULT_TG_TOKEN_FILE" ]; then
    echo "нет файла с токеном бота: $PULT_TG_TOKEN_FILE"
    exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "нужен Python 3.9 или новее"; exit 1; }

id pult >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin pult
install -d -o root -g root -m 0755 "$APP" "$APP/static" "$APP/static/fonts"
install -d -o pult -g pult -m 0700 "$DATA"
# Целая копия базы до замены программы: новая версия может перевести схему уже при первом запуске.
if [ -f "$DB" ]; then
    install -d -o pult -g pult -m 0700 "$DATA/backup"
    runuser -u pult -- python3 -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' "$DB" "$DATA/backup/before-deploy.db"
    echo "копия базы до выкладки: $DATA/backup/before-deploy.db"
fi
install -m 0644 "$SRC/server/pult_server.py" "$APP/pult_server.py"
for f in "$SRC"/server/static/*; do
    if [ -f "$f" ]; then install -m 0644 "$f" "$APP/static/"; fi
done
for f in "$SRC"/server/static/fonts/*; do install -m 0644 "$f" "$APP/static/fonts/"; done
# убрать файлы страницы, которых больше нет в выкладке
for f in "$APP"/static/*; do
    if [ -f "$f" ] && [ ! -f "$SRC/server/static/${f##*/}" ]; then rm -f "$f"; fi
done

render() {  # шаблон deploy/<юнит>.in → /etc/systemd/system/<юнит>
    if [ -n "$PULT_CERT" ]; then tls='s|^@TLS@||'; plain=''; else tls='/^@TLS@/d'; plain=' --proxy'; fi
    sed -e "$tls" -e "s|@PLAIN@|$plain|" -e "s|@PORT@|$PULT_PORT|g" -e "s|@CERT@|$PULT_CERT|g" \
        -e "s|@KEY@|$PULT_KEY|g" -e "s|@URL@|$PULT_URL|g" -e "s|@TG@|$PULT_TG_TOKEN_FILE|g" \
        -e "s|@DIGEST_AT@|$PULT_DIGEST_AT|g" "$SRC/deploy/$1.in" > "/etc/systemd/system/$1"
    chmod 0644 "/etc/systemd/system/$1"
}
render pult.service
if [ -n "$PULT_TG_TOKEN_FILE" ]; then
    render pult-digest.service
    render pult-digest.timer
fi

if [ ! -f "$DB" ] && [ -n "$PULT_OWNER" ]; then
    install -d -o pult -g pult -m 0700 "$DATA/init"
    set -- --db "$DB" --name "$PULT_OWNER" --url "$PULT_URL" --out "$DATA/init"
    if [ -n "$PULT_AI" ]; then set -- "$@" --ai "$PULT_AI"; else set -- "$@" --no-ai; fi
    runuser -u pult -- python3 "$APP/pult_server.py" init "$@"
fi
if [ -f "$SRC/keys_public.json" ]; then
    runuser -u pult -- python3 "$APP/pult_server.py" admin --db "$DB" load-keys "$SRC/keys_public.json"
fi

systemctl daemon-reload
systemctl enable pult.service >/dev/null 2>&1
systemctl restart pult.service
if [ -n "$PULT_TG_TOKEN_FILE" ]; then
    systemctl enable --now pult-digest.timer >/dev/null 2>&1
elif [ -f /etc/systemd/system/pult-digest.timer ]; then
    systemctl disable --now pult-digest.timer >/dev/null 2>&1 || true
fi
sleep 2
echo "служба: $(systemctl is-active pult.service), память: $(systemctl show pult.service -p MemoryCurrent --value)"
if [ -n "$PULT_TG_TOKEN_FILE" ]; then
    echo "сводка: таймер $(systemctl is-active pult-digest.timer), следующая $(systemctl show pult-digest.timer -p NextElapseUSecRealtime --value)"
    echo "кому: $(runuser -u pult -- python3 "$APP/pult_server.py" admin --db "$DB" digest-list | tr '\n' ';')"
fi
if command -v curl >/dev/null 2>&1; then
    # порт открывается не сразу: при старте сервер ещё и переводит базу на новую схему
    i=0
    while [ $i -lt 15 ] && ! curl -sk -o /dev/null "$PULT_URL/"; do i=$((i + 1)); sleep 1; done
    curl -s -o /dev/null -w "страница $PULT_URL с проверкой сертификата: %{http_code}\n" "$PULT_URL/" || true
    curl -s -o /dev/null -w "API без ключа: %{http_code} (ждём 401)\n" "$PULT_URL/api/state" || true
fi
if [ -d "$DATA/init" ]; then
    echo "первый вход: ссылка и ключ модели — в $DATA/init/ (перенесите ключ к ИИ и удалите папку)"
fi
