#!/usr/bin/env python3
"""
Médias 24 (LeBoursier RSS) → Telegram (@MorrocanFinancialNews)
--------------------------------------------------------------
Baseline v 1.3.1  ·  rama *medias24-dev*
• Normaliza URLs de imagen antes de *sendPhoto*
• Escapa TODOS los caracteres especiales de Markdown V2
• Caption ≤ 1 024 · Mensaje ≤ 4 096
• Manejo 403 Médias24 → cambia dinámicamente el User‑Agent
"""

import json, os, re, time, urllib.parse, requests, yaml
from datetime import date
from pathlib   import Path
from typing    import Dict, List
from bs4       import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, quote_plus

SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")

# ─────────────────────── Session ─────────────────────── #
_GENERIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_BOT_UA = (
    "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.3.1; "
    "+https://github.com/OussamaSamni/moroccan-finance-scraper)"
)

def _make_session(ua: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def fetch(url: str, timeout: float = 10.0) -> str:
    """
    • Para medias24.com usamos un UA “de navegador” para evitar el 403.
    • Resto de sitios siguen con el UA bot.
    """
    ua = _GENERIC_UA if "medias24.com" in url else _BOT_UA
    r  = _make_session(ua).get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

# ───────────────── Cache URLs enviadas ───────────────── #
def _load_cache() -> set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(cache: set) -> None:
    CACHE_FILE.write_text(json.dumps(list(cache), ensure_ascii=False, indent=2))

# ─────────────────────── Telegram ─────────────────────── #
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _escape_md(t: str) -> str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _build_msg(head: str, desc: str, link: str) -> str:
    parts = [
        f"*{_escape_md(head)}*",
        "",                       # 1 línea en blanco
        _escape_md(desc),
        "",                       # otra línea en blanco
        f"[Lire l’article complet]({_escape_md(link)})",
        "",                       # espacio antes de la firma
        "@MorrocanFinancialNews",
    ]
    return "\n".join(p for p in parts if p.strip() or p == "")

def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"

def _norm_img_url(url: str) -> str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit(
        (
            sch,
            net,
            quote(path, safe="/%"),
            quote_plus(query, safe="=&"),
            frag,
        )
    )

def _send_telegram(head: str, desc: str, link: str, img: str | None):
    caption = _truncate(_build_msg(head, desc, link), 1_024)
    fullmsg = _truncate(_build_msg(head, desc, link), 4_096)

    if img:
        try:
            r_head = requests.head(img, timeout=5)
            if r_head.ok and r_head.headers.get("Content-Type", "").startswith("image/"):
                safe = _norm_img_url(img)
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={
                        "chat_id": TG_CHAT,
                        "photo": safe,
                        "caption": caption,
                        "parse_mode": "MarkdownV2",
                    },
                    timeout=10,
                ).raise_for_status()
                return
        except Exception as e:
            print("[WARN] sendPhoto falló → fallback texto:", e)

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

# ───────────────────── Parsing genérico ───────────────────── #
def _parse(src: Dict) -> List[Dict]:
    soup = BeautifulSoup(fetch(src["list_url"]), "xml")  # RSS = XML
    sel  = src["selectors"]
    out: List[Dict] = []

    for bloc in soup.select(sel["container"]):
        a = bloc.select_one(sel["headline"])
        if not a:
            continue
        title = a.get_text(strip=True)

        link_tag = bloc.select_one(sel.get("link_attr", "link"))
        link     = link_tag.get_text(strip=True) if link_tag else ""
        if not link:
            continue

        desc = ""
        if sel.get("description"):
            d = bloc.select_one(sel["description"])
            if d:
                desc = BeautifulSoup(d.get_text(), "html.parser").get_text(strip=True)

        img = ""   # el feed no trae miniaturas

        raw_date = ""
        if sel.get("date"):
            dt = bloc.select_one(sel["date"])
            if dt:
                raw_date = dt.get_text(strip=True)

        parsed = ""
        rx = src.get("date_regex")
        if rx and raw_date and (m := re.search(rx, raw_date)):
            d, mon, y = m.groups()
            mm = src["month_map"].get(mon)
            if mm:
                parsed = f"{y}-{mm}-{int(d):02d}"

        out.append(
            {
                "title": title,
                "desc":  desc,
                "link":  link,
                "img":   img,
                "pdate": parsed or raw_date,
            }
        )
    return out

# ───────────────────────── Main ───────────────────────── #
def main() -> None:
    today   = date.today().isoformat()
    cache   = _load_cache()
    sources = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))

    ACTIVE = {"medias24_leboursier"}      # solo esta fuente en la rama de prueba

    for src in sources:
        if src["name"] not in ACTIVE:
            continue

        print(f"— {src['name']} —")
        try:
            arts = _parse(src)
        except Exception as e:
            print(f"[WARN] {src['name']} – omitido por error:", e)
            continue

        print("DEBUG – lista completa parseada:")
        for a in arts:
            print(" •", a["title"][:70], "| pdate:", a["pdate"])
        print("------------------------------------------------\n")

        for a in arts:
            if a["link"] in cache:
                continue
            if a["pdate"] and a["pdate"] != today:
                continue
            try:
                print(" Enviando:", a["title"][:60])
                _send_telegram(a["title"], a["desc"], a["link"], a["img"])
                cache.add(a["link"])
                time.sleep(6)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
