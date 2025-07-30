#!/usr/bin/env python3
"""
Medias24 (LeBoursier › Actus) → Telegram (@MorrocanFinancialNews)
Versión de prueba «acepta ayer» v0.1
────────────────────────────────────────────────────────────────────
• Scrapea vía jina.ai (Markdown) para evitar el 403
• Imagen opcional (no hay en el feed, pero se mantiene la lógica)
• Escapa Markdown V2 ‑ caption ≤ 1 024, body ≤ 4 096
• ENV requeridas: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import json, os, re, time, hashlib, tempfile, requests
from datetime import date, timedelta
from pathlib   import Path
from typing    import Dict, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlsplit, urlunsplit, quote, quote_plus

# ─────────────── Config ─────────────── #
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)
CACHE_FILE = Path("sent_articles.json")

# *** Cambia esto a False cuando sólo quieras el día actual ***
ACCEPT_YESTERDAY = True
# ────────────────────────────────────── #

# ---------- HTTP ----------- #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/0.1)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url:str, **kw)->requests.Response:
    r = _session().get(url, **kw)
    r.raise_for_status()
    return r

# ---------- Telegram helpers ---------- #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"   # caracteres que hay que escapar
def _esc(t:str)->str: return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _norm_img(url:str)->str:
    sch,net,path,query,frag = urlsplit(url)
    path  = quote(path,  safe="/%")
    query = quote_plus(query, safe="=&")
    return urlunsplit((sch,net,path,query,frag))

def _build_msg(title:str, desc:str, link:str)->str:
    return "\n".join([
        f"*{_esc(title)}*",
        "",
        _esc(desc),
        "",
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews"
    ])

def _send_tg(title:str, desc:str, link:str, img:str|None):
    caption = _build_msg(title,desc,link)[:1024]
    body    = _build_msg(title,desc,link)[:4096]

    if img:
        try:
            _session().head(img, timeout=5).raise_for_status()
            safe = _norm_img(img)
            _session().post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id":TG_CHAT,"photo":safe,
                      "caption":caption,"parse_mode":"MarkdownV2"},
                timeout=10).raise_for_status()
            return
        except Exception as e:
            print("[WARN] sendPhoto falló, envío texto:", e)

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":body,
              "parse_mode":"MarkdownV2","disable_web_page_preview":False},
        timeout=10).raise_for_status()

# ---------- Cache ---------- #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set): CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ---------- Medias24 parsing ---------- #
_PAT_HEADER = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK   = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")

def _parse_markdown(md:str)->List[Dict]:
    lines = md.splitlines()
    out:List[Dict] = []
    i=0
    while i < len(lines):
        m = _PAT_HEADER.match(lines[i])
        if m and i+1 < len(lines):
            d,mn,y = m.groups()
            date_parsed = f"{y}-{int(mn):02d}-{int(d):02d}"
            m2 = _PAT_LINK.match(lines[i+1])
            if m2:
                title, link = m2.groups()
                # busca el primer párrafo descriptivo que no sea vacío ni otro link/img
                desc=""
                j=i+2
                while j<len(lines) and (not lines[j].strip() or lines[j].startswith("![")):
                    j+=1
                if j<len(lines):
                    desc = re.sub(r"\s+", " ", lines[j]).strip(" …")
                out.append({"title":title, "desc":desc, "link":link,
                            "img":"", "pdate":date_parsed})
            i += 2
        else:
            i += 1
    return out

def fetch_markdown()->str:
    url_html = "http://medias24.com/categorie/leboursier/actus/"
    cache    = TMP_DIR / (hashlib.md5(url_html.encode()).hexdigest()+".md")
    if cache.exists() and cache.stat().st_mtime > (time.time() - 3600):
        return cache.read_text(encoding="utf-8")

    print("[DEBUG] downloading via jina.ai")
    url_jina = f"https://r.jina.ai/http://{url_html.removeprefix('https://').removeprefix('http://')}"
    md = _safe_get(url_jina, timeout=15).text
    cache.write_text(md, encoding="utf-8")
    return md

# ---------- Main ---------- #
def main()->None:
    today      = date.today().isoformat()
    yesterday  = (date.today() - timedelta(days=1)).isoformat()
    cache      = _load_cache()

    print("— medias24_leboursier —")
    md     = fetch_markdown()
    arts   = _parse_markdown(md)

    print("DEBUG – lista completa parseada:")
    for a in arts: print(" •", a['title'][:70], "| pdate:", a['pdate'])
    print("------------------------------------------------\n")

    for a in arts:
        if a["link"] in cache:
            continue
        if not (
            a["pdate"] == today or (ACCEPT_YESTERDAY and a["pdate"] == yesterday)
        ):
            continue

        try:
            print(" Enviando:", a["title"][:60])
            _send_tg(a["title"], a["desc"], a["link"], a["img"])
            cache.add(a["link"])
            time.sleep(8)
        except Exception as e:
            print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
