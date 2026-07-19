"""
LUN Representability Monitor
=============================

Скрипт періодично перевіряє список твоїх оголошень на rieltor.ua
і слідкує за полем "representability":
    1 -> ти представник на ЛУН (є медалька)
    2 (або будь-що інше, крім 1) -> тебе перебили конкуренти

При зміні статусу з "представник" на "не представник" надсилає
сповіщення в Telegram.

НАЛАШТУВАННЯ (обов'язково заповни перед запуском):
    1. RIELTOR_COOKIE   - рядок cookies з твого браузера (див. інструкцію нижче)
    2. TELEGRAM_BOT_TOKEN - токен твого Telegram-бота
    3. TELEGRAM_CHAT_ID   - твій chat_id (кому надсилати повідомлення)

ЯК ОТРИМАТИ RIELTOR_COOKIE:
    1. Зайди в кабінет my.rieltor.ua під своїм акаунтом.
    2. Відкрий DevTools (F12) -> вкладка Network -> Fetch/XHR.
    3. Онови сторінку, клікни на будь-який запит до rieltor.ua
       (наприклад "list/" або "users/info/").
    4. У вкладці Headers знайди розділ "Request Headers" -> "cookie".
    5. Скопіюй ВЕСЬ рядок cookie (він довгий, це нормально) і встав
       нижче в змінну RIELTOR_COOKIE.

    !! Це чутливі дані, що дають доступ до твого акаунту.
       Нікому їх не показуй і не публікуй в репозиторіях.

ЯК ОТРИМАТИ TELEGRAM BOT TOKEN І CHAT ID:
    1. Напиши @BotFather в Telegram -> /newbot -> отримаєш токен.
    2. Напиши своєму новому боту будь-яке повідомлення (просто "hi").
    3. Відкрий у браузері:
       https://api.telegram.org/bot<ТВІЙ_ТОКЕН>/getUpdates
       і знайди там "chat":{"id": ЦИФРИ, ...} - це і є твій chat_id.

ДВА РЕЖИМИ ЗАПУСКУ:

    1) Локально, для тесту (постійний цикл, працює поки відкритий термінал):
        pip install -r requirements.txt --break-system-packages
        python3 lun_monitor.py

    2) Одноразово (для GitHub Actions чи Windows Task Scheduler,
       які самі викликають скрипт за розкладом):
        python3 lun_monitor.py --once

У режимі --once секретні дані (cookie, токен бота, chat_id) беруться
зі змінних середовища (environment variables) - RIELTOR_COOKIE,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Це потрібно для GitHub Actions,
щоб не зберігати паролі прямо в коді.

Для локального запуску (режим 1) можна або так само задати змінні
середовища, або просто вписати значення в константи нижче.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import requests

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================
# Спочатку скрипт шукає значення в змінних середовища (env vars) -
# це потрібно для GitHub Actions. Якщо їх немає - бере значення
# нижче (зручно для локального тесту у VSCode).

RIELTOR_COOKIE = os.environ.get("RIELTOR_COOKIE", "PASTE_YOUR_COOKIE_STRING_HERE")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

# Як часто перевіряти (у хвилинах) - стосується ТІЛЬКИ локального
# режиму з нескінченним циклом. Для GitHub Actions інтервал
# налаштовується в файлі .github/workflows/monitor.yml
CHECK_INTERVAL_MINUTES = 15

# Параметри списку оголошень (як у запиті з браузера)
OFFERS_LIST_URL = "https://rieltor.ua/api/offers/list/"
OFFERS_LIST_PARAMS = {
    "page": 1,
    "limit": 25,
    "status": 10,   # 10 = активні оголошення
    "itemType": 1,  # 1 = квартири
    "mode": 10,
}

# Файл, де зберігається попередній стан оголошень (щоб не спамити
# повідомленнями і бачити тільки ЗМІНИ статусу)
STATE_FILE = Path(__file__).parent / "lun_monitor_state.json"

# ============================================================
# Технічна частина - зазвичай не потребує змін
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("lun_monitor")

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "uk-UA",
    "origin": "https://my.rieltor.ua",
    "referer": "https://my.rieltor.ua/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}

REPRESENTABILITY_OK = 1  # ти головний на ЛУН


def load_state() -> dict:
    """Завантажує попередній збережений стан оголошень з диску."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Не вдалось прочитати файл стану: %s", exc)
    return {}


