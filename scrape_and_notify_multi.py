#!/usr/bin/env python3
"""
Medias24 LeBoursier → Telegram (@MorrocanFinancialNews)
Versión “medias24 v4-lite-fix6”
────────────────────────────────────────────────────────
• Envía artículos de los últimos 3 días (hoy,-1,-2)
• Sin dependencias externas
• Salto de línea tras el primer “:” del título
• Filtrado robusto de tablas y etiquetas (normaliza acentos/mayúsculas)
• Sin vista previa de enlaces
"""

import hashlib, json, os, re, tempfile, time, unicodedata, requests
from datetime   import datetime, timedelta
from pathlib     import Path
from typing      import List
from urllib.parse import urlsplit, urlunsplit, quote, quote_plus
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ──────────── Config ──────────── #
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY = datetime.utcnow().date()
ALLOWED_DATES = {(TODAY - timedelta(days=i)).isoformat() for i in range(3)}

# ─────────── HTTP ──────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.4-fix6)",
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

# ───────── Telegram ────────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t:str) -> str: return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _newline_title(t:str) -> str:
    """Inserta un salto de línea tras el primer ':' (si existe)."""
    return re.sub(r"\s*:\s*", ":\n", t, count=1)

def _mk_msg(title:str, desc:str, link:str) -> str:
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
            safe = _norm_img(img)
            _session().post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id":TG_CHAT,"photo":safe,
                      "caption":caption,"parse_mode":"MarkdownV2"},
                timeout=10).raise_for_status()
            return
        except Exception:
            pass  # fallback texto

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":body,
              "parse_mode":"MarkdownV2","disable_web_page_preview":True},
        timeout=10).raise_for_status()

# ───────── Cache ───────── #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set): CACHE_FILE.write_text(json.dumps(list(c),ensure_ascii=False,indent=2))

# ──────── Medias24 parser ─────── #

def _canonical(t: str) -> str:
    """minúsculas + sin acentos + colapso espacios para comparación flexible"""
    t = unicodedata.normalize("NFKD", t).encode("ascii","ignore").decode()
    t = re.sub(r"\s+", " ", t.lower()).strip()
    return t

STOP = {
    "journee du", "la seance du jour", "marche de change",
    "la bourse", "masi pts", "variations valeur par valeur",
}

SKIP_RE = re.compile(r"^(journee du \d{1,2}-\d{1,2}-\d{4}|\d{1,2}-\d{1,2}-\d{4})$")

_PAT_HEADER = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK   = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")

def _strip_md_links(text:str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

def _parse_medias24(md:str) -> List[dict]:
    lines = md.splitlines()
    out   : List[dict] = []
    i = 0
    while i < len(lines):
        m = _PAT_HEADER.match(lines[i])
        if m and i+1 < len(lines):
            d,mn,y = m.groups()
            pdate  = f"{y}-{int(mn):02d}-{int(d):02d}"

            m2 = _PAT_LINK.match(lines[i+1])
            if m2:
                title, link = m2.groups()

                desc = ""
                j = i+2
                while j < len(lines):
                    raw = lines[j]
                    txt = raw.strip()
                    canon = _canonical(txt)
                    if (not txt or
                        txt.startswith("|") and txt.count("|")>=2 or
                        re.fullmatch(r"=+", txt) or
                        canon in STOP or
                        SKIP_RE.match(canon) or
                        _PAT_LINK.match(txt)):
                        j += 1; continue
                    desc = _strip_md_links(re.sub(r"\s+"," ",txt)).strip(" …")
                    break
                if not desc:
                    desc = " "   # NBSP para conservar salto

                out.append({"title":title,"desc":desc,"link":link,
                            "img":"","pdate":pdate})
            i += 2
        else:
            i += 1
    return out

def fetch_medias24() -> str:
    url_html = "http://medias24.com/categorie/leboursier/actus/"
    cache    = TMP_DIR / (hashlib.md5(url_html.encode()).hexdigest()+".md")
    if cache.exists() and cache.stat().st_mtime > time.time() - 3600:
        return cache.read_text(encoding="utf-8")

    print("[DEBUG] downloading via jina.ai")
    url_jina = "https://r.jina.ai/http://" + url_html.removeprefix("http://").removeprefix("https://")
    md = _safe_get(url_jina, timeout=15).text
    cache.write_text(md, encoding="utf-8")
    return md

# ───────── Main ───────── #
def main():
    cache = _load_cache()
    print("— medias24_leboursier —")

    try:
        arts = _parse_medias24(fetch_medias24())
    except Exception as e:
        print("[ERROR] Medias24:", e); arts=[]

    print("DEBUG – lista completa parseada:")
    for a in arts: print(" •", a["title"][:70], "| pdate:", a["pdate"])
    print("------------------------------------------------\n")

    for a in arts:
        if a["link"] in cache: continue
        if a["pdate"] not in ALLOWED_DATES: continue
        try:
            print(" Enviando:", a["title"][:60])
            _send(a["title"], a["desc"], a["link"], a["img"])
            cache.add(a["link"]); time.sleep(8)
        except Exception as e:
            print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__ == "__main__":
    main()
