"""
Scraper multi‑fuente.
Lee `sources.yml`, procesa varios medios financieros marroquíes,
filtra las noticias del día y las publica en Telegram evitando duplicados.
"""

import os
import re
import time
import urllib.parse
from datetime import date
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# HTTP con User‑Agent propio (corrige el 403 de Medias24)                      #
# --------------------------------------------------------------------------- #

def _get_session(
    total_retries: int = 5,
    backoff_factor: float = 1.0,
    status_forcelist: tuple = (429, 500, 502, 503, 504),
) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; OussamaSamni/1.0; "
            "+https://github.com/OussamaSamni/moroccan-finance-scraper)"
        ),
        "Accept-Language": "fr,en;q=0.8",
    })
    retry_strategy = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_url(url: str, timeout: float = 10.0) -> str:
    """Descarga la URL con reintentos y cabeceras UA/Idioma adecuadas."""
    session = _get_session()
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text

# --------------------------------------------------------------------------- #
# Helpers originales                                                          #
# --------------------------------------------------------------------------- #
from scrape_and_notify import (
    load_sent,   # lee sent_articles.json
    save_sent,   # guarda sent_articles.json
)

# === Cargar configuración de fuentes ===
with open("sources.yml", "r", encoding="utf-8") as f:
    SOURCES = yaml.safe_load(f)

# --------------------------------------------------------------------------- #
# Utilidades de envío a Telegram (sin cambios)                               #
# --------------------------------------------------------------------------- #

def _escape_md(text: str) -> str:
    specials = r"_*[]()~`>#+-=|{}.!\\"
    return re.sub(f"([{re.escape(specials)}])", r"\\\1", text)


def _send_telegram_md(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": False,
    }
    requests.post(url, json=payload, timeout=10).raise_for_status()


def send_article(article: dict) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    headline_md   = _escape_md(article["headline"])
    description_md = _escape_md(article["description"])
    link_md       = _escape_md(article["link"])

    parts = [
        f"*{headline_md}*",
        "",
        description_md,
        "",
        f"[Lire l’article complet]({link_md})",
        "",
        "@MorrocanFinancialNews"
    ]
    caption = "\n".join([p for p in parts if p.strip()])

    # ---------- intento con imagen ----------
    photo_url = ""
    if article["image_url"]:
        try:
            head = requests.head(article["image_url"], timeout=5)
            if head.status_code == 200 and head.headers.get("Content-Type", "").startswith("image/"):
                photo_url = urllib.parse.quote(article["image_url"], safe=":/?&=#")
        except Exception as e:
            print(f"[DEBUG] HEAD image failed: {e}")

    if photo_url:
        api_url = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "MarkdownV2",
        }
        try:
            requests.post(api_url, json=payload, timeout=10).raise_for_status()
            return
        except Exception as e:
            print(f"[DEBUG] sendPhoto failed, fallback to text: {e}")

    # ---------- fallback texto ----------
    _send_telegram_md(caption)

# --------------------------------------------------------------------------- #
# Parsing de artículos (sin cambios)                                          #
# --------------------------------------------------------------------------- #

def parse_articles_generic(html: str, cfg: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    sel = cfg["selectors"]
    articles = []

    for block in soup.select(sel["container"]):
        a_tag = block.select_one(sel["headline"])
        if not a_tag:
            continue
        headline = a_tag.get_text(strip=True)
        link_raw = a_tag.get(sel.get("link_attr", "href"), "")
        if not link_raw:
            continue
        link = urljoin(cfg["base_url"], link_raw)

        description = ""
        if sel.get("description"):
            d_tag = block.select_one(sel["description"])
            if d_tag:
                description = d_tag.get_text(strip=True)

        image_url = ""
        img_sel = sel.get("image")
        if img_sel:
            if "::attr(" in img_sel:
                css, attr = re.match(r"(.+)::attr\((.+)\)", img_sel).groups()
                img_tag = block.select_one(css)
                if img_tag and img_tag.has_attr(attr):
                    image_url = urljoin(cfg["base_url"], img_tag[attr])
            else:
                img_tag = block.select_one(img_sel)
                if img_tag and img_tag.has_attr("src"):
                    image_url = urljoin(cfg["base_url"], img_tag["src"])

        date_text = ""
        if sel.get("date"):
            dt_tag = block.select_one(sel["date"])
            if dt_tag:
                date_text = dt_tag.get_text(strip=True)

        parsed_date = ""
        rex = cfg.get("date_regex")
        if rex and date_text:
            m = re.search(rex, date_text)
            if m:
                if cfg.get("month_map"):
                    day, mon_name, year = m.groups()
                    mon_num = cfg["month_map"].get(mon_name, "")
                    if mon_num:
                        parsed_date = f"{year}-{mon_num}-{int(day):02d}"
                else:
                    g1, g2, g3 = m.groups()
                    if "/" in date_text:
                        d, mth, y = g1, g2, g3
                        parsed_date = f"{y}-{int(mth):02d}-{int(d):02d}"
                    else:
                        y, mth, d = g1, g2, g3
                        parsed_date = f"{y}-{mth}-{d}"

        articles.append(
            {
                "headline": headline,
                "description": description,
                "link": link,
                "image_url": image_url,
                "date": date_text,
                "parsed_date": parsed_date,
            }
        )

    return articles

# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def main() -> None:
    today_str = date.today().isoformat()
    sent_urls = load_sent()
    print(f"[DEBUG] URLs enviadas previamente: {len(sent_urls)}")

    for src in SOURCES:
        print(f"[DEBUG] === Procesando {src['name']} ===")
        try:
            html = fetch_url(src["list_url"])
        except Exception as e:
            print(f"[ERROR] No se pudo descargar {src['list_url']}: {e}")
            continue

        articles = parse_articles_generic(html, src)
        print(f"[DEBUG] {src['name']}: {len(articles)} artículos totales")

        todays = [
            a for a in articles
            if (a["parsed_date"] == today_str or not a["parsed_date"])
            and a["link"] not in sent_urls
        ]
        print(f"[DEBUG] {src['name']}: {len(todays)} artículos nuevos para hoy")

        for idx, art in enumerate(todays, 1):
            print(f"[INFO] Enviando ({idx}/{len(todays)}) → {art['headline'][:60]}…")
            try:
                send_article(art)
                sent_urls.add(art["link"])
                time.sleep(10)
            except Exception as e:
                print(f"[ERROR] Falló el envío: {e}")

    save_sent(sent_urls)
    print(f"[DEBUG] URLs guardadas: {len(sent_urls)}")


if __name__ == "__main__":
    main()
