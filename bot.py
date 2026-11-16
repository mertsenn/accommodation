"""Magis Real Estate -> Telegram bildirim botu.

magisrealestate.com/for-rent sayfasini ceker, CITY (varsayilan: Eindhoven)
icin musait ilan varsa Telegram'a mesaj atar. Ayni ilani tekrar tekrar
atmamak icin gorduklerini state.json'da tutar.

GitHub'da "Run workflow" ile ELLE calistirildiginda, ilan olsa da olmasa da
Telegram'a bir durum raporu atar (botun ve Telegram'in calistigini gormek icin).

Sadece standart kutuphane kullanir (pip install gerekmez).

Kullanim:
    TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python3 bot.py
    python3 bot.py --test      # Telegram baglantisini test eder
    python3 bot.py --dry-run   # mesaj atmadan bulduklarini yazdirir
    python3 bot.py --report    # durum raporunu da Telegram'a atar
"""

import html as htmllib
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

LISTING_PATH = re.compile(r"(?:^|/)for-rent/[^/]+/.+")
# Ham HTML icinde (href, JSON, attribute fark etmez) ilan adresi arar
RAW_LISTING = re.compile(
    r"(?:https?://(?:www\.)?magisrealestate\.com)?/for-rent/[^/\"'<>\s?#]+/[^\"'<>?#]+"
)
AVAILABLE = re.compile(r"(\d+)\s+(?:available|beschikbaar)", re.I)
PRICE = re.compile(r"€\s*[\d.,]+(?:\s*-\s*€\s*[\d.,]+)?")
SIZE = re.compile(r"[\d.,]+(?:\s*-\s*[\d.,]+)?\s*m\s?[2²]", re.I)
TAG = re.compile(r"<[^>]+>")


def norm_url(href):
    full = urllib.parse.urljoin(URL, href.strip())
    return full.replace(" ", "%20")


def strip_tags(fragment):
    text = TAG.sub(" ", fragment)
    return " ".join(htmllib.unescape(text).split())


class CardParser(HTMLParser):
    """/for-rent/<proje>/<ilan> linklerini ve icindeki metni toplar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = {}  # url -> metin
        self._href = None
        self._depth = 0
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        if self._href:  # kartin icinde ic ice link
            self._depth += 1
            return
        href = dict(attrs).get("href") or ""
        path = urllib.parse.unquote(urllib.parse.urlparse(norm_url(href)).path)
        if LISTING_PATH.search(path):
            self._href = norm_url(href)
            self._depth = 1
            self._buf = []

    def handle_data(self, data):
        if self._href:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._href:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = " ".join(" ".join(self._buf).split())
        # Ayni kart masaustu/mobil icin iki kez olabilir: uzun olani tut
        if len(text) > len(self.cards.get(self._href, "")):
            self.cards[self._href] = text
        self._href = None


def raw_cards(page):
    """Link yapisina bakmadan, ham HTML'de ilan adreslerini bulur ve her
    adresten bir sonraki ilana kadar olan metni dondurur: {url: metin}.
    Sitede "N available" etiketi linkin DISINDA (hemen arkasinda) durdugu
    icin musaitlik sayisi buradan okunur."""
    page = htmllib.unescape(page).replace("\\/", "/")
    matches = [(m.start(), m.end(), norm_url(m.group(0)))
               for m in RAW_LISTING.finditer(page)]
    cards = {}
    for i, (start, end, url) in enumerate(matches):
        stop = min(len(page), end + 15000)
        for nstart, _, nurl in matches[i + 1:]:
            if nurl != url:
                stop = min(stop, nstart)
                break
        text = strip_tags(page[end:stop])
        cards[url] = (cards.get(url, "") + " " + text).strip()
    return cards


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


def all_cards(page):
    """Sayfadaki butun ilan kartlari: {url: {"text": ..., "tail": ...}}
    text = kartin (linkin) kendi metni, tail = linkten sonraki ilana kadar her sey."""
    parser = CardParser()
    parser.feed(page)
    tails = raw_cards(page)
    cards = {}
    for url in list(parser.cards) + [u for u in tails if u not in parser.cards]:
        text = parser.cards.get(url, "")
        tail = tails.get(url, "")
        if len(text) < 20:
            # Link yontemi karti bulamadi: yedek olarak ham metnin basini kullan
            text = tail[:1500]
        cards[url] = {"text": text, "tail": tail}
    return cards


def find_listings(page, city=CITY):
    """Sehre ait, musait gorunen ilanlari dondurur: {url: {...}}"""
    found = {}
    for href, card in all_cards(page).items():
        text = card["text"]
        if city.lower() not in text.lower():
            continue
        m = AVAILABLE.search(text) or AVAILABLE.search(card["tail"])
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


def diagnostics(page, current):
    """Durum raporu: bot sayfada ne gordu?"""
    cards = all_cards(page)
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    lines = [
        f"{CITY} için müsait ilan: {len(current)}",
        f"Sayfadaki toplam ilan kartı: {len(cards)}",
    ]
    for url, card in list(cards.items())[:6]:
        lines.append(f"• {card['text'][:90]}")
    if not cards:
        plain = strip_tags(page)
        pos = plain.lower().find(CITY.lower())
        lines += [
            f"Sayfa başlığı: {strip_tags(title.group(1)) if title else '?'}",
            f"HTML uzunluğu: {len(page)}",
            f"Ham HTML'de 'for-rent/' sayısı: {page.count('for-rent/')}",
            f"Sayfada '{CITY}' geçiyor mu: {'evet' if pos >= 0 else 'hayır'}",
        ]
        m = re.search(r"\d+\s+available residences?", plain, re.I)
        if m:
            lines.append(f"Sayaç: {m.group(0)}")
            around = plain[max(0, m.start() - 100): m.end() + 400]
            lines.append(f"Sayaç çevresi: …{around}…")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"].strip()
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if not json.load(r).get("ok"):
                raise RuntimeError("Telegram mesaji gonderilemedi")
    except urllib.error.HTTPError as e:
        # Telegram'in hata aciklamasini log'a yaz (token burada gorunmez)
        raise RuntimeError(
            f"Telegram hatasi {e.code}: {e.read().decode(errors='replace')}"
        ) from None


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
    # GitHub'da elle (Run workflow) calistirildiysa rapor da gonder
    report = "--report" in argv or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    page = fetch(URL)
    current = find_listings(page)
    previous = load_state()
    diag = diagnostics(page, current)
    print(diag + "\n")

    if report and not dry:
        send_telegram("✅ Magis bot çalıştı (elle test)\n" + diag)

    if "for-rent" not in page:
        # Sayfa yapisi degismis ya da engellenmis olabilir -> workflow fail olsun
        raise RuntimeError("Sayfa beklenen icerigi dondurmedi (engel / yapi degisikligi?)")

    for url, item in current.items():
        old = previous.get(url)
        # Yeni ilan, ya da musait birim sayisi artmis -> haber ver
        is_new = old is None or (
            item["available"] is not None
            and (old.get("available") or 0) < item["available"]
        )
        if not is_new:
            print(f"Daha once bildirildi: {url}")
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
