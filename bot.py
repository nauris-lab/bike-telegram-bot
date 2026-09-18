#!/usr/bin/env python3
"""
Wallapop -> Telegram watcher (детский велосипед, район Дении).

Читает поисковые запросы из searches.txt, обходит Wallapop Search API с
сортировкой по близости, отбрасывает всё дальше RADIUS_KM, аксессуары и
объявления вне ценового коридора, и присылает новое в Telegram.
Уже отправленные id хранятся в state.json, поэтому повторов не будет.

Переменные окружения (GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN  - токен от @BotFather
    TELEGRAM_CHAT_ID    - chat id получателя (можно несколько через запятую)
"""

import os
import re
import sys
import json
import time
import html
import math
from pathlib import Path

import requests

# --- Что ищем ---------------------------------------------------------------

# Дения, Аликанте. Поменять координаты можно тут (найти свои: Google Maps ->
# правый клик по точке -> первая строка это "широта, долгота").
HOME_LAT = 38.8408
HOME_LON = 0.1057
RADIUS_KM = 60           # искать в этом радиусе от точки выше

MIN_PRICE_EUR = 50       # дешевле этого — обычно хлам или запчасти
MAX_PRICE_EUR = 200      # дороже этого не присылать

# Размер колеса в дюймах. Если размер удалось определить из заголовка и он вне
# этого диапазона — объявление пропускается. Если не определился — присылаем
# (лучше лишнее сообщение, чем упущенный велосипед).
# 26″ — подростковый/взрослый размер. Потолок 29 пропускает 26 / 27,5 / 28 / 29.
# Нужен строго 26 — поставь MAX_WHEEL_IN = 26.
MIN_WHEEL_IN = 26
MAX_WHEEL_IN = 29

# --- Технические настройки --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
SEARCHES_FILE = BASE_DIR / "searches.txt"
STATE_FILE = BASE_DIR / "state.json"

API_URL = "https://api.wallapop.com/api/v3/search"
ITEM_URL = "https://es.wallapop.com/item/{slug}"

MAX_PAGES = 8            # предохранитель: максимум страниц на один запрос
MAX_SEEN = 6000          # сколько id помнить (ограничивает размер state.json)
SEND_DELAY = 0.6         # пауза между сообщениями в Telegram, сек
FETCH_DELAY = 0.4        # пауза между запросами к Wallapop, сек
REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9",
    "X-DeviceOS": "0",
}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [c for c in re.split(r"[\s,]+", os.environ.get("TELEGRAM_CHAT_ID", "")) if c]

# Слова, с которых начинается заголовок аксессуара, а не велосипеда.
# Проверяются только в первых двух словах: испанские объявления почти всегда
# начинаются с названия предмета ("Sillín de bicicleta ..." -> это седло).
ACCESSORY_HEAD = {
    "sillin", "sillín", "silla", "casco", "luz", "luces", "candado", "bomba",
    "portabicicletas", "portabicis", "remolque", "rueda", "ruedas", "cubierta",
    "cubiertas", "camara", "cámara", "pedal", "pedales", "manillar", "cesta",
    "timbre", "guardabarros", "funda", "maillot", "culotte", "zapatillas",
    "rodillo", "soporte", "cadena", "freno", "frenos", "horquilla", "potencia",
    "tija", "bidon", "bidón", "alforja", "alforjas", "guantes", "gafas",
    "cuadro", "llanta", "llantas", "pinon", "piñon", "plato", "desviador",
}

# Слова, при которых объявление отбрасывается, где бы они ни стояли.
HARD_BLOCK = {
    "patinete", "estatica", "estática", "spinning", "eliptica", "elíptica",
    "despiece", "repuesto", "repuestos", "averiada", "averiado", "restaurar",
    "piezas", "chatarra",
}

# Заголовок должен содержать хотя бы одно из этих слов.
BIKE_WORDS = ("bici", "bicicleta", "bicis", "bmx", "mtb", "btwin", "bicicletas")

# "26 pulgadas", '27,5"', "28 inch" — с явной единицей измерения.
WHEEL_RE = re.compile(
    r'(?<!\d)(1[0-9]|2[0-9])(?:[.,](\d))?\s*(?:"|”|\'\'|pulg|pulgadas|polgadas|inch|in\b)',
    re.IGNORECASE,
)
# Типовой размер без единицы: "Bicicleta montaña 26 roja", "Rockrider 27,5".
STANDALONE_WHEEL_RE = re.compile(
    r'(?<!\d)(12|14|16|18|20|24|26|27|28|29)(?:[.,](\d))?(?!\d)'
)


