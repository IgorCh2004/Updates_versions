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


def append_csv(rows):
    is_new = not CSV_FILE.exists()
    # utf-8-sig, чтобы Excel правильно показывал кириллицу
    with CSV_FILE.open("a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        if is_new:
            writer.writerow(["Дата проверки", "Приложение", "Пакет", "Версия", "Дата релиза", "Что нового"])
        writer.writerows(rows)


# ---------- страница истории ----------

HISTORY_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>История версий RuStore</title>
<style>
:root { color-scheme: light dark; --bg:#f5f6f8; --card:#fff; --fg:#1d1f23; --muted:#6b7280; --line:#e3e5e9; --accent:#2563eb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14161a; --card:#1e2127; --fg:#e8eaed; --muted:#9aa0a9; --line:#2d3139; --accent:#6ea0ff; }
}
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }
main { max-width:860px; margin:0 auto; }
h1 { font-size:22px; margin:8px 0 4px; }
.sub { color:var(--muted); margin:0 0 16px; font-size:13px; }
.filters { position:sticky; top:0; z-index:1; display:flex; flex-wrap:wrap; gap:8px;
  padding:10px 0; background:var(--bg); }
.filters select, .filters input { font:inherit; padding:8px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--fg); }
.filters input { flex:1 1 220px; min-width:0; }
.count { color:var(--muted); font-size:13px; margin:4px 0 12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin-bottom:12px; }
.head { display:flex; flex-wrap:wrap; gap:4px 12px; align-items:baseline; }
.app { font-weight:600; }
.ver { color:var(--accent); font-weight:600; }
.date { color:var(--muted); font-size:13px; margin-left:auto; }
.notes { margin:8px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
.none { color:var(--muted); font-style:italic; }
.link { display:inline-block; margin-top:8px; font-size:13px; color:var(--accent); }
mark { background:#ffe58a; color:#000; border-radius:2px; }
.empty { text-align:center; color:var(--muted); padding:32px 0; }
</style>
</head>
<body>
<main>
<h1>История версий RuStore</h1>
<p class="sub">Обновлено: __UPDATED__</p>
<div class="filters">
  <select id="app" aria-label="Приложение"><option value="">Все приложения</option></select>
  <input id="q" type="search" placeholder="Поиск по тексту «Что нового»" aria-label="Поиск">
</div>
<div class="count" id="count"></div>
<div id="list"></div>
</main>
<script>
const RELEASES = __DATA__;
const $ = id => document.getElementById(id);
const appSel = $("app"), q = $("q"), list = $("list"), count = $("count");

[...new Set(RELEASES.map(r => r.name))].sort((a, b) => a.localeCompare(b, "ru")).forEach(name => {
  const o = document.createElement("option");
  o.value = o.textContent = name;
  appSel.appendChild(o);
});

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function highlight(parent, text, needle) {
  if (!needle) { parent.textContent = text; return; }
  const low = text.toLowerCase();
  let pos = 0, i;
  while ((i = low.indexOf(needle, pos)) !== -1) {
    parent.append(text.slice(pos, i));
    parent.appendChild(el("mark", "", text.slice(i, i + needle.length)));
    pos = i + needle.length;
  }
  parent.append(text.slice(pos));
}

function render() {
  const needle = q.value.trim().toLowerCase();
  const app = appSel.value;
  const shown = RELEASES.filter(r =>
    (!app || r.name === app) &&
    (!needle || (r.whats_new + " " + r.version).toLowerCase().includes(needle)));
  list.replaceChildren();
  count.textContent = "Показано релизов: " + shown.length + " из " + RELEASES.length;
  if (!shown.length) { list.appendChild(el("div", "empty", "Ничего не найдено")); return; }
  for (const r of shown) {
    const card = el("div", "card");
    const head = el("div", "head");
    head.append(el("span", "app", r.name), el("span", "ver", r.version),
                el("span", "date", r.date || "дата не указана"));
    card.appendChild(head);
    const notes = el("p", r.whats_new ? "notes" : "notes none");
    if (r.whats_new) highlight(notes, r.whats_new, needle); else notes.textContent = "Описание не указано.";
    card.appendChild(notes);
    const a = el("a", "link", "История версий в RuStore");
    a.href = "https://www.rustore.ru/catalog/app/" + encodeURIComponent(r.package) + "/versions";
    a.target = "_blank"; a.rel = "noopener";
    card.appendChild(a);
    list.appendChild(card);
  }
}

appSel.addEventListener("change", render);
q.addEventListener("input", render);
render();
</script>
</body>
</html>
"""


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


def build_history():
    """Пересобирает history.html — один самодостаточный файл без внешних зависимостей."""
    data = json.dumps(read_releases(), ensure_ascii=False).replace("</", "<\\/")
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    HISTORY_FILE.write_text(
        HISTORY_TEMPLATE.replace("__UPDATED__", updated).replace("__DATA__", data),
        encoding="utf-8",
    )
    print(f"Страница истории обновлена: {HISTORY_FILE.name}")


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
    build_history()
    print(f"Добавлено строк в releases.csv: {len(rows)}")


# ---------- основной сценарий ----------

def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="Трекер версий приложений в RuStore")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять")
    parser.add_argument("--test-email", action="store_true", help="отправить тестовое письмо с текущими версиями")
    parser.add_argument("--debug", action="store_true", help="печатать сырой ответ RuStore")
    parser.add_argument("--backfill", action="store_true", help="один раз заполнить историю прошлыми версиями со страницы RuStore")
    args = parser.parse_args()
    DEBUG = args.debug

    apps = load_json(CONFIG_FILE, {}).get("apps", [])
    if not apps:
        sys.exit("В config.json нет списка apps")

    state = load_json(STATE_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
    build_history()


if __name__ == "__main__":
    main()
