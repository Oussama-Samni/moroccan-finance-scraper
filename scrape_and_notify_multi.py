"""
Finances News → Telegram (@MorrocanFinancialNews)
-------------------------------------------------
Añadir nuevas fuentes ⇒ bloque en sources.yml y, si hace falta,
ajuste puntual en _postprocess().
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
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET","HEAD"]))
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
    """Escapa todo para Markdown V2 (por si hay caracteres raros)."""
    return re.sub(r"([_*[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)

def _compose_caption(a: Dict, source_tag: str) -> str:
    """
    Devuelve el bloque de texto con el formato:
        Titular
        dd Mois aaaa - par <fuente>
        (línea en blanco)
        Descripción
        (línea en blanco)
        Lire l’article complet
        (línea en blanco)
        @MorrocanFinancialNews
    """
    date_line = ""
    if a.get("raw_date"):
        date_line = f"\n{_escape_md(a['raw_date'])} - par {_escape_md(source_tag)}"

    parts = [
        f"{_escape_md(a['title'])}{date_line}",
        "",
        _escape_md(a['desc']),
        "",
        f"[Lire l’article complet]({_escape_md(a['link'])})",
        "",
        "@MorrocanFinancialNews"
    ]
    return "\n".join(parts)

def _send_telegram(a: Dict, source_tag: str) -> None:
    caption = _compose_caption(a, source_tag)

    # ── Imagen (si la hay y es válida) ──────────────────────────
    if a["img"]:
        try:
            head = requests.head(a["img"], timeout=5)
            if head.ok and head.headers.get("Content-Type","").startswith("image/"):
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={
                        "chat_id": TG_CHAT,
                        "photo": a["img"],
                        "caption": caption,
                        "parse_mode": "MarkdownV2",
                    },
                    timeout=10,
                ).raise_for_status()
                return
        except Exception as e:
            print("[WARN] imagen falló, envío solo texto:", e)

    # ── Fallback texto ──────────────────────────────────────────
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": caption,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        },
        timeout=10,
    ).raise_for_status()

# ───────────────── Parsing genérico ───────────────── #
def _parse(src: Dict) -> List[Dict]:
    html = fetch(src["list_url"])
    soup = BeautifulSoup(html, "html.parser")
    sel  = src["selectors"]

    seen_links: set[str] = set()
    arts: List[Dict] = []

    for bloc in soup.select(sel["container"]):
        a = bloc.select_one(sel["headline"])
        if not a:
            continue
        title = a.get_text(strip=True)
        href  = urljoin(src["base_url"], a.get(sel.get("link_attr", "href"), ""))
        if not href or href in seen_links:
            continue
        seen_links.add(href)

        desc = ""
        if sel.get("description"):
            d = bloc.select_one(sel["description"])
            if d:
                desc = d.get_text(strip=True)

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
            if src.get("month_map"):
                day, mon, yr = m.groups()
                mm = src["month_map"].get(mon)
                if mm:
                    parsed = f"{yr}-{mm}-{int(day):02d}"
            else:
                d1, m1, y1 = m.groups()
                parsed = f"{y1}-{int(m1):02d}-{int(d1):02d}"

        arts.append({
            "title": title,
            "desc":  desc,
            "link":  href,
            "img":   img_url,
            "pdate": parsed or raw_date,
            "raw_date": raw_date,      # ⇦ necesario para la línea de fecha
        })
    return arts

# ─────────────────── Flujo principal ─────────────────── #
def main() -> None:
    today  = date.today().isoformat()
    cache  = _load_cache()
    config = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))

    for src in config:
        if src["name"] not in {"financesnews", "leconomiste_economie"}:
            continue            # de momento trabajamos con estas dos

        tag = src["name"].replace("_", " ")   # ejemplo: leconomiste_economie → leconomiste economie
        print(f"— {src['name']} —")

        arts = _parse(src)

        # DEBUG: lista completa
        print("\nDEBUG – lista completa parseada:")
        for a in arts:
            print(" •", a["title"][:70], "| pdate:", a["pdate"])
        print("------------------------------------------------\n")

        for art in arts:
            if art["link"] in cache:           # ya enviado
                continue
            if art["pdate"] and art["pdate"] != today:
                continue                       # no es de hoy

            print(" Enviando:", art["title"][:60])
            try:
                _send_telegram(art, tag)
                cache.add(art["link"])
                time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
