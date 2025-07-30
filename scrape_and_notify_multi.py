"""
Scraper → Telegram (@MorrocanFinancialNews)
==========================================

Fuentes actuales
  • FinancesNews      – https://fnh.ma/articles/actualite-financiere-maroc
  • L’Economiste Eco  – https://www.leconomiste.com/categorie/Economie

Añadir nuevas fuentes:
  1) Bloque en sources.yml
  2) Si hace falta lógica extra, tocar `_postprocess()` o `_meta_description()`
"""

import json, os, re, time, urllib.parse, requests, yaml
from datetime import date
from pathlib   import Path
from typing    import Dict, List
from bs4       import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin

# ───────────────────── Configuración ───────────────────── #
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")

# ──────────────────── Utilidades HTTP ──────────────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.0; "
            "+https://github.com/OussamaSamni/moroccan-finance-scraper)"
        ),
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(
        total=4, backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"])
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def fetch(url: str, timeout: float = 10.0) -> str:
    r = _session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

# ─────────────── Cache de URLs enviadas ─────────────── #
def _load_cache() -> set:
    if CACHE_FILE.exists():
        return set(json.loads(CACHE_FILE.read_text()))
    return set()

def _save_cache(cache: set) -> None:
    CACHE_FILE.write_text(json.dumps(list(cache), ensure_ascii=False, indent=2))

# ───────────────────── Telegram ───────────────────── #
def _escape_md(text: str) -> str:
    return re.sub(r"([_*[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)

def _send_telegram(head: str, desc: str, link: str,
                   image_url: str | None = None) -> None:
    msg = "\n".join(filter(None, [
        f"*{_escape_md(head)}*",
        "",
        _escape_md(desc),
        "",
        f"[Lire l’article complet]({_escape_md(link)})",
        "",
        "@MorrocanFinancialNews"
    ]))

    if image_url:
        try:
            img_ok = requests.head(image_url, timeout=5)
            if img_ok.ok and img_ok.headers.get("Content-Type", "").startswith("image/"):
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={
                        "chat_id": TG_CHAT,
                        "photo": image_url,
                        "caption": msg,
                        "parse_mode": "MarkdownV2",
                    },
                    timeout=10,
                ).raise_for_status()
                return
        except Exception as e:
            print("[WARN] imagen falló, envío texto:", e)

    # Fallback texto
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": msg,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        },
        timeout=10,
    ).raise_for_status()

# ───────────── helpers específicos ───────────── #
def _meta_description(url: str) -> str:
    """Extrae og:description para listados sin snippet (L’Economiste)."""
    try:
        html = fetch(url, 8)
        m = re.search(
            r'<meta property="og:description"\s+content="([^"]+)"',
            html, re.I
        )
        return m.group(1).strip() if m else ""
    except Exception:
        return ""

# ───────────────── Parsing genérico ───────────────── #
def _parse(src: Dict) -> List[Dict]:
    html = fetch(src["list_url"])
    soup = BeautifulSoup(html, "html.parser")
    sel  = src["selectors"]

    seen_links: set[str] = set()     # evita duplicados FinancesNews
    arts: List[Dict] = []

    for bloc in soup.select(sel["container"]):
        a = bloc.select_one(sel["headline"])
        if not a:
            continue
        title = a.get_text(strip=True)
        href  = urljoin(src["base_url"], a.get(sel.get("link_attr", "href"), ""))
        if not href:
            continue

        if src["name"] == "financesnews" and href in seen_links:
            continue
        seen_links.add(href)

        desc = ""
        if sel.get("description"):
            d = bloc.select_one(sel["description"])
            if d:
                desc = d.get_text(strip=True)
        # fallback meta‑description para L’Economiste
        if not desc and src["name"].startswith("leconomiste"):
            desc = _meta_description(href)

        img_url = ""
        if sel.get("image"):
            css = sel["image"].split("::attr(")[0]
            img_tag = bloc.select_one(css)
            if img_tag and img_tag.has_attr("src"):
                img_url = urljoin(src["base_url"], img_tag["src"])

        raw_date = ""
        if sel.get("date"):
            d = bloc.select_one(sel["date"])
            if d:
                raw_date = d.get_text(strip=True)

        parsed = ""
        rx = src.get("date_regex")
        if rx and raw_date and (m := re.search(rx, raw_date)):
            if src.get("month_map"):           # FinancesNews (fr)
                dd, mon, yy = m.groups()
                mm = src["month_map"].get(mon)
                if mm:
                    parsed = f"{yy}-{mm}-{int(dd):02d}"
            else:                              # L’Economiste numérico
                dd, mm, yy = m.groups()
                yy = yy if len(yy) == 4 else "20" + yy   # 25 → 2025
                parsed = f"{yy}-{int(mm):02d}-{int(dd):02d}"

        arts.append({
            "title": title,
            "desc":  desc,
            "link":  href,
            "img":   img_url,
            "pdate": parsed or raw_date,
        })
    return arts

# ─────────────────── Flujo principal ─────────────────── #
def main() -> None:
    today  = date.today().isoformat()
    cache  = _load_cache()
    sources = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))

    # limitar a las fuentes actualmente soportadas
    valid_sources = {"financesnews", "leconomiste_economie"}

    for src in sources:
        if src["name"] not in valid_sources:
            continue

        print(f"— {src['name']} —")
        arts_all = _parse(src)

        # DEBUG
        print("\nDEBUG – lista completa parseada:")
        for a in arts_all:
            print(" •", a["title"][:70], "| pdate:", a["pdate"])
        print("------------------------------------------------\n")

        for art in arts_all:
            if art["link"] in cache:
                continue
            if art["pdate"] and art["pdate"] != today:
                continue

            print(" Enviando:", art["title"][:60])
            try:
                _send_telegram(art["title"], art["desc"], art["link"], art["img"])
                cache.add(art["link"])
                time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)


if __name__ == "__main__":
    main()
