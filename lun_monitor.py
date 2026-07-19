"""
LUN Representability Monitor
=============================

Скрипт перевіряє список твоїх ПЛАТНИХ оголошень на rieltor.ua і
дивиться на поле "representability":
    1 -> ти представник на ЛУН (є медалька)
    2 (або будь-що інше, крім 1) -> тебе перебили конкуренти

ЛОГІКА СПОВІЩЕНЬ (проста і надійна):
    - Якщо ВСІ платні оголошення є представниками -> сповіщення
      НЕ надсилається (тиша).
    - Якщо ХОЧА Б ОДНЕ платне оголошення НЕ є представником ->
      надсилається сповіщення в Telegram зі списком таких оголошень.
    - Це відбувається на КОЖНІЙ перевірці, а не тільки при зміні
      статусу. Тобто поки оголошення лишається "не представником",
      сповіщення надходитиме щоразу (кожні ~15 хв, чи як налаштовано
      розклад у GitHub Actions) - це зроблено НАВМИСНО, щоб не
      пропустити ситуацію, коли тебе перебили і встигли повернути
      позицію назад між двома перевірками.
    - Безкоштовні оголошення (де немає платної ставки, dailyCost=0)
      ІГНОРУЮТЬСЯ - по них немає сенсу перевіряти представництво,
      бо там немає аукціону ставок.

НАЛАШТУВАННЯ (обов'язково заповни перед запуском):
    1. RIELTOR_COOKIE     - рядок cookies з твого браузера (див. інструкцію нижче)
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

import os
import sys
import time
import logging

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
    "limit": 100,   # максимум оголошень на одній сторінці запиту
    "status": 10,   # 10 = активні оголошення
    "mode": 10,
    # itemType навмисно НЕ вказаний - тоді API повертає всі типи
    # нерухомості одразу (квартири, будинки, комерцію), а не тільки
    # один конкретний тип.
}

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


def fetch_offers() -> list[dict]:
    """
    Робить запити до rieltor.ua і повертає ПОВНИЙ список оголошень,
    автоматично проходячи всі сторінки пагінації (навіть якщо
    оголошень більше, ніж поміщається на одну сторінку).
    """
    headers = dict(HEADERS)
    headers["cookie"] = RIELTOR_COOKIE

    all_items: list[dict] = []
    page = 1

    while True:
        params = dict(OFFERS_LIST_PARAMS)
        params["page"] = page

        response = requests.get(
            OFFERS_LIST_URL,
            params=params,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        if payload.get("status") != "OK":
            raise RuntimeError(f"Неочікувана відповідь API: {payload}")

        data = payload["data"]
        all_items.extend(data["items"])

        pagination = data.get("pagination", {})
        page_count = pagination.get("pageCount", 1)

        log.info(
            "Завантажено сторінку %s з %s (%s оголошень на ній).",
            page,
            page_count,
            len(data["items"]),
        )

        if page >= page_count:
            break
        page += 1

    return all_items


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


def is_paid_offer(offer: dict) -> bool:
    """
    Платне оголошення визначаємо по dailyCost > 0.
    Безкоштовні (тариф "free") мають dailyCost == 0 - там немає
    аукціону ставок, тому representability для них не перевіряємо.
    """
    return (offer.get("dailyCost") or 0) > 0


def check_once() -> None:
    """
    Один прохід перевірки: тягне ВЕСЬ список оголошень і, якщо серед
    платних є хоч одне НЕ представник - надсилає одне сповіщення
    в Telegram зі списком усіх таких оголошень. Якщо всі платні
    оголошення представники - нічого не надсилає.
    """
    offers = fetch_offers()

    not_representative = []

    for offer in offers:
        if not is_paid_offer(offer):
            log.info("%s -> (безкоштовне, пропускаємо)", format_offer_line(offer))
            continue

        representability = offer.get("representability")
        is_representative = representability == REPRESENTABILITY_OK

        log.info(
            "%s -> %s",
            format_offer_line(offer),
            "✅ представник" if is_representative else "❌ НЕ представник",
        )

        if not is_representative:
            not_representative.append(offer)

    if not not_representative:
        log.info("Усі платні оголошення є представниками на ЛУН. Сповіщення не потрібне.")
        return

    lines = [
        "⚠️ <b>Оголошення НЕ є представником на ЛУН:</b>",
        "",
    ]
    for offer in not_representative:
        lun_url = offer.get("lunUrl", "")
        lines.append(
            f"❌ {format_offer_line(offer)}\n"
            f"<a href=\"{lun_url}\">Відкрити на ЛУН</a>"
        )

    message = "\n\n".join(lines)
    send_telegram_message(message)
    log.warning(
        "Надіслано сповіщення про %s оголошень без представництва.",
        len(not_representative),
    )


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

    try:
        check_once()
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

    while True:
        try:
            check_once()
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
