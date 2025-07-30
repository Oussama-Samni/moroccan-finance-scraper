#!/usr/bin/env python3
"""
Medias24 (LeBoursier)  ➜  Telegram (@MorrocanFinancialNews)
Versión “medias24 v3‑fixUnderline”
----------------------------------
· Usa jina.ai para sortear el 403, cachea markdown un día
· Escapa Markdown V2 y normaliza URLs de imagen
· Caption ≤ 1 024 caracteres  ·  Mensaje ≤ 4 096
"""

import json, os, re, time, hashlib, tempfile, urllib.parse, requests
from datetime import date
from pathlib   import Path
from typing    import Dict, List
from bs4       import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, quote_plus

# ───────────── Configuración ───────────── #
SRC_NAME   = "medias24_leboursier"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True, parents=True)

# ---------- HTTP helpers ---------- #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.3)",
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

# ---------- Telegram helpers ---------- #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t:str)->str: return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _msg(title:str, desc:str, link:str) -> str:
    return "\n".join([
        f"*{_esc(title)}*",
        "",
        _esc(desc),
        "",
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews"
    ])

def _norm_img(url:str)->str:
    sch,net,path,query,frag=urlsplit(url)
    return urlunsplit((sch,net,quote(path,safe='/%'),quote_plus(query,safe='=&'),frag))

def _send(title:str, desc:str, link:str, img:str|None):
    caption = _msg(title,desc,link)[:1024]
    body    = _msg(title,desc,link)[:4096]
    if img:
        try:
            _session().head(img,timeout=5).raise_for_status()
            safe=_norm_img(img)
            _session().post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id":TG_CHAT,"photo":safe,
                      "caption":caption,"parse_mode":"MarkdownV2"},
                timeout=10).raise_for_status()
            return
        except Exception: 
            pass
    _session().post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":body,"parse_mode":"MarkdownV2"},
        timeout=10).raise_for_status()

# ---------- Cache ---------- #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set): 
    CACHE_FILE.write_text(json.dumps(list(c),ensure_ascii=False,indent=2))

# ────────────── Medias24 parser ────────────── #
_PAT_HEADER = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK   = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")

def _parse_markdown(md:str)->List[Dict]:
    """Extrae lista de artículos (título, link, fecha, descripción) del markdown jina.ai"""
    lines = md.splitlines()
    out:List[Dict] = []
    i=0
    while i < len(lines):
        m = _PAT_HEADER.match(lines[i])
        if m and i+1 < len(lines):
            d,mn,y = m.groups()
            parsed = f"{y}-{int(mn):02d}-{int(d):02d}"
            m2 = _PAT_LINK.match(lines[i+1])
            if m2:
                title, link = m2.groups()
                desc = ""
                j = i + 2
                while j < len(lines):
                    txt = lines[j].strip()
                    # --- filtros de líneas que NO son descripción ---------------- #
                    if (
                        not txt                             # vacía
                        or txt.startswith("![")            # imagen
                        or _PAT_LINK.match(txt)            # otro link
                        or re.fullmatch(r"[=\- ]{3,}", txt)  # ### FIX: subrayados =====
                    ):
                        j += 1
                        continue
                    desc = re.sub(r"\s+", " ", txt).strip(" …")
                    break
                out.append({"title":title,
                            "desc":desc,
                            "link":link,
                            "img":"",
                            "pdate":parsed})
            i += 2
        else:
            i += 1
    return out

def _fetch_markdown() -> str:
    url_html = "http://medias24.com/categorie/leboursier/actus/"
    cache    = TMP_DIR / (hashlib.md5(url_html.encode()).hexdigest()+".md")
    today_key = date.today().isoformat()
    if cache.exists() and cache.stem.endswith(today_key):
        return cache.read_text(encoding="utf-8")

    print("[DEBUG] downloading via jina.ai")
    url_jina = f"https://r.jina.ai/http://{url_html.lstrip('http://').lstrip('https://')}"
    md = _safe_get(url_jina, timeout=15).text
    # re‑nombre el archivo con la fecha para invalidar caché diaria
    cache.unlink(missing_ok=True)
    cache = cache.with_stem(cache.stem.split('.')[0] + today_key)
    cache.write_text(md, encoding="utf-8")
    return md

# ───────────── Main ───────────── #
def main() -> None:
    today = date.today().isoformat()
    # -------- durante la prueba queremos incluir AYER también -------- #
    yesterday = (date.today()).isoformat()  # ← cambia .isoformat() a str(today - 1) si lo necesitas
    cache = _load_cache()

    print(f"— {SRC_NAME} —")
    try:
        md   = _fetch_markdown()
        arts = _parse_markdown(md)
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
        if a["pdate"] not in {today, yesterday}:   # ↞ permite ayer y hoy
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
