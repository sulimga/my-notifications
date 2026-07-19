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

SITE_COOKIE = os.environ.get("SITE_COOKIE", "PASTE_YOUR_COOKIE_STRING_HERE")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

# Базовий домен сайту-джерела - теж винесений у секрет/env, а не
# прописаний прямо в коді. Це зроблено спеціально для публічного
# репозиторію: щоб простий пошук на GitHub за назвою домену не
# знаходив цей код. Значення виду "https://example.com" (без
# слеша в кінці).
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "PASTE_YOUR_SITE_BASE_URL_HERE")
SITE_CABINET_URL = os.environ.get("SITE_CABINET_URL", "PASTE_YOUR_SITE_CABINET_URL_HERE")

# Як часто перевіряти (у хвилинах) - стосується ТІЛЬКИ локального
# режиму з нескінченним циклом. Для GitHub Actions інтервал
# налаштовується в файлі .github/workflows/monitor.yml
CHECK_INTERVAL_MINUTES = 15

# Параметри списку оголошень (як у запиті з браузера)
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
    "origin": SITE_CABINET_URL,
    "referer": f"{SITE_CABINET_URL}/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}

REPRESENTABILITY_OK = 1  # ти головний в агрегаторі


def fetch_offers() -> list[dict]:
    """
    Робить запити до сайту-джерела і повертає ПОВНИЙ список оголошень,
    автоматично проходячи всі сторінки пагінації (навіть якщо
    оголошень більше, ніж поміщається на одну сторінку).
    """
    headers = dict(HEADERS)
    headers["cookie"] = SITE_COOKIE

    all_items: list[dict] = []
    page = 1

    while True:
        params = dict(OFFERS_LIST_PARAMS)
        params["page"] = page

        response = requests.get(
            f"{SITE_BASE_URL}/api/offers/list/",
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
        log.info("Усі платні оголошення є представниками в агрегаторі. Сповіщення не потрібне.")
        return

    lines = [
        "⚠️ <b>Оголошення НЕ є представником в агрегаторі:</b>",
        "",
    ]
    for offer in not_representative:
        aggregator_url = offer.get("lunUrl", "")
        lines.append(
            f"❌ {format_offer_line(offer)}\n"
            f"<a href=\"{aggregator_url}\">Відкрити оголошення</a>"
        )

    message = "\n\n".join(lines)
    send_telegram_message(message)
    log.warning(
        "Надіслано сповіщення про %s оголошень без представництва.",
        len(not_representative),
    )


def notify_error(message: str) -> None:
    """
    Логує помилку і одразу пише про неї в Telegram - щоб мовчання
    скрипта завжди означало "все ок", а не "щось зламалось непомітно".
    """
    log.error(message)
    send_telegram_message(f"🔴 <b>Помилка моніторингу оголошень</b>\n{message}")


def _check_config() -> bool:
    placeholders_present = any(
        "PASTE_YOUR" in value
        for value in (
            SITE_COOKIE,
            TELEGRAM_BOT_TOKEN,
            SITE_BASE_URL,
            SITE_CABINET_URL,
        )
    )
    if placeholders_present:
        log.error(
            "Секрети не задані! Заповни SITE_COOKIE, TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_CHAT_ID, SITE_BASE_URL, SITE_CABINET_URL - або на "
            "початку файлу, або через змінні середовища / GitHub Secrets."
        )
        return False
    return True


def _describe_exception(exc: Exception) -> str:
    """Перетворює технічну помилку на зрозуміле повідомлення для людини."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in (401, 403):
            return (
                f"Сайт-джерело відповів помилкою авторизації (HTTP {status}).\n"
                "Найімовірніша причина - протух SITE_COOKIE.\n"
                "Онови його: зайди у свій кабінет, дістань свіжий cookie "
                "(DevTools -> Network -> Headers -> Cookie) і онови секрет "
                "SITE_COOKIE в GitHub → Settings → Secrets and variables → Actions."
            )
        return f"Сайт-джерело відповів помилкою HTTP {status}."

    if isinstance(exc, requests.RequestException):
        return f"Не вдалось з'єднатися з сайтом-джерелом: {exc}"

    return f"Неочікувана помилка в скрипті: {exc}"


def run_once() -> None:
    """Один прохід перевірки - саме цей режим використовує GitHub Actions."""
    if not _check_config():
        sys.exit(1)

    try:
        check_once()
        log.info("Перевірка завершена успішно.")
    except Exception as exc:  # noqa: BLE001 - навмисно широкий except, щоб нічого не пропустити
        notify_error(_describe_exception(exc))
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
        except Exception as exc:  # noqa: BLE001 - навмисно широкий except у циклі
            notify_error(_describe_exception(exc))

        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
