#!/usr/bin/env python3
"""
Medias24 LeBoursier → Telegram (@MorrocanFinancialNews)
Versión «medias24 v4-lite-fix5»
────────────────────────────────────────────────────────
• Envía artículos de los últimos 3 días (hoy, -1, -2)
• Sin dependencias externas
• Sin “tarjetas” (link-preview desactivado)
• Formato con espacios fijos y sin líneas-fecha sueltas
"""

import hashlib, json, os, re, tempfile, time, urllib.parse, requests
from datetime   import datetime, timedelta
from pathlib     import Path
from typing      import Dict, List
from bs4         import BeautifulSoup          # noqa: F401  (reserva futuro)
from requests.adapters import HTTPAdapter
from urllib3.util.retry  import Retry
from urllib.parse import urlsplit, urlunsplit, quote, quote_plus

# ───────────────────────── Config ───────────────────────── #
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY          = datetime.utcnow().date()
ALLOWED_DATES  = { (TODAY - timedelta(days=i)).isoformat() for i in range(3) }

# ───────────────────── HTTP helpers ────────────────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.4-lite-fix5)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url: str, **kw) -> requests.Response:
    r = _session().get(url, **kw)
    r.raise_for_status()
    return r

# ─────────────────── Telegram helpers ─────────────────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t: str) -> str:
    return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _mk_msg(title: str, desc: str, link: str) -> str:
    return "\n".join([
        f"*{_esc(title)}*",
        "",                           # ← línea en blanco fija
        _esc(desc),
        "",                           # ← otra línea en blanco fija
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews",
    ])

def _norm_img(url: str) -> str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net, quote(path, safe='/%'),
                       quote_plus(query, safe='=&'), frag))

def _send(title: str, desc: str, link: str, img: str | None):
    caption = _mk_msg(title, desc, link)[:1024]
    body    = _mk_msg(title, desc, link)[:4096]

    if img:
        try:
            _session().head(img, timeout=5).raise_for_status()
            safe = _norm_img(img)
            _session().post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id": TG_CHAT,
                      "photo": safe,
                      "caption": caption,
                      "parse_mode": "MarkdownV2"},
                timeout=10).raise_for_status()
            return
        except Exception:
            pass   # fallback texto

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT,
              "text": body,
              "parse_mode": "MarkdownV2",
              "disable_web_page_preview": True},   # ← sin tarjeta
        timeout=10).raise_for_status()

# ─────────────────────── Cache ────────────────────────── #
def _load_cache() -> set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(c: set):
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ──────────── Medias24 specific parser ──────────────── #
_PAT_HEADER   = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK     = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")
_PAT_DATELINE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}$")   # ej. 31-07-2025
_PAT_STRIPMD  = re.compile(r"\[([^\]]+)\]\([^)]+\)")

def _parse_medias24(md: str) -> List[Dict]:
    lines = md.splitlines()
    out: List[Dict] = []
    i = 0
    while i < len(lines):
        m = _PAT_HEADER.match(lines[i])
        if m and i + 1 < len(lines):
            d, mn, y = m.groups()
            pdate = f"{y}-{int(mn):02d}-{int(d):02d}"

            # título + enlace
            m2 = _PAT_LINK.match(lines[i + 1])
            if m2:
                title, link = m2.groups()

                # descripción → primer párrafo “limpio”
                desc = ""
                j = i + 2
                while j < len(lines):
                    txt = lines[j].strip()
                    if (not txt or
                        re.fullmatch(r"=+", txt) or
                        _PAT_DATELINE.match(txt) or
                        _PAT_LINK.match(txt)):
                        j += 1
                        continue
                    desc = _PAT_STRIPMD.sub(r"\1", re.sub(r"\s+", " ", txt)).strip(" …")
                    break

                out.append({
                    "title": title,
                    "desc":  desc,
                    "link":  link,
                    "img":   "",     # jina.ai no devuelve imágenes
                    "pdate": pdate,
                })
            i += 2
        else:
            i += 1
    return out

def fetch_medias24() -> str:
    url_html = "http://medias24.com/categorie/leboursier/actus/"
    cache    = TMP_DIR / (hashlib.md5(url_html.encode()).hexdigest() + ".md")
    if cache.exists() and cache.stat().st_mtime > time.time() - 3600:
        return cache.read_text(encoding="utf-8")

    print("[DEBUG] downloading via jina.ai")
    url_jina = f"https://r.jina.ai/http://{url_html.removeprefix('http://').removeprefix('https://')}"
    md = _safe_get(url_jina, timeout=15).text
    cache.write_text(md, encoding="utf-8")
    return md

# ───────────────────────── Main ──────────────────────── #
def main():
    cache = _load_cache()
    print("— medias24_leboursier —")

    try:
        arts = _parse_medias24(fetch_medias24())
    except Exception as e:
        print("[ERROR] Medias24:", e)
        arts = []

    print("DEBUG – lista completa parseada:")
    for a in arts:
        print(" •", a["title"][:70], "| pdate:", a["pdate"])
    print("------------------------------------------------\n")

    for a in arts:
        if a["link"] in cache:
            continue
        if a["pdate"] not in ALLOWED_DATES:
            continue
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