def save_state(state: dict) -> None:
    """Зберігає поточний стан оголошень на диск."""
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_offers() -> list[dict]:
    """Робить запит до rieltor.ua і повертає список оголошень."""
    headers = dict(HEADERS)
    headers["cookie"] = RIELTOR_COOKIE

    response = requests.get(
        OFFERS_LIST_URL,
        params=OFFERS_LIST_PARAMS,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    if payload.get("status") != "OK":
        raise RuntimeError(f"Неочікувана відповідь API: {payload}")

    return payload["data"]["items"]


def send_telegram_message(text: str) -> None:
    """Надсилає повідомлення в Telegram через Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Не вдалось надіслати повідомлення в Telegram: %s", exc)


def format_offer_line(offer: dict) -> str:
    return f"{offer['address']} ({offer['price']})"


def check_once(previous_state: dict) -> dict:
    """
    Робить один прохід перевірки: тягне список оголошень, порівнює
    зі старим станом, надсилає сповіщення при зміні, повертає новий стан.
    """
    offers = fetch_offers()
    new_state = {}
    changes = []

    for offer in offers:
        offer_id = str(offer["id"])
        representability = offer.get("representability")
        is_representative = representability == REPRESENTABILITY_OK

        new_state[offer_id] = {
            "address": offer["address"],
            "price": offer["price"],
            "representability": representability,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

        prev = previous_state.get(offer_id)
        prev_representability = prev["representability"] if prev else None

        # Перше запам'ятовування стану - не сповіщаємо, просто фіксуємо
        if prev is None:
            log.info(
                "%s -> %s",
                format_offer_line(offer),
                "✅ представник" if is_representative else "❌ НЕ представник",
            )
            continue

        # Зміна: був представником, а тепер ні
        if prev_representability == REPRESENTABILITY_OK and not is_representative:
            changes.append(
                f"❌ <b>Втратив позицію представника на ЛУН</b>\n"
                f"{format_offer_line(offer)}\n"
                f"<a href=\"{offer.get('lunUrl', '')}\">Відкрити на ЛУН</a>"
            )
            log.warning("ВТРАТА позиції: %s", format_offer_line(offer))

        # Зміна: не був представником, а тепер знову так
        elif prev_representability != REPRESENTABILITY_OK and is_representative:
            changes.append(
                f"✅ <b>Знову представник на ЛУН</b>\n"
                f"{format_offer_line(offer)}"
            )
            log.info("Відновлення позиції: %s", format_offer_line(offer))

    if changes:
        message = "\n\n".join(changes)
        send_telegram_message(message)

    return new_state


def _check_config() -> bool:
    if "PASTE_YOUR" in RIELTOR_COOKIE or "PASTE_YOUR" in TELEGRAM_BOT_TOKEN:
        log.error(
            "Секрети не задані! Заповни RIELTOR_COOKIE, TELEGRAM_BOT_TOKEN "
            "і TELEGRAM_CHAT_ID - або на початку файлу, або через "
            "змінні середовища / GitHub Secrets."
        )
        return False
    return True


def run_once() -> None:
    """Один прохід перевірки - саме цей режим використовує GitHub Actions."""
    if not _check_config():
        sys.exit(1)

    state = load_state()
    try:
        state = check_once(state)
        save_state(state)
        log.info("Перевірка завершена успішно.")
    except requests.RequestException as exc:
        log.error("Помилка запиту до rieltor.ua: %s", exc)
        sys.exit(1)


def run_forever() -> None:
    """Нескінченний цикл - для локального запуску у VSCode."""
    if not _check_config():
        return

    log.info(
        "Старт моніторингу. Перевірка кожні %s хв. Ctrl+C для зупинки.",
        CHECK_INTERVAL_MINUTES,
    )

    state = load_state()

    while True:
        try:
            state = check_once(state)
            save_state(state)
        except requests.RequestException as exc:
            log.error("Помилка запиту до rieltor.ua: %s", exc)
        except Exception as exc:  # noqa: BLE001 - навмисно широкий except у циклі
            log.exception("Неочікувана помилка: %s", exc)

        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
