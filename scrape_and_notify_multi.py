#!/usr/bin/env python3
"""
Medias24 LeBoursier → Telegram (@MorrocanFinancialNews)
Versión “medias24 v4-lite-fix6”
────────────────────────────────────────────────────────
• Últimos 3 días (hoy,-1,-2)
• Sin dependencias externas
• Omite cabeceras tipo “Marché de change”
• Fallback: og:description + descarga y re-envío de og:image
"""

import hashlib, json, os, re, tempfile, time, requests
from datetime   import datetime, timedelta, timezone
from pathlib    import Path
from typing     import Dict, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import (
    urlsplit, urlunsplit, quote, quote_plus, urljoin,
)

# ───────────── Config ───────────── #
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY = datetime.now(timezone.utc).date()
ALLOWED_DATES = { (TODAY - timedelta(days=i)).isoformat() for i in range(3) }

# ─────────── HTTP helpers ────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.4-lite-fix6)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url:str, **kw) -> requests.Response:
    r = _session().get(url, **kw)
    r.raise_for_status()
    return r

# ────────── Telegram helpers ───────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _esc(t:str)->str: return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

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

def _fix_scheme(url:str)->str:
    return "https:" + url if url.startswith("//") else url

def _norm_img(url:str)->str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net, quote(path, safe='/%'),
                       quote_plus(query, safe='=&'), frag))

def _send(title:str, desc:str, link:str, img_url:str|None):
    caption = _mk_msg(title, desc, link)[:1024]
    body    = _mk_msg(title, desc, link)[:4096]

    img_url = _fix_scheme(img_url) if img_url else _find_meta(link, "og:image")
    if img_url:
        try:
            # descarga local
            data = _safe_get(img_url, timeout=10).content
            files = {"photo": ("img.jpg", data)}
            _session().post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT, "caption": caption,
                      "parse_mode": "MarkdownV2"},
                files=files, timeout=15).raise_for_status()
            return
        except Exception as e:
            print("[WARN] foto falla → texto", e)

    _session().post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": body,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True
        },
        timeout=10
    ).raise_for_status()

# ───────────── Cache ───────────── #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set): CACHE_FILE.write_text(json.dumps(list(c), indent=2, ensure_ascii=False))

# ──────── Medias24 specific ─────── #
_PAT_HEADER = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK   = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")
_PAT_DATE   = re.compile(r"^Le\s+\d+/\d+/\d+\s+à\s+\d")
_PAT_SECTION= re.compile(r"^[A-ZÉÈÀÂÂÎÔÛÇ][a-zéèàâêîôûç]+(?:\s+[a-zéèàâêîôûç]+)?$")

def _strip_md_links(text:str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

def _find_meta(url:str, prop:str) -> str:
    try:
        html = _safe_get(url, timeout=8).text
        m = re.search(rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        return _fix_scheme(m.group(1)) if m else ""
    except Exception:
        return ""

def _parse_medias24(md: str) -> List[Dict]:
    lines = md.splitlines()
    out: List[Dict] = []
    i = 0
    while i < len(lines):
        if (m := _PAT_HEADER.match(lines[i])) and i + 1 < len(lines):
            d, mn, y = m.groups()
            pdate = f"{y}-{int(mn):02d}-{int(d):02d}"

            if (m2 := _PAT_LINK.match(lines[i + 1])):
                title, link = m2.groups()
                desc = ""
                j = i + 2
                while j < len(lines):
                    txt = lines[j].strip()
                    if (not txt or re.fullmatch(r"=+", txt)
                        or _PAT_DATE.match(txt)
                        or _PAT_LINK.match(txt)
                        or _PAT_SECTION.match(txt)):        # ← descarta “La bourse”…
                        j += 1
                        continue
                    desc = _strip_md_links(re.sub(r"\s+", " ", txt)).strip(" …")
                    break

                if not desc:                               # fallback a og:description
                    desc = _find_meta(link, "og:description")

                out.append({
                    "title": title,
                    "desc":  desc,
                    "link":  link,
                    "img":   "",         # la buscamos luego
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

# ───────────── Main ───────────── #
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
        if a["link"] in cache:              continue
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
