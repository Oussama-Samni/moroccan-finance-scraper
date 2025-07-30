#!/usr/bin/env python3
"""
Finances News · L’Economiste · EcoActu · Médias24  →  Telegram
Baseline robusto  v1.3‑RSSUA
-------------------------------------------------------------
• Normaliza URLs de imagen antes de sendPhoto
• Escapa TODOS los caracteres especiales de Markdown V2
• Caption ≤ 1 024 caracteres · Mensaje ≤ 4 096
• Para Médias24 feed: User‑Agent de lector RSS + Accept XML
"""

import json, os, re, time, sys, urllib.parse, requests, yaml
from datetime import date
from pathlib   import Path
from typing    import Dict, List
from bs4       import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import (
    urljoin, urlsplit, urlunsplit, quote, quote_plus
)

SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")

# ───────────────────────── Session ───────────────────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        # UA típico de lector RSS (Feedbin) para saltar filtros anti‑bot
        "User-Agent":
            "Mozilla/5.0 (compatible; Feedbin/2.0; +https://feedbin.com)",
        "Accept-Language": "fr,en;q=0.8",
        # Declaramos preferencia por XML/RSS
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def fetch(url: str, timeout: float = 10.0) -> str:
    r = _session().get(url, timeout=timeout)
    r.raise_for_status()      # se captura en main()
    return r.text

# ───────────── Cache de URLs enviadas ───────────── #
def _load_cache() -> set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(cache: set) -> None:
    CACHE_FILE.write_text(json.dumps(list(cache), ensure_ascii=False, indent=2))

# ───────────────────── Telegram ───────────────────── #
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _escape_md(t: str) -> str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _build_msg(head: str, desc: str, link: str) -> str:
    parts = [
        f"*{_escape_md(head)}*",
        "",                       # línea en blanco
        _escape_md(desc),
        "",
        f"[Lire l’article complet]({_escape_md(link)})",
        "",
        "@MorrocanFinancialNews",
    ]
    # mantenemos líneas vacías para el espaciado deseado
    return "\n".join(parts)

def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"

def _norm_img_url(url: str) -> str:
    """Escapa (), espacios y UTF‑8 en path/query; deja esquema + host intactos."""
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((
        sch,
        net,
        quote(path, safe="/%"),
        quote_plus(query, safe="=&"),
        frag,
    ))

def _send_telegram(head: str, desc: str, link: str, img: str | None):
    caption = _truncate(_build_msg(head, desc, link), 1_024)
    fullmsg = _truncate(_build_msg(head, desc, link), 4_096)

    if img:
        try:
            h = requests.head(img, timeout=5)
            if h.ok and h.headers.get("Content-Type", "").startswith("image/"):
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={
                        "chat_id": TG_CHAT,
                        "photo": _norm_img_url(img),
                        "caption": caption,
                        "parse_mode": "MarkdownV2",
                    },
                    timeout=10,
                ).raise_for_status()
                return
        except Exception as e:
            print("[WARN] sendPhoto falló – envío texto:", e, file=sys.stderr)

    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": fullmsg,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        },
        timeout=10,
    ).raise_for_status()

# ────────────── Helpers de parsing ─────────────── #
def _extract_first(block: BeautifulSoup, specs: str, base_url: str) -> str:
    """
    Devuelve la primera URL de imagen válida según spec:
      • "selector::attr(src)"  (EcoActu usa style)
      • "selector"             (atributo src)
    Múltiples specs separados por coma → prueba en cadena.
    """
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if "::attr(" in spec:
            css, attr = re.match(r"(.+)::attr\((.+)\)", spec).groups()
            tag = block.select_one(css)
            if tag and tag.has_attr(attr):
                raw = tag[attr]
                if attr == "style" and "background-image" in raw:
                    m = re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    raw = m.group(2) if m else raw
                return urljoin(base_url, raw)
        else:
            tag = block.select_one(spec)
            if tag and tag.has_attr("src"):
                return urljoin(base_url, tag["src"])
    return ""

def _parse(src: Dict) -> List[Dict]:
    try:
        html = fetch(src["list_url"])
    except requests.HTTPError as e:
        print(f"[WARN] {src['name']} – omitido por error: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(html, "html.parser")
    sel  = src["selectors"]
    seen: set[str] = set()          # FinancesNews duplica la 1.ª noticia
    out : List[Dict] = []

    for bloc in soup.select(sel["container"]):
        a = bloc.select_one(sel["headline"])
        if not a:
            continue
        title = a.get_text(strip=True)
        link  = urljoin(src["base_url"], a.get(sel.get("link_attr", "href"), ""))
        if not link or (src["name"] == "financesnews" and link in seen):
            continue
        seen.add(link)

        desc = ""
        if sel.get("description"):
            d = bloc.select_one(sel["description"])
            if d:
                desc = d.get_text(strip=True)

        img = _extract_first(bloc, sel.get("image", ""), src["base_url"]) if sel.get("image") else ""

        raw_date = ""
        if sel.get("date"):
            d = bloc.select_one(sel["date"])
            if d:
                raw_date = d.get_text(strip=True)

        parsed = ""
        if (rx := src.get("date_regex")) and raw_date and (m := re.search(rx, raw_date)):
            if src.get("month_map"):
                d, mon, y = m.groups()
                if (mm := src["month_map"].get(mon)):
                    parsed = f"{y}-{mm}-{int(d):02d}"
            else:
                d, mn, y = m.groups()
                parsed = f"{y}-{int(mn):02d}-{int(d):02d}"

        out.append({
            "title": title,
            "desc":  desc,
            "link":  link,
            "img":   img,
            "pdate": parsed or raw_date,
        })
    return out

# ───────────────────────── Main ───────────────────────── #
def main():
    today  = date.today().isoformat()
    cache  = _load_cache()
    sources = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))

    ACTIVE = {"medias24_leboursier"}   # ← sólo probamos esta fuente aquí

    for src in sources:
        if src["name"] not in ACTIVE:
            continue

        print(f"— {src['name']} —")
        articles = _parse(src)

        print("DEBUG – lista completa parseada:")
        for art in articles:
            print(" •", art["title"][:70], "| pdate:", art["pdate"])
        print("------------------------------------------------")

        for art in articles:
            if art["link"] in cache:
                continue
            if art["pdate"] and art["pdate"] != today:
                continue
            try:
                print(" Enviando:", art["title"][:60])
                _send_telegram(art["title"], art["desc"], art["link"], art["img"])
                cache.add(art["link"])
                time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e, file=sys.stderr)

    _save_cache(cache)

if __name__ == "__main__":
    main()
