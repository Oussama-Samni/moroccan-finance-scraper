"""
Scraper multi‑fuente.
Lee `sources.yml`, procesa medios financieros marroquíes y envía las noticias
del día a Telegram evitando duplicados.
"""

import os
import re
import time
import urllib.parse
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Sesión HTTP con User‑Agent personalizado                                    #
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
    """Descarga la URL; desactiva verify para casablanca‑bourse (cert intermedio)."""
    session = _get_session()
    if "casablanca-bourse.com" in urlparse(url).netloc:
        resp = session.get(url, timeout=timeout, verify=False)
    else:
        resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text

# --------------------------------------------------------------------------- #
# Cargar estado y configuración                                               #
# --------------------------------------------------------------------------- #
from scrape_and_notify import load_sent, save_sent

with open("sources.yml", "r", encoding="utf-8") as f:
    SOURCES = yaml.safe_load(f)

# --------------------------------------------------------------------------- #
# Utilidades de envío a Telegram                                              #
# --------------------------------------------------------------------------- #

def _escape_md(text: str) -> str:
    specials = r"_*[]()~`>#+-=|{}.!\\"
    return re.sub(f"([{re.escape(specials)}])", r"\\\1", text)

def _send_telegram_md(msg: str) -> None:
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": msg,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": False,
    }
    requests.post(url, json=payload, timeout=10).raise_for_status()

def send_article(article: dict) -> None:
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")

    headline = _escape_md(article["headline"])
    desc     = _escape_md(article["description"])
    link_md  = _escape_md(article["link"])

    caption = "\n".join([
        f"*{headline}*",
        "",
        desc,
        "",
        f"[Lire l’article complet]({link_md})",
        "",
        "@MorrocanFinancialNews"
    ])

    photo_url = ""
    if article["image_url"]:
        try:
            url_img = article["image_url"].replace("(", "%28").replace(")", "%29")
            r = requests.get(url_img, stream=True, timeout=5)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/"):
                photo_url = urllib.parse.quote(url_img, safe=":/?&=#")
        except Exception as e:
            print(f"[DEBUG] Image check failed: {e}")

    if photo_url:
        api = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {
            "chat_id": chat,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "MarkdownV2",
        }
        try:
            requests.post(api, json=payload, timeout=10).raise_for_status()
            return
        except Exception as e:
            print(f"[DEBUG] sendPhoto failed, fallback to text: {e}")

    _send_telegram_md(caption)

# --------------------------------------------------------------------------- #
# Parsing genérico                                                            #
# --------------------------------------------------------------------------- #

def parse_articles_generic(html: str, cfg: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    sel  = cfg["selectors"]
    arts = []

    for block in soup.select(sel["container"]):
        # ---- Titular y enlace ----
        a_tag = block.select_one(sel["headline"])
        if not a_tag:
            continue
        headline = a_tag.get_text(strip=True)
        link_raw = a_tag.get(sel.get("link_attr", "href"), "")
        if not link_raw:
            continue
        link = urljoin(cfg["base_url"], link_raw)

        # Omitir filas que apuntan al list_url (ej. L’Economiste)
        if link.rstrip("/") == cfg["list_url"].rstrip("/"):
            continue

        # ---- Descripción ----
        desc = ""
        if sel.get("description"):
            d = block.select_one(sel["description"])
            if d:
                desc = d.get_text(strip=True)

        # ---- Imagen (soporta lista y pseudo‑attr) ----
        img_url = ""
        img_sel = sel.get("image", "")
        for sel_img in [s.strip() for s in img_sel.split(",") if s.strip()]:
            if "::attr(" in sel_img:
                css, attr = re.match(r"(.+)::attr\((.+)\)", sel_img).groups()
                tag = block.select_one(css)
                if tag and tag.has_attr(attr):
                    raw = tag[attr]
                    if attr == "style" and "background-image" in raw:
                        m = re.search(r'url\\((["\\\']?)(.*?)\\1\\)', raw)
                        if m:
                            raw = m.group(2)
                    img_url = urljoin(cfg["base_url"], raw)
            else:
                tag = block.select_one(sel_img)
                if tag and tag.has_attr("src"):
                    img_url = urljoin(cfg["base_url"], tag["src"])
            if img_url:
                break

        # ---- Fecha ----
        date_txt = ""
        if sel.get("date"):
            dtag = block.select_one(sel["date"])
            if dtag:
                date_txt = dtag.get_text(strip=True)

        parsed = ""
        rex = cfg.get("date_regex")
        if rex and date_txt:
            m = re.search(rex, date_txt)
            if m:
                if cfg.get("month_map"):
                    day, mon, year = m.groups()
                    mon_num = cfg["month_map"].get(mon, "")
                    if mon_num:
                        parsed = f"{year}-{mon_num}-{int(day):02d}"
                else:
                    g1, g2, g3 = m.groups()
                    if "/" in date_txt:
                        d, mth, y = g1, g2, g3
                        parsed = f"{y}-{int(mth):02d}-{int(d):02d}"
                    else:
                        y, mth, d = g1, g2, g3
                        parsed = f"{y}-{mth}-{d}"

        arts.append({
            "headline": headline,
            "description": desc,
            "link": link,
            "image_url": img_url,
            "date": date_txt,
            "parsed_date": parsed,
        })

    return arts

# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def main() -> None:
    today = date.today().isoformat()
    sent  = load_sent()
    print(f"[DEBUG] URLs enviadas previamente: {len(sent)}")

    for src in SOURCES:
        print(f"[DEBUG] === Procesando {src['name']} ===")
        try:
            html = fetch_url(src["list_url"])
        except Exception as e:
            print(f"[ERROR] No se pudo descargar {src['list_url']}: {e}")
            continue

        arts = parse_articles_generic(html, src)
        print(f"[DEBUG] {src['name']}: {len(arts)} artículos totales")

        todays = [a for a in arts
                  if (a["parsed_date"] == today or not a["parsed_date"])
                  and a["link"] not in sent]
        print(f"[DEBUG] {src['name']}: {len(todays)} nuevos para hoy")

        for idx, art in enumerate(todays, 1):
            print(f"[INFO] Enviando ({idx}/{len(todays)}) → {art['headline'][:60]}…")
            try:
                send_article(art)
                sent.add(art["link"])
                time.sleep(10)
            except Exception as e:
                print(f"[ERROR] Falló el envío: {e}")

    save_sent(sent)
    print(f"[DEBUG] URLs guardadas: {len(sent)}")


if __name__ == "__main__":
    main()
