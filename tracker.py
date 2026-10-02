"""Трекер новых версий приложений в RuStore.

Раз в день проверяет приложения из config.json. Если вышла новая версия,
отправляет одно письмо-сводку и дописывает строку в releases.csv.

Запуск:
    python tracker.py               обычная проверка
    python tracker.py --dry-run     показать письмо в консоли, ничего не слать и не сохранять
    python tracker.py --test-email  отправить тестовое сообщение с текущими версиями
    python tracker.py --debug       напечатать сырой ответ RuStore (для отладки)

Куда отправлять — задаётся переменными окружения:
    Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (несколько чатов через запятую)
    или почта: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_TO
"""

import argparse
import csv
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"
CSV_FILE = ROOT / "releases.csv"

API_URL = "https://backapi.rustore.ru/applicationData/overallInfo/{package}"
PAGE_URL = "https://www.rustore.ru/catalog/app/{package}/versions"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

DEBUG = False


# ---------- получение данных из RuStore ----------

def http_get(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_from_api(package):
    """Основной источник: внутренний JSON-адрес RuStore."""
    raw = http_get(API_URL.format(package=package))
    if DEBUG:
        print(f"--- API {package} ---\n{raw[:2000]}\n")
    body = json.loads(raw).get("body") or {}
    version = body.get("versionName")
    if not version:
        raise ValueError("в ответе API нет поля versionName")
    return {
        "version": str(version),
        "date": str(body.get("appVerUpdatedAt") or "")[:10],
        "whats_new": (body.get("whatsNew") or "").strip(),
    }


def fetch_from_page(package):
    """Запасной источник: страница истории версий, ищем данные в её коде."""
    raw = http_get(PAGE_URL.format(package=package))
    if DEBUG:
        print(f"--- PAGE {package} ---\n{raw[:2000]}\n")
    for text in (raw, raw.replace('\\"', '"').replace("\\\\", "\\")):
        version = re.search(r'"versionName"\s*:\s*"([^"]+)"', text)
        if not version:
            continue
        whats_new = re.search(r'"whatsNew"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        date = re.search(r'"appVerUpdatedAt"\s*:\s*"([^"]+)"', text)
        return {
            "version": version.group(1),
            "date": date.group(1)[:10] if date else "",
            "whats_new": _unescape(whats_new.group(1)) if whats_new else "",
        }
    raise ValueError("на странице не нашлось данных о версии")


def _unescape(value):
    try:
        return json.loads(f'"{value}"').strip()
    except json.JSONDecodeError:
        return value.strip()


def fetch_app(package):
    errors = []
    for source in (fetch_from_api, fetch_from_page):
        try:
            return source(package)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"{source.__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


