"""Трекер новых версий приложений в RuStore.

Раз в день проверяет приложения из config.json. Если вышла новая версия,
отправляет одно письмо-сводку и дописывает строку в releases.csv.

Страница показывает новые функции, выделенные из текста релизов. Функции новой версии сразу появляются
с плашкой «Не проверено»; кнопки «Проверить» / «Отклонить» ставят общую отметку (хранится в Cloudflare Worker).

Запуск:
    python tracker.py               обычная проверка
    python tracker.py --dry-run     показать письмо в консоли, ничего не слать и не сохранять
    python tracker.py --test-email  отправить тестовое сообщение с текущими версиями
    python tracker.py --features    показать релизы, для которых ещё не выделены новые функции (features.json)
    python tracker.py --debug       напечатать сырой ответ RuStore (для отладки)

Куда отправлять — задаётся переменными окружения:
    Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (несколько чатов через запятую)
    или почта: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_TO
Новые функции из текста релиза в features.json записывает Claude Code по просьбе («разбери новые релизы») — без API-ключей.
"""

import argparse
import base64
import csv
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"
CSV_FILE = ROOT / "releases.csv"
HISTORY_FILE = ROOT / "history.html"
ICONS_DIR = ROOT / "icons"
PENDING_FILE = ROOT / "pending.json"        # новые версии: на странице у них кнопка «Проверить»
FEATURES_FILE = ROOT / "features.json"      # «пакет|версия» → новые функции, выделенные из текста релиза ([] — функций нет)
SITE_DIR = ROOT / "docs"  # папка, которую публикует GitHub Pages: только страница истории

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
PAUSE_BETWEEN_APPS = 3  # секунд, чтобы не упереться в лимит запросов RuStore


# ---------- получение данных из RuStore ----------

def http_get(url, retries=3):
    """GET с повтором: при перегрузке RuStore/VK отвечает 429 или страницей «Ошибка 429»."""
    request = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
            if "Ошибка 429" not in body[:3000]:
                return body
            error = "RuStore ответил «Ошибка 429» (слишком много запросов)"
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            error = "HTTP 429 (слишком много запросов)"
        if attempt == retries:
            raise ValueError(error)
        wait = 10 * 3 ** attempt  # 10, 30, 90 секунд
        print(f"{error}; повтор через {wait} с")
        time.sleep(wait)


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


def _page_stream(raw):
    """Страница на Next.js отдаёт данные кусками self.__next_f.push([1,"..."]); склеиваем их."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', raw)
    return "".join(json.loads(f'"{c}"') for c in chunks)


def _text_chunk(data, chunk_id):
    """Длинные тексты вынесены в блоки «<id>:T<длина в байтах, hex>,<текст>»; whatsNew ссылается на них как "$<id>"."""
    m = re.search(rb"(?<![0-9a-zA-Z])" + chunk_id.encode() + rb":T([0-9a-f]+),", data)
    if not m:
        return ""
    return data[m.end():m.end() + int(m.group(1), 16)].decode("utf-8", errors="replace")


def page_versions(package):
    """Все версии со страницы /versions, от новых к старым, без повторных загрузок одной версии."""
    raw = http_get(PAGE_URL.format(package=package))
    if DEBUG:
        print(f"--- PAGE {package} ---\n{raw[:2000]}\n")
    stream = _page_stream(raw)
    data = stream.encode("utf-8")
    pattern = (
        r'\{"versionName":"([^"]*)","versionCode":\d+,"whatsNew":"((?:[^"\\]|\\.)*)",'
        r'"appVerUpdatedAt":"([^"]*)","status":"\w+"\}'
    )
    found = {}
    for version, notes, updated in re.findall(pattern, stream):
        notes = json.loads(f'"{notes}"')
        if re.fullmatch(r"\$[0-9a-f]+", notes):
            notes = _text_chunk(data, notes[1:])
        entry = {"version": version, "date": updated[:10], "whats_new": notes.strip()}
        old = found.get(version)
        if old is None:
            found[version] = entry  # записи идут от новых к старым — первая и есть актуальный текст
        else:
            old["date"] = min(old["date"], entry["date"])  # датой релиза считаем первую загрузку
    if not found:
        raise ValueError("на странице не нашлось данных о версиях")
    return list(found.values())


def fetch_from_page(package):
    """Запасной источник: самая свежая версия со страницы истории версий."""
    return page_versions(package)[0]


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


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_csv(rows):
    is_new = not CSV_FILE.exists()
    # utf-8-sig, чтобы Excel правильно показывал кириллицу
    with CSV_FILE.open("a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        if is_new:
            writer.writerow(["Дата проверки", "Приложение", "Пакет", "Версия", "Дата релиза", "Что нового"])
        writer.writerows(rows)


# ---------- новые функции из текста релиза ----------

def missing_features():
    """Релизы из конфига, которых ещё нет в features.json: функции из них выделяет Claude Code по просьбе, без API."""
    features = load_json(FEATURES_FILE, {})
    packages = {a["package"] for a in load_json(CONFIG_FILE, {}).get("apps", [])}
    todo = [r for r in reversed(read_releases())
            if r["package"] in packages and f"{r['package']}|{r['version']}" not in features]
    if todo:
        print(f"Нужно выделить функции для релизов: {len(todo)}")
        for r in todo:
            print(f"  {r['name']} {r['version']} ({r['date']})")
    return todo


def feature_rows(releases):
    """Плоский список функций для страницы; порядок — как у релизов (от новых к старым).
    key («пакет|версия#номер») — ключ общей отметки «проверено/отклонено»; список функций релиза после записи не меняется."""
    features = load_json(FEATURES_FILE, {})
    return [
        {"key": f"{r['package']}|{r['version']}#{i}", "name": r["name"], "package": r["package"],
         "version": r["version"], "date": r["date"], "checked": r["checked"], "text": text}
        for r in releases for i, text in enumerate(features.get(f"{r['package']}|{r['version']}", []))
    ]


# ---------- страница истории ----------

HISTORY_TEMPLATE_FILE = ROOT / "history_template.html"


def read_releases():
    """Читает releases.csv; без дублей (пакет + версия), от новых к старым."""
    if not CSV_FILE.exists():
        return []
    with CSV_FILE.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader, None)  # заголовок
        rows = [r for r in reader if len(r) >= 6]
    seen, releases = set(), []
    for checked, name, package, version, date, whats_new in rows:
        if (package, version) in seen:
            continue
        seen.add((package, version))
        releases.append({
            "checked": checked, "name": name, "package": package,
            "version": version, "date": date, "whats_new": whats_new.strip(),
        })
    # дата релиза; если её нет — дата проверки. Для равных дат порядок записи в CSV сохраняется.
    releases.reverse()
    releases.sort(key=lambda r: r["date"] or r["checked"], reverse=True)
    return releases


