#!/usr/bin/env python3
"""
Multi-fuente → Telegram (@MorrocanFinancialNews)
Versión “v1.3”
────────────────────────────────────────────────────────
• Finances News, L’Economiste, EcoActu, Médias24
• Normaliza URLs de imagen antes de sendPhoto
• Escapa todos los caracteres especiales de Markdown V2
• Caption ≤ 1 024 caracteres · Mensaje ≤ 4 096
• Fecha hoy sólo: envía artículos con pdate == today
• Soporta años de 2 dígitos y month_map insensible a mayúsc/minúsc
• Omite imágenes con pseudo-elementos (p.ej. "::before")
"""

import hashlib
import html
import json
import os
import re
import tempfile
import time
import requests
import yaml
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, quote_plus

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────── Config ─────────── #
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY      = date.today().isoformat()
TODAY_UTC  = datetime.now(timezone.utc).date()

# ─────────── HTTP ─────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/v1.3)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429,500,502,503,504),
        allowed_methods=frozenset(["GET","HEAD"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url: str, **kw) -> requests.Response:
    r = _session().get(url, **kw)
    r.raise_for_status()
    return r

# ───────── Telegram ───────── #
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(text: str) -> str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", text)

def _build_msg(title: str, desc: str, link: str) -> str:
    parts = [
        f"*{_esc(title)}*",
        "",
        _esc(desc),
        "",
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews",
    ]
    return "\n".join(p for p in parts if p.strip())

def _norm_img_url(url: str) -> str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((
        sch,
        net,
        quote(path, safe="/%"),
        quote_plus(query, safe="=&"),
        frag,
    ))

def _send(title: str, desc: str, link: str, img: str | None):
    caption = _build_msg(title, desc, link)[:1024]
    fullmsg = _build_msg(title, desc, link)[:4096]

    if img:
        try:
            head = _session().head(img, timeout=5)
            if head.ok and head.headers.get("Content-Type", "").startswith("image/"):
                safe = _norm_img_url(img)
                _session().post(
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
        except Exception:
            pass  # fallback to text

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": fullmsg,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        },
        timeout=10,
    ).raise_for_status()

# ───────── Cache ───────── #
def _load_cache() -> set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(cache: set[str]) -> None:
    CACHE_FILE.write_text(json.dumps(list(cache), ensure_ascii=False, indent=2))

# ─── Generic HTML parser ─── #
def _extract_first(block: BeautifulSoup, specs: str, base_url: str) -> str:
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

_MONTH_MAP = {
    k.lower(): v for k, v in {
        "Janvier":"01","Février":"02","Mars":"03","Avril":"04","Mai":"05","Juin":"06",
        "Juillet":"07","Août":"08","Septembre":"09","Octobre":"10","Novembre":"11","Décembre":"12"
    }.items()
}

def _parse_generic(src: Dict) -> List[Dict]:
    soup = BeautifulSoup(_safe_get(src["list_url"]).text, "html.parser")
    sel  = src["selectors"]
    seen = set()
    out  = []

    for bloc in soup.select(sel["container"]):
        a = bloc.select_one(sel["headline"])
        if not a:
            continue

        title = a.get_text(strip=True)
        link  = urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or link in seen:
            continue
        seen.add(link)

        desc = ""
        if sel.get("description"):
            d = bloc.select_one(sel["description"])
            desc = d.get_text(strip=True) if d else ""

        img = ""
        if sel.get("image"):
            img = _extract_first(bloc, sel["image"], src["base_url"])

        # date parsing: soporta 2 or 4 digit year
        raw_date = ""
        if sel.get("date"):
            dt = bloc.select_one(sel["date"])
            raw_date = dt.get_text(strip=True) if dt else ""
        pdate = ""
        if (rx := src.get("date_regex")) and raw_date:
            if m := re.search(rx, raw_date):
                d, mon, y = m.groups()
                year = y if len(y) == 4 else f"20{y}"
                mon_low = mon.lower()
                mm = _MONTH_MAP.get(mon_low, mon_low.zfill(2))
                pdate = f"{year}-{mm}-{int(d):02d}"

        out.append({
            "title": title,
            "desc":  desc,
            "link":  link,
            "img":   img,
            "pdate": pdate or raw_date,
        })

    return out

# ─── Medias24 via WP-JSON ─── #
API_URL = (
    "https://medias24.com/wp-json/wp/v2/posts"
    "?categories=14389&per_page=30&_embed"
)
_PAT_SIGLAS_PTS = re.compile(r"^[A-ZÉÈÎÂÀÇ][A-Z0-9ÉÈÎÂÀÇ\s]{2,20}\s+Pts$", re.ASCII)

def _clean_html(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(txt).strip()

def _parse_medias24_json() -> List[Dict]:
    cache = TMP_DIR / "medias24_wp.json"
    if cache.exists() and cache.stat().st_mtime > time.time() - 900:
        data = json.loads(cache.read_text())
    else:
        data = _safe_get(API_URL, timeout=15).json()
        cache.write_text(json.dumps(data, ensure_ascii=False))
    out = []
    for post in data:
        d_gmt = datetime.fromisoformat(post["date_gmt"].replace("Z","")).date()
        if d_gmt != TODAY_UTC:
            continue

        title = _clean_html(post["title"]["rendered"])
        link  = post["link"]

        desc_raw = post.get("excerpt", {}).get("rendered", "")
        desc = _clean_html(desc_raw)
        low = desc.lower()
        if (
            _PAT_SIGLAS_PTS.match(desc)
            or low in {"marché de change", "la séance du jour", "la bourse", "masi pts",
                       f"journée du {TODAY_UTC:%d-%m-%Y}".lower()}
        ):
            desc = ""
        img = ""
        media = post.get("_embedded", {}).get("wp:featuredmedia", [])
        if media:
            img = media[0].get("source_url", "")

        out.append({
            "title": title,
            "desc":  desc or " ",
            "link":  link,
            "img":   img,
            "pdate": str(d_gmt),
        })
    return out

# ───────── Main ───────── #
def main():
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
            if a["link"] in cache:
                continue
            if a["pdate"] != TODAY:
                continue
            print(" Enviando:", a["title"][:60])
            try:
                _send(a["title"], a["desc"], a["link"], a["img"])
                cache.add(a["link"])
                time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
