#!/usr/bin/env python3
"""
Multi-fuente → Telegram (@MorrocanFinancialNews)
Baseline v1.0 – parche pseudo-element
────────────────────────────────────────────────────────
· Funciona con cualquier web definida en sources.yml
· Usa _extract_first para procesar correctamente specs con ::attr(...)
· Evita el NotImplementedError de SoupSieve
"""

import hashlib
import json
import os
import re
import tempfile
import time
import requests
import yaml
from datetime       import date, datetime, timedelta, timezone
from pathlib        import Path
from typing         import Dict, List
from urllib.parse   import urljoin, urlsplit, urlunsplit, quote, quote_plus
from bs4            import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────── Config ─────────── #
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

# Para Medias24 JSON (solo si la fuente lo requiere)
TODAY_UTC  = datetime.now(timezone.utc).date()

# ─────────── HTTP ─────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/v1.0)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url:str, **kw) -> requests.Response:
    r = _session().get(url, **kw); r.raise_for_status(); return r

# ───────── Telegram ───────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t:str) -> str:
    return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _mk_msg(title:str, desc:str, link:str) -> str:
    return "\n".join([
        f"*{_esc(title)}*",
        "",
        _esc(desc),
        "",
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews"
    ])

def _norm_img(url:str) -> str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net, quote(path, safe='/%'),
                       quote_plus(query, safe='=&'), frag))

def _send(title:str, desc:str, link:str, img:str|None):
    caption = _mk_msg(title, desc, link)[:1024]
    body    = _mk_msg(title, desc, link)[:4096]

    if img:
        try:
            _session().head(img, timeout=5).raise_for_status()
            safe = _norm_img(img)
            _session().post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id": TG_CHAT, "photo": safe,
                      "caption": caption, "parse_mode": "MarkdownV2"},
                timeout=10
            ).raise_for_status()
            return
        except Exception:
            pass  # fallback a texto

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": body,
              "parse_mode": "MarkdownV2",
              "disable_web_page_preview": True},
        timeout=10
    ).raise_for_status()

# ───────── Cache ───────── #
def _load_cache() -> set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(c:set[str]) -> None:
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ─── Generic HTML parser ─── #
def _extract_first(block: BeautifulSoup, specs: str, base_url: str) -> str:
    """
    Dada una cadena de specs separadas por comas (p.ej. "img::attr(src), div.thumb img"),
    devuelve la primera URL válida o cadena vacía.
    Maneja tanto selector::attr(xxx) como selector simple (busca atributo src).
    """
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if "::attr(" in spec:
            css, attr = re.match(r"(.+)::attr\((.+)\)", spec).groups()
            tag = block.select_one(css)
            if tag and tag.has_attr(attr):
                raw = tag[attr]
                # extrae url() de style si corresponde
                if attr == "style" and "background-image" in raw:
                    m = re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    raw = m.group(2) if m else raw
                return urljoin(base_url, raw)
        else:
            tag = block.select_one(spec)
            if tag and tag.has_attr("src"):
                return urljoin(base_url, tag["src"])
    return ""

def _parse_generic(src:Dict) -> List[Dict]:
    soup = BeautifulSoup(_safe_get(src["list_url"]).text, "html.parser")
    sel  = src["selectors"]
    seen = set()
    out  = []

    for b in soup.select(sel["container"]):
        a = b.select_one(sel["headline"])
        if not a: continue

        title = a.get_text(strip=True)
        link  = urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or link in seen:
            continue
        seen.add(link)

        desc = ""
        if sel.get("description"):
            d = b.select_one(sel["description"])
            desc = d.get_text(strip=True) if d else ""

        # — aquí va el parche: nunca usar select_one directo sobre ::attr
        img = ""
        if sel.get("image"):
            img = _extract_first(b, sel["image"], src["base_url"])

        # fecha
        raw_date = ""
        if sel.get("date"):
            dt = b.select_one(sel["date"])
            raw_date = dt.get_text(strip=True) if dt else ""
        parsed = ""
        if (rx:=src.get("date_regex")) and raw_date and (m:=re.search(rx,raw_date)):
            if src.get("month_map"):
                d,mon,y = m.groups()
                mon2 = src["month_map"].get(mon, mon)
                parsed = f"{y}-{mon2}-{int(d):02d}"
            else:
                d,mn,y = m.groups()
                parsed = f"{y}-{int(mn):02d}-{int(d):02d}"

        out.append({
            "title": title,
            "desc":  desc,
            "link":  link,
            "img":   img,
            "pdate": parsed or raw_date,
        })

    return out

# ─── Medias24 via WP-JSON ─── #
API_URL = (
    "https://medias24.com/wp-json/wp/v2/posts"
    "?categories=14389&per_page=20&_embed"
)
_PAT_SIGLAS = re.compile(r"^[A-ZÉÈÎÂÀÇ][A-Z0-9ÉÈÎÂÀÇ\s]{2,20}\s+Pts$", re.ASCII)

def _clean_html(raw:str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\s+", " ", txt).strip()

def _parse_medias24_json() -> List[Dict]:
    cache = TMP_DIR/"medias24_wp.json"
    if cache.exists() and cache.stat().st_mtime > time.time()-900:
        data = json.loads(cache.read_text())
    else:
        data = _safe_get(API_URL, timeout=15).json()
        cache.write_text(json.dumps(data, ensure_ascii=False))
    out = []
    for post in data:
        # filtrar por fecha UTC = hoy
        d = datetime.fromisoformat(post["date_gmt"].replace("Z","")).date()
        if d != TODAY_UTC: continue

        title = _clean_html(post["title"]["rendered"])
        link  = post["link"]

        desc_raw = post.get("excerpt",{}).get("rendered","")
        desc = _clean_html(desc_raw)
        # desc que son solo tags frecuentes → vacío
        low = desc.lower()
        if (_PAT_SIGLAS.match(desc)
            or low in {"marché de change","la séance du jour","la bourse",
                       f"journée du {TODAY_UTC:%d-%m-%Y}".lower()}):
            desc = ""

        img = ""
        m = post.get("_embedded",{}).get("wp:featuredmedia",[])
        if m:
            img = m[0].get("source_url","")

        out.append({"title":title,"desc":desc or " ",
                    "link":link,"img":img,"pdate":str(d)})
    return out

# ───────── Main ───────── #
def main():
    today = date.today().isoformat()
    cache = _load_cache()
    sources = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))

    for src in sources:
        name = src["name"]
        print(f"— {name} —")

        if name == "medias24_leboursier":
            arts = _parse_medias24_json()
        else:
            arts = _parse_generic(src)

        print("DEBUG – lista completa parseada:")
        for a in arts:
            print(" •", a["title"][:70], "| pdate:", a["pdate"])
        print("------------------------------------------------\n")

        for a in arts:
            if a["link"] in cache:       continue
            if a["pdate"] and a["pdate"] != today: continue
            try:
                print(" Enviando:", a["title"][:60])
                _send(a["title"], a["desc"], a["link"], a["img"])
                cache.add(a["link"])
                time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
