"""
Finances News → Telegram (@MorrocanFinancialNews)
-------------------------------------------------
Scraper autocontenido y modular.  Añadir nuevas fuentes
⇒ incluir su bloque en sources.yml y (opcional) ajuste
de _postprocess() si necesitara algo específico.
"""

import json, os, re, time, urllib.parse, requests, yaml
from datetime import date
from pathlib   import Path
from typing    import Dict, List
from bs4       import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin

# ---------- Configuración ----------
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")

# ---------- Utilidades HTTP ----------
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
                  allowed_methods=frozenset(["GET", "HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def fetch(url: str, timeout: float = 10.0) -> str:
    r = _session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

# ---------- Cache de URLs enviadas ----------
def _load_cache() -> set:
    if CACHE_FILE.exists():
        return set(json.loads(CACHE_FILE.read_text()))
    return set()

def _save_cache(cache: set) -> None:
    CACHE_FILE.write_text(json.dumps(list(cache), ensure_ascii=False, indent=2))

# ---------- Telegram ----------
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

# ---------- Parsing genérico ----------
def _parse(src: Dict) -> List[Dict]:
    html = fetch(src["list_url"])
    soup = BeautifulSoup(html, "html.parser")
    sel  = src["selectors"]

    arts = []
    for bloc in soup.select(sel["container"]):
        a = bloc.select_one(sel["headline"])
        if not a:
            continue
        title = a.get_text(strip=True)
        href  = urljoin(src["base_url"], a.get(sel.get("link_attr", "href"), ""))
        if not href:
            continue

        desc = ""
        if sel.get("description"):
            d = bloc.select_one(sel["description"])
            if d:
                desc = d.get_text(strip=True)

        img_url = ""
        if sel.get("image"):
            img_tag = bloc.select_one(sel["image"].split("::attr(")[0])
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
                d, mon, y = m.groups()
                mm = src["month_map"].get(mon)
                if mm:
                    parsed = f"{y}-{mm}-{int(d):02d}"
            else:
                d, mth, y = m.groups()
                parsed = f"{y}-{int(mth):02d}-{int(d):02d}"

        arts.append({
            "title": title,
            "desc":  desc,
            "link":  href,
            "img":   img_url,
            "pdate": parsed or raw_date
        })
    return arts

# ---------- Flujo principal ----------
def main() -> None:
    today = date.today().isoformat()
    cache = _load_cache()

    sources = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))

    for src in sources:
        if src["name"] != "financesnews":       # hay solo una, pero dejamos filtro
            continue

        print("— FinancesNews —")

        # --- DEBUG: listar todo lo que se ha parseado ---
        print("\nDEBUG – lista completa parseada:")
        for a in _parse(src):
            print(" •", a["title"][:70], "| pdate:", a["pdate"])
        print("------------------------------------------------\n")
        
        for art in _parse(src):
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
