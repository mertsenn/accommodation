"""Magis Real Estate -> Telegram bildirim botu.

magisrealestate.com/for-rent sayfasini ceker, CITY (varsayilan: Eindhoven)
icin musait ilan varsa Telegram'a mesaj atar. Ayni ilani tekrar tekrar
atmamak icin gorduklerini state.json'da tutar.

Sadece standart kutuphane kullanir (pip install gerekmez).

Kullanim:
    TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python3 bot.py
    python3 bot.py --test      # Telegram baglantisini test eder
    python3 bot.py --dry-run   # mesaj atmadan bulduklarini yazdirir
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

URL = "https://magisrealestate.com/for-rent"
CITY = os.environ.get("CITY", "Eindhoven")
STATE_FILE = Path(__file__).with_name("state.json")
LISTING_PATH = re.compile(r"^/for-rent/[^/]+/.+")
AVAILABLE = re.compile(r"(\d+)\s+(?:available|beschikbaar)", re.I)
PRICE = re.compile(r"€\s*[\d.,]+(?:\s*-\s*€\s*[\d.,]+)?")
SIZE = re.compile(r"[\d.,]+(?:\s*-\s*[\d.,]+)?\s*m\s?[2²]", re.I)


class CardParser(HTMLParser):
    """/for-rent/<proje>/<ilan> linklerini ve icindeki metni toplar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = {}  # href -> metin
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        full = urllib.parse.urljoin(URL, href)
        parsed = urllib.parse.urlparse(full)
        if parsed.netloc.endswith("magisrealestate.com") and LISTING_PATH.match(
            urllib.parse.unquote(parsed.path)
        ):
            self._href = full.replace(" ", "%20")
            self._buf = []

    def handle_data(self, data):
        if self._href:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            text = " ".join(" ".join(self._buf).split())
            # Sayfada ayni kart masaustu/mobil icin iki kez olabilir: uzun olani tut
            if len(text) > len(self.cards.get(self._href, "")):
                self.cards[self._href] = text
            self._href = None


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Accept-Language": "en",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def find_listings(html, city=CITY):
    """Sehre ait, musait gorunen ilanlari dondurur: {url: {...}}"""
    parser = CardParser()
    parser.feed(html)
    found = {}
    for href, text in parser.cards.items():
        if city.lower() not in text.lower():
            continue
        m = AVAILABLE.search(text)
        count = int(m.group(1)) if m else None
        if count == 0:
            continue  # kart var ama musait birim yok
        price = PRICE.search(text)
        size = SIZE.search(text)
        found[href] = {
            "text": text,
            "available": count,
            "price": price.group(0) if price else None,
            "size": size.group(0) if size else None,
        }
    return found


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError("Telegram mesaji gonderilemedi")


def format_message(url, item):
    lines = [f"🏠 Magis'te {CITY} ilanı!"]
    if item["available"] is not None:
        lines.append(f"Müsait: {item['available']}")
    if item["price"]:
        lines.append(f"Kira: {item['price']} /ay (excl.)")
    if item["size"]:
        lines.append(f"Boyut: {item['size']}")
    lines.append(item["text"][:300])
    lines.append(url)
    return "\n".join(lines)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main(argv):
    if "--test" in argv:
        send_telegram("✅ Magis bot çalışıyor.")
        print("Test mesaji gonderildi.")
        return 0

    dry = "--dry-run" in argv
    html = fetch(URL)
    if "for-rent" not in html:
        # Sayfa yapisi degismis ya da engellenmis olabilir -> workflow fail olsun
        raise RuntimeError("Sayfa beklenen icerigi dondurmedi (engel / yapi degisikligi?)")

    current = find_listings(html)
    previous = load_state()
    print(f"{CITY} icin musait ilan: {len(current)}")

    for url, item in current.items():
        old = previous.get(url)
        # Yeni ilan, ya da musait birim sayisi artmis -> haber ver
        is_new = old is None or (
            item["available"] is not None
            and (old.get("available") or 0) < item["available"]
        )
        if not is_new:
            continue
        msg = format_message(url, item)
        print("BILDIRIM:\n" + msg + "\n")
        if not dry:
            send_telegram(msg)

    # Kaybolan ilanlar state'ten duser; tekrar cikarsa yeniden bildirilir
    new_state = {u: {"available": i["available"]} for u, i in current.items()}
    if not dry and new_state != previous:
        STATE_FILE.write_text(json.dumps(new_state, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
