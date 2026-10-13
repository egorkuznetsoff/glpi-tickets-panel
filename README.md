# glpi-tickets-panel

Личная панель мониторинга корпоративных заявок **GLPI** (IT Helpdesk) с уведомлениями в Telegram о новых заявках.

## Архитектура

```
Рабочий ПК (Windows)                 VPS
┌────────────────────────┐          ┌──────────────────────────────┐
│ glpi_agent.ps1 / .py   │──POST──▶ │ nginx /glpi/ → :8087          │
│ опрашивает GLPI REST   │          │ panel.py (Flask)              │
│ API раз в 30 мин,      │ ◀──GET── │ хранит заявки в PostgreSQL    │
│ delta-sync             │          │ отдаёт веб-интерфейс          │
└────────────────────────┘          └──────────────┬───────────────┘
                                                   ▼
                                       Telegram Bot — уведомления
```

Агент нужен потому, что корпоративный GLPI доступен только из внутренней сети: агент сам отправляет данные на VPS.

## Стек

| Слой | Технология |
|---|---|
| Сервер | Python 3, Flask (порт 8087), PostgreSQL (`foxdb`) |
| Агент | PowerShell (`glpi_agent.ps1`) или Python (`glpi_agent.py`), запуск через `start.bat` |
| Интеграции | GLPI REST API (`apirest.php`), Telegram Bot API |
| Фронтенд | Vanilla JS, встроен в `panel.py` |

## Структура

```
panel.py              — сервер и веб-интерфейс
glpi-panel.service    — systemd unit
agent_src/            — исходники агента (ps1, py, start.bat, README.txt)
uploads/index.html    — форма создания заявок
```

## Развёртывание

**Сервер:**

1. Зависимости:
   ```bash
   sudo apt install python3-flask python3-psycopg2
   ```
2. PostgreSQL `foxdb`, пользователь `fox` — впишите пароль вместо `CHANGE_ME_DB_PASSWORD`.
3. Код и сервис:
   ```bash
   git clone https://github.com/egorkuznetsoff/glpi-tickets-panel.git /home/egor/glpi-panel
   sudo cp /home/egor/glpi-panel/glpi-panel.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now glpi-panel
   ```
4. nginx:
   ```nginx
   location ^~ /glpi/ { proxy_pass http://127.0.0.1:8087/; }
   ```
5. При первом запуске создаётся `config.json`. В нём генерируются `secret_key` и `api_key`, остальные поля — значения по умолчанию. Впишите туда `password`, `telegram_bot_token`, `telegram_chat_id`, `glpi_url`, `glpi_app_token` и `glpi_user_token`, затем перезапустите сервис: `sudo systemctl restart glpi-panel`.

**Агент (рабочий ПК):**

1. В `agent_src/glpi_agent.ps1` (или `.py`) подставьте `CHANGE_ME_GLPI_APP_TOKEN`, `CHANGE_ME_GLPI_USER_TOKEN` и `CHANGE_ME_PANEL_API_KEY` (тот же ключ, что в `config.json` панели).
2. Соберите ZIP для раздачи:
   ```bash
   cd agent_src && mkdir -p ../agent && python3 -c "import zipfile; z=zipfile.ZipFile('../agent/glpi-agent.zip','w',zipfile.ZIP_DEFLATED); [z.write(f) for f in ['glpi_agent.ps1','start.bat','README.txt']]"
   ```
3. На ПК запустите `start.bat` (можно добавить в автозагрузку).

## Секреты

Перед публикацией на GitHub все секреты заменены заглушками `CHANGE_ME_*`.
Перед запуском найдите их и подставьте свои значения:

```bash
grep -rn CHANGE_ME_ .
```

## Автор

**Егор Кузнецов** (Egor Kuznetsov) — идея, разработка, деплой и сопровождение.
GitHub: [@egorkuznetsoff](https://github.com/egorkuznetsoff) · 2026

Часть кода написана в паре с AI-ассистентом Claude (Anthropic).
Репозиторий приватный, все права защищены.
