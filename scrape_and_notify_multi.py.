"""
Scraper multi‑fuente para Oussama.
Lee `sources.yml`, procesa cada web de finanzas marroquíes en orden,
filtra las noticias del día y las envía a Telegram evitando duplicados.
"""

import re
import time
from datetime import date
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup

# Reutilizamos utilidades del scraper original
from scrape_and_notify import (
    fetch_url,
    send_article,
    load_sent,
    save_sent,
)

# === Cargar configuración de fuentes ===
with open("sources.yml", "r", encoding="utf-8") as f:
    SOURCES = yaml.safe_load(f)


def parse_articles_generic(html: str, cfg: dict) -> list[dict]:
    """Devuelve una lista de artículos según los selectores de cfg."""
    soup = BeautifulSoup(html, "html.parser")
    sel = cfg["selectors"]

    articles = []
    for block in soup.select(sel["container"]):
        # --- titular & link ---------------------------------------------------
        a_tag = block.select_one(sel["headline"])
        if not a_tag:
            continue
        headline = a_tag.get_text(strip=True)
        link_raw = a_tag.get(sel.get("link_attr", "href"), "")
        if not link_raw:
            continue
        link = urljoin(cfg["base_url"], link_raw)

        # --- descripción ------------------------------------------------------
        description = ""
        if sel.get("description"):
            desc_tag = block.select_one(sel["description"])
            if desc_tag:
                description = desc_tag.get_text(strip=True)

        # --- imagen -----------------------------------------------------------
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

        # --- fecha ------------------------------------------------------------
        date_text = ""
        if sel.get("date"):
            date_tag = block.select_one(sel["date"])
            if date_tag:
                date_text = date_tag.get_text(strip=True)

        parsed_date = ""
        rex = cfg.get("date_regex")
        if rex and date_text:
            m = re.search(rex, date_text)
            if m:
                if cfg.get("month_map"):  # meses en francés
                    day, mon_name, year = m.groups()
                    mon_num = cfg["month_map"].get(mon_name, "")
                    if mon_num:
                        parsed_date = f"{year}-{mon_num}-{int(day):02d}"
                else:  # dd/mm/aaaa  o  aaaa-mm-dd
                    g1, g2, g3 = m.groups()
                    if "/" in date_text:        # dd/mm/aaaa
                        d, mth, y = g1, g2, g3
                        parsed_date = f"{y}-{int(mth):02d}-{int(d):02d}"
                    else:                       # aaaa-mm-dd
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

        # Filtrar por fecha de hoy (si la tiene) y deduplicar
        todays = [
            a for a in articles
            if (a["parsed_date"] == today_str or not a["parsed_date"])
            and a["link"] not in sent_urls
        ]
        print(f"[DEBUG] {src['name']}: {len(todays)} artículos nuevos para hoy")

        for idx, art in enumerate(todays, 1):
            try:
                print(f"[INFO] Enviando ({idx}/{len(todays)}) → {art['headline'][:60]}…")
                send_article(art)
                sent_urls.add(art["link"])
                time.sleep(10)  # evita spam en Telegram
            except Exception as e:
                print(f"[ERROR] Falló el envío: {e}")

    save_sent(sent_urls)
    print(f"[DEBUG] URLs guardadas: {len(sent_urls)}")


if __name__ == "__main__":
    main()
