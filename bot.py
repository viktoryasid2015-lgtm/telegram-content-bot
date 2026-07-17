"""
Telegram Content Bot
=====================
Раз в CHECK_INTERVAL секунд проверяет Google-таблицу.
Если наступило время публикации (совпадает День недели + Время из таблицы
и пост ещё не публиковался — колонка "Статус" пустая):
  1. Отправляет русский текст + картинку (колонка "Медиа") в чат "Chat ID".
  2. Переводит текст на украинский (бесплатный Google Translate).
  3. Отправляет перевод + картинку (колонка "Медиа УКР") в чат "Chat ID УКР".
  4. Записывает результат в колонку "Статус".
Каждое воскресенье поздно вечером колонка "Статус" очищается,
чтобы на следующей неделе тот же план публикаций повторился заново.

Дополнительно: бот отвечает фиксированным предупреждением, если ему написали
в личку или тегнули (@username) в группе — на украинском, если чат где-либо
в таблице указан как "Chat ID УКР", иначе на русском. На обычные сообщения
в группе и на ответы на опубликованные посты бота не реагирует.

Структура таблицы (первая строка — заголовки):
A: Дата           (день недели, например "Понедельник")
B: Время          (например "9:00")
C: Chat ID        (для русской версии)
D: Thread ID      (ID темы в группе для русской версии; пусто — если тем нет)
E: Chat ID УКР    (для украинской версии)
F: Thread ID УКР  (ID темы в группе для украинской версии; пусто — если тем нет)
G: Текст          (только на русском)
H: Медиа          (ссылка на картинку/файл на Google Drive, русская версия)
I: Медиа УКР      (ссылка на картинку/файл на Google Drive, украинская версия)
J: Статус         (сюда бот сам пишет результат публикации)
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz
import requests
import gspread
from google.oauth2.service_account import Credentials
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("content-bot")

# ---------- Настройки из переменных окружения ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"].strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "Лист1").strip()
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Kiev")
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# ---------- Автоответ на входящие сообщения ----------
NOTICE_RU = (
    "⚠️ Важно!\n\n"
    "Данный бот не осуществляет поддержку пользователей и не занимается решением вопросов.\n\n"
    "Для получения помощи, пожалуйста, обращайтесь к вашему куратору."
)
try:
    NOTICE_UKR = GoogleTranslator(source="ru", target="uk").translate(NOTICE_RU)
except Exception:
    NOTICE_UKR = NOTICE_RU  # если перевод не удался при старте — лучше на русском, чем ничего

TZ = pytz.timezone(TIMEZONE)

# Названия дней недели как обычно пишут в таблице (Python: Monday=0)
WEEKDAY_NAMES = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}

# ---------- Подключение к Google Sheets ----------
def get_worksheet():
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_NAME)


# ---------- Вспомогательные функции ----------
def drive_link_to_direct(url: str) -> str | None:
    """Превращает ссылку вида .../d/FILE_ID/view в прямую ссылку на файл."""
    if not url:
        return None
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        return url  # уже похоже на прямую ссылку — пробуем как есть
    file_id = match.group(1)
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def parse_time_cell(value: str):
    """'9:00' -> (9, 0). Возвращает None, если не получилось распарсить."""
    try:
        h, m = value.strip().split(":")
        return int(h), int(m)
    except Exception:
        return None


def parse_multi(value: str):
    """Разбивает строку по запятой в список, СОХРАНЯЯ пустые позиции
    (нужно для сопоставления Chat ID и Thread ID по порядку)."""
    if not value:
        return []
    return [v.strip() for v in value.split(",")]


def send_to_group_list(chat_ids_raw: str, thread_ids_raw: str, text: str, media_url: str | None):
    """Отправляет один и тот же пост во все группы, перечисленные через запятую
    в chat_ids_raw. Thread ID берётся из той же позиции в thread_ids_raw
    (если позиция пустая или её нет — пост уйдёт в общий чат без темы)."""
    chat_list = parse_multi(chat_ids_raw)
    thread_list = parse_multi(thread_ids_raw)
    summaries = []
    for i, chat_id in enumerate(chat_list):
        if not chat_id:
            continue
        thread_id = thread_list[i] if i < len(thread_list) else None
        ok, info = send_telegram(chat_id, text, media_url, thread_id)
        summaries.append(f"{chat_id}:{'OK' if ok else 'FAIL'}({info})")
    return summaries


def send_telegram(chat_id: str, text: str, media_url: str | None, thread_id: str | None = None):
    """Отправляет сообщение (с картинкой, если она есть и доступна).
    thread_id — ID темы (topic) внутри группы; если пусто, шлёт в общий чат."""
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    extra = {}
    if thread_id and str(thread_id).strip():
        extra["message_thread_id"] = str(thread_id).strip()

    if media_url:
        resp = requests.post(
            f"{base}/sendPhoto",
            data={"chat_id": chat_id, "caption": text[:1024], **extra},
            params={"photo": media_url},
            timeout=30,
        )
        if resp.ok and resp.json().get("ok"):
            # если текст длиннее лимита подписи к фото — дошлём остаток отдельным сообщением
            if len(text) > 1024:
                requests.post(f"{base}/sendMessage",
                               data={"chat_id": chat_id, "text": text[1024:], **extra},
                               timeout=30)
            return True, "фото+текст"
        else:
            log.warning("Не удалось отправить фото (%s), отправляю только текст: %s",
                        media_url, resp.text[:300])

    # запасной вариант — просто текст
    resp = requests.post(f"{base}/sendMessage",
                          data={"chat_id": chat_id, "text": text, **extra},
                          timeout=30)
    ok = resp.ok and resp.json().get("ok")
    return ok, ("только текст" if ok else f"ОШИБКА: {resp.text[:200]}")


def collect_ukr_chat_ids(ws) -> set:
    """Собирает все Chat ID из столбца 'Chat ID УКР' по всей таблице —
    нужно, чтобы понимать, в какой чат отвечать на украинском."""
    rows = ws.get_all_values()
    ids = set()
    for row in rows[1:]:
        row = row + [""] * (10 - len(row))
        chat_id_ukr = row[4]
        for cid in parse_multi(chat_id_ukr):
            if cid:
                ids.add(cid.strip())
    return ids


def get_bot_identity():
    """Узнаёт собственный ID и username бота — нужно, чтобы понимать,
    когда его тегнули или ответили на его сообщение."""
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    resp = requests.get(f"{base}/getMe", timeout=15)
    result = resp.json().get("result", {})
    return result.get("id"), (result.get("username") or "").lower()


def is_bot_addressed(msg: dict, bot_id, bot_username: str) -> bool:
    """True, если сообщение — личка или тег бота (@username) в группе.
    Ответы на опубликованные посты бота НЕ считаются обращением к боту."""
    chat = msg.get("chat", {})
    if chat.get("type") == "private":
        return True

    # тег @username в тексте или подписи
    text = msg.get("text") or msg.get("caption") or ""
    entities = msg.get("entities") or msg.get("caption_entities") or []
    for ent in entities:
        if ent.get("type") == "mention":
            mention = text[ent["offset"]: ent["offset"] + ent["length"]].lstrip("@").lower()
            if mention == bot_username:
                return True
        if ent.get("type") == "text_mention" and ent.get("user", {}).get("id") == bot_id:
            return True

    return False


def get_initial_offset() -> int:
    """Узнаёт номер последнего уже случившегося апдейта, чтобы при старте
    бот не начал отвечать на старые сообщения из истории."""
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        resp = requests.get(f"{base}/getUpdates", params={"offset": -1, "timeout": 0}, timeout=15)
        result = resp.json().get("result", [])
        if result:
            return result[-1]["update_id"] + 1
    except Exception as e:
        log.warning("Не удалось получить начальный offset: %s", e)
    return 0


def message_listener(shared_state: dict):
    """Работает в фоне постоянно: слушает входящие сообщения и отвечает
    фиксированным предупреждением — но только если это личка боту, его
    тегнули (@username), или ответили на его сообщение.
    Язык ответа: украинский, если чат из списка украинских групп, иначе русский."""
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    offset = get_initial_offset()
    bot_id, bot_username = get_bot_identity()
    log.info("Слушатель входящих сообщений запущен (бот: @%s, id %s).", bot_username, bot_id)

    while True:
        try:
            resp = requests.get(
                f"{base}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=35,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue  # это служебный апдейт (добавление в группу и т.п.), пропускаем
                frm = msg.get("from", {})
                if frm.get("is_bot"):
                    continue  # не отвечаем другим ботам (и самим себе)

                if not is_bot_addressed(msg, bot_id, bot_username):
                    continue  # не личка, не тег и не ответ боту — пропускаем

                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                thread_id = msg.get("message_thread_id")

                text = NOTICE_UKR if chat_id in shared_state.get("ukr_chat_ids", set()) else NOTICE_RU
                ok, info = send_telegram(chat_id, text, None, thread_id)
                log.info("Автоответ в чат %s: %s (%s)", chat_id, "OK" if ok else "FAIL", info)
        except Exception as e:
            log.exception("Ошибка в слушателе сообщений: %s", e)
            time.sleep(5)


# ---------- Основная логика ----------
def process_due_posts(ws):
    rows = ws.get_all_values()  # список списков, включая заголовок
    now = datetime.now(TZ)
    today_name = WEEKDAY_NAMES[now.weekday()]

    for idx, row in enumerate(rows[1:], start=2):  # строки таблицы, start=2 т.к. 1 — заголовок
        row = row + [""] * (10 - len(row))  # на случай если строка короче 10 колонок
        (date_cell, time_cell, chat_id_ru, thread_id_ru, chat_id_ukr, thread_id_ukr,
         text_ru, media_ru, media_ukr, status) = row[:10]

        if status.strip():
            continue  # уже опубликовано на этой неделе
        if date_cell.strip() != today_name:
            continue
        parsed = parse_time_cell(time_cell)
        if not parsed:
            continue
        hour, minute = parsed
        if now.hour != hour or now.minute != minute:
            continue
        if not text_ru.strip():
            continue

        log.info("Публикую строку %s (%s %s)", idx, date_cell, time_cell)
        results = []

        # --- Русская версия (может быть несколько групп через запятую) ---
        if chat_id_ru.strip():
            ru_summaries = send_to_group_list(
                chat_id_ru, thread_id_ru, text_ru.strip(), drive_link_to_direct(media_ru.strip())
            )
            results.append("RU: " + (", ".join(ru_summaries) if ru_summaries else "нет групп"))
        else:
            results.append("RU: пропущено (нет Chat ID)")

        # --- Украинская версия (перевод, тоже может быть несколько групп) ---
        if chat_id_ukr.strip():
            try:
                text_ukr = GoogleTranslator(source="ru", target="uk").translate(text_ru.strip())
            except Exception as e:
                log.error("Ошибка перевода: %s", e)
                text_ukr = text_ru.strip()  # если перевод не удался — публикуем как есть
            ukr_summaries = send_to_group_list(
                chat_id_ukr, thread_id_ukr, text_ukr, drive_link_to_direct(media_ukr.strip())
            )
            results.append("UKR: " + (", ".join(ukr_summaries) if ukr_summaries else "нет групп"))
        else:
            results.append("UKR: пропущено (нет Chat ID)")

        status_text = f"{now.strftime('%d.%m %H:%M')} — " + "; ".join(results)
        ws.update_cell(idx, 10, status_text)  # колонка J = Статус
        log.info("Статус записан: %s", status_text)


def maybe_weekly_reset(ws, state: dict):
    """Каждое воскресенье поздно вечером очищает колонку Статус."""
    now = datetime.now(TZ)
    today_key = now.strftime("%Y-%m-%d")

    if now.weekday() == 6 and now.hour == 23 and state.get("last_reset_date") != today_key:
        rows = ws.get_all_values()
        n_rows = len(rows)
        if n_rows > 1:
            log.info("Воскресенье, %s строк — очищаю колонку Статус", n_rows - 1)
            cell_range = f"J2:J{n_rows}"
            ws.update(cell_range, [[""] for _ in range(n_rows - 1)])
        state["last_reset_date"] = today_key


def run_health_server():
    """Простейший веб-сервер, отвечающий 'OK' на любой запрос.
    Нужен только для Render.com (или похожих хостингов), которые держат
    бесплатным исключительно 'веб-сервисы' — сервис должен отвечать на HTTP,
    иначе платформа сочтёт его background worker и не даст бесплатный тариф.
    Внешний пинг-сервис (например UptimeRobot) должен стучаться сюда
    каждые ~10 минут, чтобы Render не "усыпил" бота из-за неактивности."""
    port = int(os.environ.get("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Бот работает.".encode("utf-8"))

        def do_HEAD(self):
            # UptimeRobot и некоторые другие пинг-сервисы шлют HEAD, а не GET —
            # без этого метода сервер отвечал 501 Not Implemented.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()

        def log_message(self, format, *args):
            pass  # не засоряем логи каждым health-check запросом

    server = HTTPServer(("0.0.0.0", port), Handler)
    log.info("Health-check сервер запущен на порту %s", port)
    server.serve_forever()


def main():
    log.info("Бот запускается. Часовой пояс: %s, интервал проверки: %s сек.",
              TIMEZONE, CHECK_INTERVAL)
    ws = get_worksheet()
    log.info("Подключение к таблице успешно: %s", ws.title)

    state = {"last_reset_date": None}
    shared_state = {"ukr_chat_ids": collect_ukr_chat_ids(ws)}

    listener_thread = threading.Thread(target=message_listener, args=(shared_state,), daemon=True)
    listener_thread.start()

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    while True:
        try:
            process_due_posts(ws)
            maybe_weekly_reset(ws, state)
            shared_state["ukr_chat_ids"] = collect_ukr_chat_ids(ws)
        except Exception as e:
            log.exception("Ошибка в основном цикле: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
