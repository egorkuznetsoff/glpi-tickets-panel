#!/usr/bin/env python3
"""
GLPI Agent — синхронизирует заявки с корпоративного GLPI на личную панель 24fox.ru/glpi/
Запускается на Windows 11, опрашивает GLPI каждые 30 минут.
"""
import urllib.request
import urllib.error
import json
import time
import os
import sys
from pathlib import Path

# ── Конфигурация (редактировать config.json) ──────────────────────────────────

BASE_DIR   = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "glpi_url":       "https://glpi.example.com/apirest.php",
    "glpi_app_token": "CHANGE_ME_GLPI_APP_TOKEN",
    "glpi_user_token":"CHANGE_ME_GLPI_USER_TOKEN",
    "panel_url":      "https://24fox.ru/glpi/api/sync",
    "panel_api_key":  "CHANGE_ME_PANEL_API_KEY",
    "locations":      ["RU0974", "RU0943", "RU0975"],
    "interval_sec":   1800
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
        except Exception as e:
            log(f"Ошибка чтения config.json: {e}")
    else:
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        log("Создан config.json с настройками по умолчанию")
    return DEFAULT_CONFIG

# ── Логирование ───────────────────────────────────────────────────────────────

LOG_FILE = BASE_DIR / "agent.log"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

# ── HTTP helpers (только stdlib) ──────────────────────────────────────────────

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_post(url, data, headers=None, timeout=30):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ── GLPI API ──────────────────────────────────────────────────────────────────

def glpi_headers(cfg, session_token=None):
    h = {"App-Token": cfg["glpi_app_token"], "Content-Type": "application/json"}
    if session_token:
        h["Session-Token"] = session_token
    return h

def init_session(cfg):
    url = cfg["glpi_url"] + "/initSession"
    h = {
        "Authorization": f"user_token {cfg['glpi_user_token']}",
        "App-Token": cfg["glpi_app_token"]
    }
    data = http_get(url, headers=h)
    return data["session_token"]

def kill_session(cfg, token):
    try:
        url = cfg["glpi_url"] + "/killSession"
        http_get(url, headers=glpi_headers(cfg, token))
    except:
        pass

def get_my_user_id(cfg, token):
    url = cfg["glpi_url"] + "/getFullSession"
    data = http_get(url, headers=glpi_headers(cfg, token))
    return data.get("glpiID") or data.get("session", {}).get("glpiID")

def search_tickets(cfg, token, criteria_params, label=""):
    """Поиск тикетов через GLPI search API."""
    fields = {
        "forcedisplay[0]": 2,   # ID
        "forcedisplay[1]": 1,   # Название
        "forcedisplay[2]": 12,  # Статус
        "forcedisplay[3]": 10,  # Приоритет
        "forcedisplay[4]": 15,  # Дата создания
        "forcedisplay[5]": 19,  # Дата изменения
        "forcedisplay[6]": 64,  # Имя заявителя
        "forcedisplay[7]": 83,  # Локация
        "range": "0-200",
        "sort": 19,
        "order": "DESC",
        **criteria_params
    }
    params = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in fields.items())
    url = cfg["glpi_url"] + f"/search/Ticket?{params}"
    try:
        data = http_get(url, headers=glpi_headers(cfg, token))
        rows = data.get("data") or []
        if label:
            log(f"  {label}: найдено {len(rows)} тикетов")
        return rows
    except Exception as e:
        log(f"  Ошибка поиска ({label}): {e}")
        return []

def parse_ticket(row, source):
    """Преобразует строку из GLPI search в наш формат."""
    return {
        "id":        row.get("2") or row.get(2, 0),
        "name":      row.get("1") or row.get(1, ""),
        "status":    row.get("12") or row.get(12, 0),
        "priority":  row.get("10") or row.get(10, 0),
        "date":      row.get("15") or row.get(15, ""),
        "date_mod":  row.get("19") or row.get(19, ""),
        "requester": row.get("64") or row.get(64, ""),
        "location":  row.get("83") or row.get(83, ""),
        "source":    source,
    }

# ── Основная синхронизация ────────────────────────────────────────────────────

import urllib.parse

def sync(cfg):
    log("=== Начало синхронизации ===")
    token = None
    try:
        token = init_session(cfg)
        log("GLPI: сессия открыта")

        my_id = get_my_user_id(cfg, token)
        log(f"GLPI: user_id = {my_id}")

        all_tickets = {}  # id -> ticket (дедупликация)

        # 1. Мои тикеты (где я заявитель)
        if my_id:
            rows = search_tickets(cfg, token, {
                "criteria[0][field]": 4,
                "criteria[0][link]": "AND",
                "criteria[0][searchtype]": "equals",
                "criteria[0][value]": my_id,
            }, label="Мои тикеты")
            for row in rows:
                t = parse_ticket(row, "mine")
                if t["id"]:
                    all_tickets[t["id"]] = t

        # 2. Тикеты по локациям (по полю локация + по содержанию)
        for i, loc in enumerate(cfg["locations"]):
            # По полю Location (field 83)
            rows = search_tickets(cfg, token, {
                "criteria[0][field]": 83,
                "criteria[0][link]": "AND",
                "criteria[0][searchtype]": "contains",
                "criteria[0][value]": loc,
            }, label=f"Локация {loc} (поле)")
            for row in rows:
                t = parse_ticket(row, loc)
                if t["id"] and t["id"] not in all_tickets:
                    all_tickets[t["id"]] = t

            # По содержанию тикета (field 21)
            rows = search_tickets(cfg, token, {
                "criteria[0][field]": 21,
                "criteria[0][link]": "AND",
                "criteria[0][searchtype]": "contains",
                "criteria[0][value]": loc,
            }, label=f"Локация {loc} (контент)")
            for row in rows:
                t = parse_ticket(row, loc)
                if t["id"] and t["id"] not in all_tickets:
                    all_tickets[t["id"]] = t

        tickets = list(all_tickets.values())
        log(f"Итого уникальных тикетов: {len(tickets)}")

        # Отправляем на панель
        result = http_post(
            cfg["panel_url"],
            {"tickets": tickets},
            headers={"X-Api-Key": cfg["panel_api_key"]}
        )
        log(f"Панель: {result}")

    except Exception as e:
        log(f"ОШИБКА синхронизации: {e}")
    finally:
        if token:
            kill_session(cfg, token)
            log("GLPI: сессия закрыта")
    log("=== Синхронизация завершена ===\n")

# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    log("GLPI Agent запущен")
    cfg = load_config()
    log(f"Интервал: {cfg['interval_sec']} сек ({cfg['interval_sec']//60} мин)")
    log(f"Локации: {', '.join(cfg['locations'])}")
    log(f"Панель: {cfg['panel_url']}")

    # Первый запуск сразу
    sync(cfg)

    while True:
        log(f"Следующая синхронизация через {cfg['interval_sec']//60} мин...")
        time.sleep(cfg["interval_sec"])
        cfg = load_config()  # перечитываем конфиг (позволяет менять на лету)
        sync(cfg)

if __name__ == "__main__":
    main()