def apps_with_icons():
    """Приложения из config.json; иконки из icons/ вшиваются в страницу, чтобы она оставалась одним файлом."""
    apps = load_json(CONFIG_FILE, {}).get("apps", [])
    for app in apps:
        icon = ICONS_DIR / f"{app['package']}.webp"
        if icon.exists():
            app["icon"] = "data:image/webp;base64," + base64.b64encode(icon.read_bytes()).decode()
    return apps


def unchecked_keys():
    """«пакет|версия» новых версий: на странице у них плашка и кнопка «Проверить». У старых версий их нет."""
    return [f"{r['package']}|{r['version']}" for r in load_json(PENDING_FILE, [])]


def add_pending(releases, today):
    pending = load_json(PENDING_FILE, [])
    seen = {(r["package"], r["version"]) for r in pending}
    for r in releases:
        if (r["package"], r["version"]) not in seen:
            pending.append({
                "checked": today, "name": r["name"], "package": r["package"],
                "version": r["version"], "date": r["date"], "whats_new": r["whats_new"],
            })
    save_json(PENDING_FILE, pending)


def build_history():
    """Пересобирает history.html — один самодостаточный файл без внешних зависимостей."""
    apps = apps_with_icons()
    packages = {a["package"] for a in apps}
    # на странице только приложения из config.json (бывшие, например ВТБ, остаются в CSV, но скрыты)
    releases = [r for r in read_releases() if r["package"] in packages]
    apps_json = json.dumps(apps, ensure_ascii=False).replace("</", "<\\/")
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    template = HISTORY_TEMPLATE_FILE.read_text(encoding="utf-8")
    page = (
        template.replace("__UPDATED__", updated).replace("__APPS__", apps_json)
        .replace("__UNCHECKED__", json.dumps(unchecked_keys(), ensure_ascii=False).replace("</", "<\\/"))
        .replace("__FEATURES__", json.dumps(feature_rows(releases), ensure_ascii=False).replace("</", "<\\/"))
    )
    HISTORY_FILE.write_text(page, encoding="utf-8")
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    # те же данные в JSON — их читает Telegram-бот (worker/worker.js); иконки ему не нужны
    bot_apps = [{"package": a["package"], "name": a["name"]} for a in apps]
    (SITE_DIR / "data.json").write_text(
        json.dumps({"apps": bot_apps, "releases": releases, "unchecked": unchecked_keys()}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Страница обновлена: {HISTORY_FILE.name} (релизов: {len(releases)}, функций: {len(feature_rows(releases))})")


# ---------- письмо ----------

def history_url():
    """Ссылка на опубликованную страницу истории; задаётся переменной HISTORY_URL."""
    return os.environ.get("HISTORY_URL", "").strip()


def build_email(releases, errors, test=False, unchanged=()):
    short = ", ".join(f"{r['name']} {r['version']}" for r in releases)
    subject = ("[ТЕСТ] " if test else "") + (
        f"RuStore: новые версии — {short}" if releases else "RuStore: обновлений нет"
    )

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
    if unchanged:
        text_parts.append("\n".join(f"{name} — обновлений нет" for name in unchanged) + "\n")
        html_parts.append(
            "<p style='margin:24px 0 4px;color:#666'>"
            + "<br>".join(f"{html.escape(name)} — обновлений нет" for name in unchanged)
            + "</p>"
        )
    if history_url():
        text_parts.append(f"История обновлений: {history_url()}")
        html_parts.append(
            f"<p style='margin:24px 0 0'><a href='{html.escape(history_url())}'>История обновлений</a></p>"
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


def send_telegram(releases, errors, test=False, unchanged=()):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_ids = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    # в каждом сообщении — ссылка на страницу истории
    footer = f"\n\nИстория обновлений: {history_url()}" if history_url() else ""
    messages = []
    for r in releases:
        text = (
            ("[ТЕСТ] " if test else "")
            + f"{r['name']} — версия {r['version']} ({r['date'] or 'дата не указана'})\n\n"
            + (r["whats_new"] or "Описание не указано.")
        )
        link = "\n\n" + PAGE_URL.format(package=r["package"]) + footer
        messages.append(text[: 4000 - len(link)] + link)  # лимит Telegram — 4096 символов
    if unchanged:
        text = ("[ТЕСТ] " if test else "") + "\n".join(f"{name} — обновлений нет" for name in unchanged)
        messages.append(text[: 4000 - len(footer)] + footer)
    if errors:
        text = "Не удалось проверить:\n" + "\n".join(f"- {e}" for e in errors)
        messages.append(text[: 4000 - len(footer)] + footer)

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


def has_channel():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("SMTP_HOST"))


def notify(releases, errors, test=False, unchanged=()):
    """Telegram, если задан токен бота; иначе почта."""
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        send_telegram(releases, errors, test, unchanged)
    elif os.environ.get("SMTP_HOST"):
        send_email(build_email(releases, errors, test, unchanged))
    else:
        sys.exit("Не заданы ни TELEGRAM_BOT_TOKEN, ни SMTP_HOST — некуда отправлять")


# ---------- заполнение истории ----------

def backfill(apps, state, today, dry_run=False):
    """Один раз дописывает в releases.csv прошлые версии со страницы /versions."""
    known = {(r["package"], r["version"]) for r in read_releases()}
    rows = []
    for i, app in enumerate(apps):
        if i:
            time.sleep(PAUSE_BETWEEN_APPS)
        try:
            versions = page_versions(app["package"])
        except Exception as exc:  # одно приложение не должно ронять остальные
            print(f"ОШИБКА {app['name']}: {exc}")
            continue
        missing = [v for v in versions if (app["package"], v["version"]) not in known]
        print(f"{app['name']}: на странице {len(versions)} версий, новых для истории {len(missing)}")
        # приложение, которого нет в state, запоминаем по самой свежей версии — без лишнего письма
        if app["package"] not in state and not dry_run:
            state[app["package"]] = versions[0]["version"]
        for v in reversed(missing):  # в CSV — от старых к новым
            rows.append([today, app["name"], app["package"], v["version"], v["date"], v["whats_new"]])
    if dry_run:
        print(f"Было бы добавлено строк: {len(rows)} (dry-run, ничего не сохранено)")
        return
    if rows:
        append_csv(rows)
        save_state(state)
    missing_features()
    build_history()
    print(f"Добавлено строк в releases.csv: {len(rows)}")


# ---------- основной сценарий ----------

def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="Трекер версий приложений в RuStore")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--test-email", action="store_true", help="отправить тестовое письмо с текущими версиями")
    parser.add_argument("--debug", action="store_true", help="печатать сырой ответ RuStore")
    parser.add_argument("--features", action="store_true", help="показать релизы без выделенных функций и пересобрать страницу")
    parser.add_argument("--backfill", action="store_true", help="один раз заполнить историю прошлыми версиями со страницы RuStore")
    args = parser.parse_args()
    DEBUG = args.debug

    apps = load_json(CONFIG_FILE, {}).get("apps", [])
    if not apps:
        sys.exit("В config.json нет списка apps")

    state = load_json(STATE_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.features:
        missing_features()
        build_history()
        return

    if args.backfill:
        backfill(apps, state, today, dry_run=args.dry_run)
        return

    current, errors = [], []
    for i, app in enumerate(apps):
        if i:
            time.sleep(PAUSE_BETWEEN_APPS)
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

    new_packages = {r["package"] for r in new}
    unchanged = [r["name"] for r in current if r["package"] not in new_packages]

    if not new:
        print("Новых версий нет.")
    if args.dry_run:
        message = build_email(new, errors, unchanged=unchanged)
        print("\n--- ПИСЬМО (dry-run) ---")
        print("Тема:", message["Subject"])
        print(message.get_body(preferencelist=("plain",)).get_content())
    elif new or has_channel():
        # каждый день — сводка: новые версии (на проверку) и «обновлений нет» по остальным
        notify(new, errors, unchanged=unchanged)

    if args.dry_run:
        return

    # Сюда доходим только если письмо отправлено без ошибки
    for r in baseline + new:
        state[r["package"]] = r["version"]
    save_state(state)
    append_csv([[today, r["name"], r["package"], r["version"], r["date"], r["whats_new"]] for r in baseline + new])
    add_pending(new, today)  # на странице новая версия помечена «не проверено», пока кто-то не нажмёт «Проверить»
    missing_features()
    build_history()


if __name__ == "__main__":
    main()
