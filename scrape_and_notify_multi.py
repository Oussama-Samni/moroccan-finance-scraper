#!/usr/bin/env python3
"""
Medias24 LeBoursier → Telegram (@MorrocanFinancialNews)
Versión “medias24 v5-wpjson-today”
────────────────────────────────────────────────────────
• Consulta la API WordPress de Medias24 (categoría Actus, id 5877)
• Envía **solo** las noticias publicadas hoy (fecha UTC)
• Mantiene salto tras “:”, imagen destacada, cache de 15 min, etc.
"""

import html, json, os, re, time, hashlib, tempfile, requests
from datetime   import datetime, timezone
from pathlib     import Path
from typing      import List, Dict
from urllib.parse import urlsplit, urlunsplit, quote, quote_plus
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ───────── Config ───────── #
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY_UTC = datetime.now(timezone.utc).date().isoformat()   # ← solo hoy
ALLOWED_DATES = {TODAY_UTC}

CAT_ID   = 5877                                    # “Actus”
API_URL  = "https://medias24.com/wp-json/wp/v2/posts"
QUERY    = f"?categories={CAT_ID}&per_page=20&_embed"

# ───────── HTTP ───────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/2.0-wpjson)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url: str, **kw) -> requests.Response:
    r = _session().get(url, **kw)
    r.raise_for_status()
    return r

# ───────── Telegram helpers ───────── #
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t: str) -> str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _newline_title(t: str) -> str:
    return re.sub(r"\s*:\s*", ":\n", t, count=1)

def _mk_msg(title: str, desc: str, link: str) -> str:
    return "\n".join([
        f"*{_esc(_newline_title(title))}*",
        "",
        _esc(desc),
        "",
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
            pass  # fallback texto

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT,
              "text": body,
              "parse_mode": "MarkdownV2",
              "disable_web_page_preview": True},
        timeout=10).raise_for_status()

# ───────── Cache ───────── #
def _load_cache() -> set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(c: set):
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ──────── Helpers ─────── #
_TAG_RE = re.compile(r"<[^>]+>")
def _clean_html(txt: str) -> str:
    return _TAG_RE.sub("", html.unescape(txt)).strip()

# ──────── Fetch & parse ─────── #
def fetch_wp_json() -> List[Dict]:
    cache_f = TMP_DIR / "medias24_actus.json"
    if cache_f.exists() and cache_f.stat().st_mtime > time.time() - 900:
        return json.loads(cache_f.read_text())

    print("[DEBUG] downloading WP JSON")
    data = _safe_get(API_URL + QUERY, timeout=15).json()
    cache_f.write_text(json.dumps(data), encoding="utf-8")
    return data

def _parse_items(items: List[Dict]) -> List[Dict]:
    out = []
    for post in items:
        pdate = post["date_gmt"][:10]

        title = _clean_html(post["title"]["rendered"])
        desc = _clean_html(
            (post.get("yoast_head_json") or {}).get("description")
            or post.get("excerpt", {}).get("rendered", "")
        ) or " "

        link = post["link"]

        img = ""
        media = post.get("_embedded", {}).get("wp:featuredmedia")
        if media and isinstance(media, list) and media[0].get("source_url"):
            img = media[0]["source_url"]

        out.append({"title": title, "desc": desc, "link": link,
                    "img": img, "pdate": pdate})
    return out

# ───────── Main ───────── #
def main():
    cache = _load_cache()
    print("— medias24_leboursier (WP JSON) —")

    try:
        arts = _parse_items(fetch_wp_json())
    except Exception as e:
        print("[ERROR] Medias24:", e); arts = []

    print("DEBUG – parseados:")
    for a in arts:
        print(" •", a["title"][:70], "|", a["pdate"])
    print("------------------------------------------------\n")

    for a in arts:
        if a["pdate"] != TODAY_UTC:
            continue
        if a["link"] in cache:
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
