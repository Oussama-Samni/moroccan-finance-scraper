#!/usr/bin/env python3
"""
Medias24 LeBoursier → Telegram (@MorrocanFinancialNews)
Versión “medias24 v5-json”
────────────────────────────────────────────────────────
• Obtiene las noticias vía API WP-JSON (cat 14389, _embed)
• Envía únicamente los artículos publicados HOY (hora UTC)
• Inserta salto tras el primer “:”
• Filtra tablas, etiquetas genéricas, fechas sueltas y “SIGLAS Pts”
• Desactiva siempre la vista previa de enlaces
"""

import hashlib, json, os, re, tempfile, time, requests, html
from datetime     import datetime, timezone
from pathlib       import Path
from typing        import List
from urllib.parse  import urlsplit, urlunsplit, quote, quote_plus
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────── Config ─────────── #
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY_UTC = datetime.now(timezone.utc).date()     # día actual en UTC

API_URL   = (
    "https://medias24.com/wp-json/wp/v2/posts"
    "?categories=14389&per_page=30&_embed"        # cat «Actus»
)

# ─────────── HTTP ─────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/v5-json)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url:str, **kw) -> requests.Response:
    r = _session().get(url, **kw); r.raise_for_status(); return r

# ───────── Telegram ───────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t:str)->str: return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _newline_title(t:str)->str:
    return re.sub(r"\s*:\s*", ":\n", t, count=1)

def _mk_msg(title:str, desc:str, link:str)->str:
    return "\n".join([
        f"*{_esc(_newline_title(title))}*",
        "",
        _esc(desc),
        "",
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews",
    ])

def _norm_img(url:str)->str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net, quote(path, safe='/%'),
                       quote_plus(query, safe='=&'), frag))

def _send(title:str, desc:str, link:str, img:str|None):
    caption = _mk_msg(title, desc, link)[:1024]
    body    = _mk_msg(title, desc, link)[:4096]

    if img:
        try:
            _session().head(img, timeout=5).raise_for_status()
            _session().post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id": TG_CHAT, "photo": _norm_img(img),
                      "caption": caption, "parse_mode": "MarkdownV2"},
                timeout=10).raise_for_status()
            return
        except Exception:
            pass   # fallback texto

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": body,
              "parse_mode": "MarkdownV2",
              "disable_web_page_preview": True},
        timeout=10).raise_for_status()

# ───────── Cache ───────── #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set): CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ──────── Parsing helpers ─────── #
_PAT_SIGLAS_PTS = re.compile(r"^[A-ZÉÈÎÂÀÇ][A-Z0-9ÉÈÎÂÀÇ\s]{2,20}\s+Pts$", re.ASCII)

def _clean_html(raw:str)->str:
    txt = re.sub(r"<[^>]+>", "", raw)      # quita etiquetas
    return html.unescape(txt).strip()

def _parse_posts(json_list:list)->List[dict]:
    arts=[]
    for post in json_list:
        # Fecha en UTC
        d_gmt = datetime.fromisoformat(post["date_gmt"].replace("Z","")).date()
        if d_gmt != TODAY_UTC:
            continue

        title = html.unescape(post["title"]["rendered"]).strip()
        link  = post["link"]

        # descripción: toma excerpt
        desc_raw = post.get("excerpt", {}).get("rendered", "") or ""
        desc = _clean_html(desc_raw)
        # filtros finales: si desc es etiqueta freq -> lo vaciamos
        if (_PAT_SIGLAS_PTS.match(desc)
            or desc.lower() in {"marché de change", "la séance du jour",
                                "la bourse", "masi pts",
                                f"journée du {TODAY_UTC.strftime('%d-%m-%Y')}".lower()}):
            desc = ""

        # imagen destacada
        img = ""
        media = post.get("_embedded", {}).get("wp:featuredmedia", [])
        if media:
            img = media[0].get("source_url","")

        arts.append({"title":title, "desc":desc or " ",
                     "link":link,  "img":img, "pdate": str(d_gmt)})
    return arts

def fetch_medias24_json()->list:
    cache = TMP_DIR / "medias24_wp.json"
    if cache.exists() and cache.stat().st_mtime > time.time()-900:   # 15 min
        return json.loads(cache.read_text())

    print("[DEBUG] downloading WP JSON")
    data = _safe_get(API_URL, timeout=15).json()
    cache.write_text(json.dumps(data, ensure_ascii=False))
    return data

# ───────── Main ───────── #
def main():
    cache = _load_cache()
    print("— medias24_leboursier (WP JSON) —")

    try:
        posts_json = fetch_medias24_json()
        arts = _parse_posts(posts_json)
    except Exception as e:
        print("[ERROR] Medias24:", e); arts=[]

    print("DEBUG – parseados:")
    for a in arts: print(" •", a["title"][:70])
    print("------------------------------------------------")

    for a in arts:
        if a["link"] in cache:
            continue
        try:
            print(" Enviando:", a["title"][:60])
            _send(a["title"], a["desc"], a["link"], a["img"])
            cache.add(a["link"]); time.sleep(8)
        except Exception as e:
            print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
