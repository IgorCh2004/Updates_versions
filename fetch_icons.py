"""Скачивает иконки приложений из config.json в папку icons/ (запускать вручную при добавлении приложения).

    python fetch_icons.py           скачать недостающие
    python fetch_icons.py --force   перекачать все
"""

import re
import sys
import time
import urllib.request

import tracker

ICONS = tracker.ROOT / "icons"


def icon_url(package):
    """Адрес иконки именно этого приложения (на странице есть иконки и других приложений)."""
    raw = tracker.http_get(f"https://www.rustore.ru/catalog/app/{package}")
    stream = tracker._page_stream(raw)
    # блоки приложений начинаются с "appId":N,"appName"; нужен тот, внутри которого наш packageName
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'"appId":(\d+),"appName"', stream)]
    for i, (start, _) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(stream)
        block = stream[start:end]
        if f'"packageName":"{package}"' not in block:
            continue
        # собственная иконка идёт первой среди iconUrl с пресетом web_app_icon (дальше — рекомендованные приложения)
        m = re.search(r'"iconUrl":"(https://static\.rustore\.ru/imgproxy/[^"]*preset:web_app_icon[^"]*)"', block)
        if m:
            break
    else:
        raise ValueError("не нашёл iconUrl")
    return m.group(1)


def main():
    force = "--force" in sys.argv
    ICONS.mkdir(exist_ok=True)
    for i, app in enumerate(tracker.load_json(tracker.CONFIG_FILE, {}).get("apps", [])):
        target = ICONS / f"{app['package']}.webp"
        if target.exists() and not force:
            continue
        if i:
            time.sleep(tracker.PAUSE_BETWEEN_APPS)
        try:
            url = icon_url(app["package"])
            request = urllib.request.Request(url, headers=tracker.HEADERS)
            target.write_bytes(urllib.request.urlopen(request, timeout=30).read())
            print(f"{app['name']}: {target.name} ({target.stat().st_size} байт)")
        except Exception as exc:
            print(f"ОШИБКА {app['name']}: {exc}")


if __name__ == "__main__":
    main()
