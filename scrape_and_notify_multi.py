#!/usr/bin/env python3
"""
Multi‑fuente → Telegram (@MorrocanFinancialNews)
Versión “medias24 v3” – ahora parsea la salida Markdown de jina.ai
-----------------------------------------------------------------
· Normaliza URLs de imagen
· Escapa Markdown V2
· Caption ≤1 024 · Body ≤4 096
"""

import json, os, re, time, hashlib, tempfile, urllib.parse, requests, yaml
from datetime import date
from pathlib import Path
from typing  import Dict, List
from bs4     import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, quote_plus

# ───────────── Configuración ───────────── #
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

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
    """GET con reporte de error pero sin romper el flujo."""
    try:
        r = _session().get(url, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        raise RuntimeError(f"{e}")

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
    return urlunsplit((sch,net,quote(path,safe='/%),'),quote_plus(query,safe='=&'),frag))

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
        except Exception: pass
    _session().post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":body,"parse_mode":"MarkdownV2"},
        timeout=10).raise_for_status()

# ---------- Cache ---------- #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set): CACHE_FILE.write_text(json.dumps(list(c),ensure_ascii=False,indent=2))

# ────────────── Medias24 parser ────────────── #
_PAT_HEADER = re.compile(r"^Le\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+à\s+\d")
_PAT_LINK   = re.compile(r"^\[(.+?)\]\((https?://[^\s)]+)\)")
def _parse_medias24(markdown:str)->List[Dict]:
    lines = markdown.splitlines()
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
                desc   = ""
                # busca primer párrafo descriptivo (salta imágenes / links)
                j=i+2
                while j<len(lines) and not lines[j].strip():
                    j+=1
                if j<len(lines):
                    desc = re.sub(r"\s+", " ", lines[j]).strip(" …")
                out.append({"title":title,
                            "desc":desc,
                            "link":link,
                            "img":"",
                            "pdate":date_parsed})
            i += 2
        else:
            i += 1
    return out

def fetch_medias24() -> str:
    url_html = "http://medias24.com/categorie/leboursier/actus/"
    cache    = TMP_DIR / (hashlib.md5(url_html.encode()).hexdigest()+".md")
    if cache.exists() and (date.today().day == cache.stat().st_mtime_ns//1_000_000_000//86400):
        return cache.read_text(encoding="utf-8")

    print("[DEBUG] downloading via jina.ai")
    url_jina = f"https://r.jina.ai/http://{url_html.lstrip('http://').lstrip('https://')}"
    md = _safe_get(url_jina, timeout=15).text
    cache.write_text(md, encoding="utf-8")
    return md

# ---------- Generic HTML parser (otras fuentes) ---------- #
def _extract_first(block:BeautifulSoup,specs:str,base:str)->str:
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if "::attr(" in spec:
            css,attr = re.match(r"(.+)::attr\((.+)\)",spec).groups()
            tag=block.select_one(css)
            if tag and tag.has_attr(attr):
                raw=tag[attr]
                if attr=="style" and "background-image" in raw:
                    m=re.search(r'url\((["\']?)(.*?)\1\)', raw); raw=m.group(2) if m else raw
                return urljoin(base,raw)
        else:
            tag=block.select_one(spec)
            if tag and tag.has_attr("src"):
                return urljoin(base,tag["src"])
    return ""

def _parse_generic(src:Dict)->List[Dict]:
    soup=BeautifulSoup(_safe_get(src["list_url"],timeout=10).text,"html.parser")
    sel=src["selectors"]; seen=set(); out=[]
    for b in soup.select(sel["container"]):
        a=b.select_one(sel["headline"]); 
        if not a: continue
        title=a.get_text(strip=True)
        link=urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or (src["name"]=="financesnews" and link in seen): continue
        seen.add(link)
        desc=""
        if sel.get("description"):
            d=b.select_one(sel["description"]); desc=d.get_text(strip=True) if d else ""
        img=_extract_first(b, sel.get("image",""), src["base_url"]) if sel.get("image") else ""
        raw_date=""
        if sel.get("date"):
            d=b.select_one(sel["date"]); raw_date=d.get_text(strip=True) if d else ""
        parsed=""
        if (rx:=src.get("date_regex")) and raw_date and (m:=re.search(rx,raw_date)):
            if src.get("month_map"):
                d,mon,y=m.groups(); parsed=f"{y}-{src['month_map'][mon]}-{int(d):02d}"
            else:
                d,mn,y=m.groups(); parsed=f"{y}-{int(mn):02d}-{int(d):02d}"
        out.append({"title":title,"desc":desc,"link":link,"img":img,"pdate":parsed or raw_date})
    return out

# ───────────── Main ───────────── #
def main() -> None:
    today=date.today().isoformat()
    cache=_load_cache()
    sources=yaml.safe_load(open(SRC_FILE,encoding="utf-8"))

    for src in sources:
        if src["name"]!="medias24_leboursier":
            continue
        print("— medias24_leboursier —")
        try:
            md = fetch_medias24()
            arts = _parse_medias24(md)
        except Exception as e:
            print("[ERROR] Medias24:", e); arts=[]
        print("DEBUG – lista completa parseada:")
        for a in arts: print(" •",a["title"][:70],"| pdate:",a["pdate"])
        print("------------------------------------------------\n")
        for a in arts:
            if a["link"] in cache: continue
            if a["pdate"]!=today:   continue
            try:
                print(" Enviando:", a["title"][:60])
                _send(a["title"],a["desc"],a["link"],a["img"])
                cache.add(a["link"]); time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:",e)
    _save_cache(cache)

if __name__=="__main__":
    main()
