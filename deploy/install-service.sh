#!/usr/bin/env bash
# Установка бота как systemd-сервиса (автозапуск при загрузке и перезапуск при падении).
#
# Запуск из папки репозитория:
#   sudo bash deploy/install-service.sh
#
# Параметры (все необязательные):
#   --name NAME   имя сервиса (по умолчанию: имя папки репозитория)
#   --user USER   от чьего имени запускать бота (по умолчанию: тот, кто вызвал sudo)
#   --uninstall   остановить и удалить сервис
#
# Бот запускается из этой же папки, поэтому кнопка «Обновление бота» (git pull)
# продолжает работать. Пользователь USER должен иметь доступ к репозиторию на git pull.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="$(basename "$PROJECT_DIR")"
RUN_USER="${SUDO_USER:-}"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)      SERVICE_NAME="${2:?не указано имя}"; shift 2 ;;
        --user)      RUN_USER="${2:?не указан пользователь}"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)           echo "Неизвестный параметр: $1" >&2; exit 1 ;;
    esac
done

# Имя сервиса: только безопасные символы
SERVICE_NAME="$(echo "$SERVICE_NAME" | tr -c 'A-Za-z0-9_.\n-' '-')"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите через sudo: sudo bash deploy/install-service.sh" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd не найден на этой системе." >&2
    exit 1
fi

if [[ $UNINSTALL -eq 1 ]]; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    echo "Сервис ${SERVICE_NAME} удалён."
    exit 0
fi

if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    echo "Укажите обычного пользователя: --user ИМЯ (запускать бота от root не стоит)." >&2
    exit 1
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "Пользователь $RUN_USER не существует." >&2
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/bot.py" ]]; then
    echo "Не нашёл bot.py в $PROJECT_DIR" >&2
    exit 1
fi
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "Нет файла .env в $PROJECT_DIR. Создайте его (BOT_TOKEN, ADMIN_IDS, ...) и запустите скрипт снова." >&2
    exit 1
fi

# Права на папку: бот пишет туда базу, а обновление меняет файлы
chown -R "$RUN_USER":"$(id -gn "$RUN_USER")" "$PROJECT_DIR"
chmod 600 "$PROJECT_DIR/.env"

# Виртуальное окружение и зависимости
if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
    echo "Создаю виртуальное окружение..."
    sudo -u "$RUN_USER" python3 -m venv "$PROJECT_DIR/venv"
fi
echo "Ставлю зависимости..."
sudo -u "$RUN_USER" "$PROJECT_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$RUN_USER" "$PROJECT_DIR/venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# Юнит systemd
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Telegram-бот (${SERVICE_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/venv/bin/python ${PROJECT_DIR}/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
sleep 2

echo
systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
cat <<EOF

Готово. Полезные команды:
  sudo systemctl status ${SERVICE_NAME}      # состояние
  sudo systemctl restart ${SERVICE_NAME}     # перезапуск
  sudo journalctl -u ${SERVICE_NAME} -f      # логи в реальном времени
  sudo bash deploy/install-service.sh --uninstall --name ${SERVICE_NAME}   # удалить
EOF