# ---------- файлы состояния ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def append_csv(rows):
    is_new = not CSV_FILE.exists()
    # utf-8-sig, чтобы Excel правильно показывал кириллицу
    with CSV_FILE.open("a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        if is_new:
            writer.writerow(["Дата проверки", "Приложение", "Пакет", "Версия", "Дата релиза", "Что нового"])
        writer.writerows(rows)


# ---------- письмо ----------

def build_email(releases, errors, test=False):
    short = ", ".join(f"{r['name']} {r['version']}" for r in releases)
    subject = ("[ТЕСТ] " if test else "") + f"RuStore: новые версии — {short}"

    text_parts, html_parts = [], []
    for r in releases:
        link = PAGE_URL.format(package=r["package"])
        notes = r["whats_new"] or "Описание не указано."
        text_parts.append(
            f"{r['name']} — версия {r['version']} ({r['date'] or 'дата не указана'})\n"
            f"{notes}\n{link}\n"
        )
        html_parts.append(
            f"<h3 style='margin:24px 0 4px'>{html.escape(r['name'])} — {html.escape(r['version'])}</h3>"
            f"<p style='margin:0 0 8px;color:#666'>{html.escape(r['date'] or 'дата не указана')}</p>"
            f"<p style='margin:0 0 8px;white-space:pre-wrap'>{html.escape(notes)}</p>"
            f"<p style='margin:0'><a href='{link}'>История версий в RuStore</a></p>"
        )
    if errors:
        text_parts.append("Не удалось проверить:\n" + "\n".join(f"- {e}" for e in errors))
        html_parts.append(
            "<h3 style='margin:24px 0 4px'>Не удалось проверить</h3><ul>"
            + "".join(f"<li>{html.escape(e)}</li>" for e in errors)
            + "</ul>"
        )

    message = EmailMessage()
    message["Subject"] = subject
    message.set_content("\n".join(text_parts))
    message.add_alternative(
        "<div style='font-family:Arial,sans-serif;font-size:15px;line-height:1.45'>"
        + "".join(html_parts)
        + "</div>",
        subtype="html",
    )
    return message


def send_email(message):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipients = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    message["From"] = user
    message["To"] = ", ".join(recipients)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(user, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(message)
    print(f"Письмо отправлено: {', '.join(recipients)}")


def send_telegram(releases, errors, test=False):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_ids = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    messages = []
    for r in releases:
        text = (
            ("[ТЕСТ] " if test else "")
            + f"{r['name']} — версия {r['version']} ({r['date'] or 'дата не указана'})\n\n"
            + (r["whats_new"] or "Описание не указано.")
        )
        link = "\n\n" + PAGE_URL.format(package=r["package"])
        messages.append(text[: 4000 - len(link)] + link)  # лимит Telegram — 4096 символов
    if errors:
        messages.append(("Не удалось проверить:\n" + "\n".join(f"- {e}" for e in errors))[:4000])

    for chat_id in chat_ids:
        for text in messages:
            data = json.dumps(
                {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            ).encode("utf-8")
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(request, timeout=30).read()
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Telegram отклонил сообщение: {exc.read().decode()}") from None
    print(f"Отправлено в Telegram: {len(messages)} сообщ., чатов: {len(chat_ids)}")


def notify(releases, errors, test=False):
    """Telegram, если задан токен бота; иначе почта."""
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        send_telegram(releases, errors, test)
    elif os.environ.get("SMTP_HOST"):
        send_email(build_email(releases, errors, test))
    else:
        sys.exit("Не заданы ни TELEGRAM_BOT_TOKEN, ни SMTP_HOST — некуда отправлять")


# ---------- основной сценарий ----------

def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="Трекер версий приложений в RuStore")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--test-email", action="store_true", help="отправить тестовое письмо с текущими версиями")
    parser.add_argument("--debug", action="store_true", help="печатать сырой ответ RuStore")
    args = parser.parse_args()
    DEBUG = args.debug

    apps = load_json(CONFIG_FILE, {}).get("apps", [])
    if not apps:
        sys.exit("В config.json нет списка apps")

    state = load_json(STATE_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    current, errors = [], []
    for app in apps:
        try:
            info = fetch_app(app["package"])
            current.append({**app, **info})
            print(f"{app['name']}: {info['version']} ({info['date']})")
        except Exception as exc:  # одно приложение не должно ронять остальные
            errors.append(f"{app['name']}: {exc}")
            print(f"ОШИБКА {app['name']}: {exc}")

    if not current:
        sys.exit("Не удалось получить данные ни по одному приложению")

    if args.test_email:
        notify(current, errors, test=True)
        return

    # Приложение, которого ещё нет в state, запоминаем без письма
    baseline = [r for r in current if r["package"] not in state]
    new = [r for r in current if r["package"] in state and state[r["package"]] != r["version"]]

    for r in baseline:
        print(f"Запомнил текущую версию: {r['name']} {r['version']}")

    if new:
        message = build_email(new, errors)
        if args.dry_run:
            print("\n--- ПИСЬМО (dry-run) ---")
            print("Тема:", message["Subject"])
            print(message.get_body(preferencelist=("plain",)).get_content())
        else:
            notify(new, errors)
    else:
        print("Новых версий нет.")

    if args.dry_run:
        return

    # Сюда доходим только если письмо отправлено без ошибки
    for r in baseline + new:
        state[r["package"]] = r["version"]
    save_state(state)
    append_csv(
        [[today, r["name"], r["package"], r["version"], r["date"], r["whats_new"]] for r in baseline + new]
    )


if __name__ == "__main__":
    main()