# --- Загрузка конфигурации и состояния --------------------------------------

def load_searches():
    """Прочитать searches.txt -> список поисковых фраз."""
    out = []
    for line in SEARCHES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_state():
    """Вернуть (список_виденных_id, это_первый_запуск)."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return list(data.get("seen", [])), False
        except Exception as e:
            print(f"  ! не смог прочитать state.json ({e}); начинаю с нуля",
                  file=sys.stderr)
    return [], True


def save_state(seen):
    seen = seen[-MAX_SEEN:]
    STATE_FILE.write_text(
        json.dumps({"seen": seen}, ensure_ascii=False),
        encoding="utf-8",
    )


# --- Wallapop ---------------------------------------------------------------

def distance_km(lat, lon):
    """Расстояние от дома до точки, км (формула гаверсинуса)."""
    r_lat, r_lon = math.radians(lat), math.radians(lon)
    h_lat, h_lon = math.radians(HOME_LAT), math.radians(HOME_LON)
    dlat, dlon = r_lat - h_lat, r_lon - h_lon
    a = math.sin(dlat / 2) ** 2 + math.cos(h_lat) * math.cos(r_lat) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def search(session, keywords):
    """Все объявления по одной фразе в пределах RADIUS_KM.

    Wallapop сортирует по близости, поэтому как только страница целиком ушла
    за радиус — дальше смотреть незачем.
    """
    params = {
        "keywords": keywords,
        "latitude": HOME_LAT,
        "longitude": HOME_LON,
        "source": "search_box",
        "order_by": "closest",   # единственный режим, который реально учитывает координаты
    }
    found, token = [], None

    for _ in range(MAX_PAGES):
        q = dict(params)
        if token:
            q["next_page"] = token
        try:
            r = session.get(API_URL, params=q, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ! запрос «{keywords}» не удался: {e}", file=sys.stderr)
            break

        try:
            items = data["data"]["section"]["payload"]["items"]
        except (KeyError, TypeError):
            break
        if not items:
            break

        in_radius = 0
        for it in items:
            loc = it.get("location") or {}
            lat, lon = loc.get("latitude"), loc.get("longitude")
            if lat is None or lon is None:
                continue
            d = distance_km(lat, lon)
            if d <= RADIUS_KM:
                it["_distance_km"] = d
                found.append(it)
                in_radius += 1

        if in_radius == 0:          # вся страница уже за радиусом
            break
        token = (data.get("meta") or {}).get("next_page")
        if not token:
            break
        time.sleep(FETCH_DELAY)

    return found


# --- Фильтры ----------------------------------------------------------------

def get_price(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def is_reserved(item):
    for key in ("reserved", "sold"):
        v = item.get(key)
        if isinstance(v, dict):
            v = v.get("flag")
        if v is True:
            return True
    return False


def detect_wheel_inches(title):
    """Размер колеса из заголовка в дюймах (float), или None если не нашёлся.

    Понимает дробные размеры: "27,5 pulgadas" и '27.5"' дают 27.5.
    """
    for rx in (WHEEL_RE, STANDALONE_WHEEL_RE):
        m = rx.search(title or "")
        if m:
            whole = int(m.group(1))
            frac = m.group(2)
            return whole + int(frac) / 10 if frac else float(whole)
    return None


def looks_like_accessory(title):
    words = re.findall(r"[a-záéíóúñü]+", (title or "").lower())
    if any(w in HARD_BLOCK for w in words):
        return True
    return any(w in ACCESSORY_HEAD for w in words[:2])


def keep(item, stats):
    """Решить, показывать ли объявление. Считает причины отсева в stats."""
    title = item.get("title") or ""
    low = title.lower()

    if is_reserved(item):
        stats["reserved"] += 1
        return False
    if not any(w in low for w in BIKE_WORDS):
        stats["not_a_bike"] += 1
        return False
    if looks_like_accessory(title):
        stats["accessory"] += 1
        return False

    price = get_price(item)
    if price is not None and (price < MIN_PRICE_EUR or price > MAX_PRICE_EUR):
        stats["price"] += 1
        return False

    wheel = detect_wheel_inches(title)
    if wheel is not None and not (MIN_WHEEL_IN <= wheel <= MAX_WHEEL_IN):
        stats["wheel"] += 1
        return False

    return True


# --- Сообщение --------------------------------------------------------------

def pick_image(item):
    imgs = item.get("images") or []
    if not imgs:
        return None
    urls = imgs[0].get("urls") or {}
    for size in ("big", "large", "medium", "original", "small"):
        if urls.get(size):
            return urls[size]
    return next(iter(urls.values()), None)


def build_message(item, query):
    title = html.escape((item.get("title") or "").strip())
    price = get_price(item)
    loc = item.get("location") or {}
    city = html.escape(loc.get("city") or "")
    dist = item.get("_distance_km")
    wheel = detect_wheel_inches(item.get("title") or "")
    slug = item.get("web_slug") or ""
    link = ITEM_URL.format(slug=slug) if slug else ""

    head = f"🚲 <b>{title}</b>"
    bits = []
    if price is not None:
        bits.append(f"<b>{price:.0f} €</b>")
    if wheel:
        bits.append(f"{wheel:g}″")
    if city:
        bits.append(city)
    if dist is not None:
        bits.append(f"{dist:.0f} км")

    desc = (item.get("description") or "").strip().replace("\n", " ")
    desc = re.sub(r"\s+", " ", desc)
    if len(desc) > 200:
        desc = desc[:197] + "…"

    parts = [head, " · ".join(bits)]
    if desc:
        parts.append(html.escape(desc))
    if link:
        parts.append(f'👉 <a href="{html.escape(link)}">Открыть на Wallapop</a>')
    parts.append(f"<i>по запросу: {html.escape(query)}</i>")
    return "\n\n".join(p for p in parts if p), pick_image(item)


# --- Telegram ---------------------------------------------------------------

def _send_one(chat_id, text_html, image_url):
    if image_url:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            data={
                "chat_id": chat_id,
                "photo": image_url,
                "caption": text_html[:1024],
                "parse_mode": "HTML",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            return True
        print(f"  ! sendPhoto {r.status_code} -> {chat_id}: {r.text[:180]}",
              file=sys.stderr)

    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        print(f"  ! sendMessage {r.status_code} -> {chat_id}: {r.text[:180]}",
              file=sys.stderr)
    return r.status_code == 200


def send(text_html, image_url):
    ok = False
    for chat_id in CHAT_IDS:
        if _send_one(chat_id, text_html, image_url):
            ok = True
        time.sleep(0.1)
    return ok


# --- Main -------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv

    if not dry_run and (not TOKEN or not CHAT_IDS):
        print("ОШИБКА: задай TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    searches = load_searches()
    seen, bootstrap = load_state()
    seen_set = set(seen)
    if dry_run:
        bootstrap = False   # в тестовом прогоне показываем всё, что нашли

    if bootstrap:
        print("Первый запуск: запоминаю текущие объявления БЕЗ отправки.")

    stats = {"reserved": 0, "not_a_bike": 0, "accessory": 0, "price": 0, "wheel": 0}
    fresh = []

    session = requests.Session()
    for query in searches:
        items = search(session, query)
        new_count = 0
        for it in items:
            item_id = it.get("id")
            if not item_id or item_id in seen_set:
                continue
            seen_set.add(item_id)
            seen.append(item_id)
            new_count += 1
            if bootstrap:
                continue
            if keep(it, stats):
                fresh.append(build_message(it, query))
        print(f"  «{query}» -> {len(items)} в радиусе, {new_count} новых")
        time.sleep(FETCH_DELAY)

    if dry_run:
        print(f"\n[dry-run] прошло фильтры: {len(fresh)}")
        for text, img in fresh[:10]:
            plain = re.sub(r"<[^>]+>", "", text).replace("\n\n", " | ")
            print("   •", plain[:150])
        print(f"\nотсеяно: {stats}")
        return

    sent = 0
    for text_html, image_url in fresh:
        if send(text_html, image_url):
            sent += 1
        time.sleep(SEND_DELAY)

    save_state(seen)
    print(f"Готово. Отправлено {sent}, отсеяно {stats}, "
          f"в памяти {len(seen[-MAX_SEEN:])} id.")


if __name__ == "__main__":
    main()
