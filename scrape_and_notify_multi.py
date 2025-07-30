#!/usr/bin/env python3
"""
Medias24 (LeBoursier) → Telegram (@MorrocanFinancialNews)
Versión v4 _solo para la prueba de “hoy + ayer”_
-------------------------------------------------
· Acepta artículos con fecha de hoy **o** de ayer
· Resto de la lógica idéntica a la v3
· Cuando acabes la prueba, pon DAYS_BACK = 0 y el
  filtro volverá a enviar solo artículos del día.
"""

import json, os, re, time, hashlib, tempfile, requests, yaml
from datetime import date, timedelta
from pathlib import Path
from typing  import Dict, List
from bs4     import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, quote_plus

# ───────── Config ───────── #
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

# 0 = solo hoy · 1 = hoy+ayer (prueba) · 2 = hoy+2 días atrás, etc.
DAYS_BACK = 1          #  ← cámbialo a 0 cuando termines la prueba

# ──────── HTTP helpers ──────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.4)",
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

# ──────── Telegram helpers ──────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"           # caracteres de markdown v2
esc = lambda t: re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _norm_img(url:str)->str:
    sch,net,path,query,frag = urlsplit(url)
    return urlunsplit((sch,net,quote(path,safe='/%'),quote_plus(query,safe='=&'),frag))

def _mk_msg(title:str, desc:str, link:str) -> str:
    return "\n".join([
        f"*{esc(title)}*",
        "",
        esc(desc),
        "",
        f"[Lire l’article complet]({esc(link)})",
        "",
        "@MorrocanFinancialNews"
    ])

def _send(tit:str, desc:str, link:str, img:str|None):
    caption = _mk_msg(tit,desc,link)[:1024]
    body    = _mk_msg(tit,desc,link)[:4096]
    if img:
        try:
            _session().head(img,timeout=5).raise_for_status()
            _session().post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id":TG_CHAT,"photo":_norm_img(img),
                      "caption":caption,"parse_mode":"MarkdownV2"},
                timeout=10).raise_for_status()
            return
        except Exception:
            pass
    _session().post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":body,"parse_mode":"MarkdownV2"},
        timeout=10).raise_for_status()

# ─────── Medias24 specific (HTML‑>Markdown jina.ai) ─────── #
_PAT_HEAD = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")
def _parse_medias24(md:str)->List[Dict]:
    lines = md.splitlines(); out=[]
    i=0
    while i<len(lines):
        m=_PAT_HEAD.match(lines[i])
        if m and i+1<len(lines):
            d,mn,y = m.groups()
            pdate = f"{y}-{int(mn):02d}-{int(d):02d}"
            lk = _PAT_LINK.match(lines[i+1])
            if lk:
                title,link = lk.groups()
                # busca una línea de descripción (no vacía ni link)
                desc=""
                j=i+2
                while j<len(lines) and (not lines[j].strip() or _PAT_LINK.match(lines[j])):
                    j+=1
                if j<len(lines):
                    desc = re.sub(r"\s+"," ",lines[j]).strip(" …")
                out.append({"title":title,"desc":desc,"link":link,"img":"","pdate":pdate})
            i+=2
        else:
            i+=1
    return out

def _fetch_medias24_md() -> str:
    url_html  = "http://medias24.com/categorie/leboursier/actus/"
    cache     = TMP_DIR / (hashlib.md5(url_html.encode()).hexdigest()+".md")
    today_key = (date.today()).isoformat()
    if cache.exists() and cache.stat().st_mtime_ns//1_000_000_000 > (time.time()-3600):
        return cache.read_text("utf-8")
    print("[DEBUG] downloading via jina.ai")
    md = _safe_get(f"https://r.jina.ai/http://{url_html.lstrip('http://').lstrip('https://')}",timeout=15).text
    cache.write_text(md,"utf-8"); return md

# ───────────── Main ───────────── #
def main():
    today   = date.today()
    limit   = { (today - timedelta(days=i)).isoformat()
                for i in range(DAYS_BACK+1) }     # hoy ± n días
    cache   = _load_cache()
    sources = yaml.safe_load(open(SRC_FILE,encoding="utf-8"))

    for src in sources:
        if src["name"]!="medias24_leboursier":
            continue
        print("— medias24_leboursier —")
        arts = _parse_medias24(_fetch_medias24_md())
        print("DEBUG – lista completa parseada:")
        for a in arts: print(" •",a['title'][:70],"| pdate:",a['pdate'])
        print("------------------------------------------------\n")

        for a in arts:
            if a["link"] in cache:     continue
            if a["pdate"] not in limit: continue   # ← filtro hoy+ayer
            try:
                print(" Enviando:", a["title"][:60])
                _send(a["title"],a["desc"],a["link"],a["img"])
                cache.add(a["link"]); time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:",e)
    _save_cache(cache)

if __name__=="__main__":
    main()
