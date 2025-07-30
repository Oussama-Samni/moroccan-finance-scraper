#!/usr/bin/env python3
"""
Finances News, L’Economiste, EcoActu, Médias24 LeBoursier → Telegram
Versión v1.6‑tmp  (envía hoy + ayer para pruebas)
"""

import json, os, re, time, urllib.parse, requests, yaml
from datetime import date, timedelta          # ⬅️  NUEVO
from pathlib   import Path
from typing    import Dict, List
from bs4       import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, quote_plus

SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")

# ──────────────── Sesión HTTP ──────────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.6-tmp; "
            "+https://github.com/OussamaSamni/moroccan-finance-scraper)"
        ),
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def fetch(url:str, timeout:float=10.0) -> str:
    r=_session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

# ───────────── Cache de enlaces enviados ───────────── #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(c:set)->None:
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ────────────── Utilidades Telegram ────────────── #
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _escape_md(t:str)->str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _build_msg(head:str, desc:str, link:str)->str:
    return "\n".join([
        f"*{_escape_md(head)}*",
        "",
        _escape_md(desc),
        "",
        f"[Lire l’article complet]({_escape_md(link)})",
        "",
        "@MorrocanFinancialNews"
    ])

def _truncate(txt:str, lim:int)->str:
    return txt if len(txt)<=lim else txt[:lim-1]+"…"

def _norm_img(url:str)->str:
    sch, net, path, q, frag = urlsplit(url)
    return urlunsplit((sch, net, quote(path, safe="/%"), quote_plus(q, safe="=&"), frag))

def _send_tg(head:str, desc:str, link:str, img:str|None):
    caption=_truncate(_build_msg(head, desc, link), 1024)
    fullmsg=_truncate(_build_msg(head, desc, link), 4096)

    if img:
        try:
            h=requests.head(img, timeout=5)
            if h.ok and h.headers.get("Content-Type","").startswith("image/"):
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={"chat_id":TG_CHAT,"photo":_norm_img(img),
                          "caption":caption,"parse_mode":"MarkdownV2"},
                    timeout=10).raise_for_status()
                return
        except Exception as e:
            print("[WARN] sendPhoto falló, uso texto:", e)

    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":fullmsg,
              "parse_mode":"MarkdownV2","disable_web_page_preview":False},
        timeout=10).raise_for_status()

# ─────────────── Helpers de parsing ─────────────── #
def _extract_first(block:BeautifulSoup, specs:str, base:str)->str:
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if "::attr(" in spec:
            css, attr = re.match(r"(.+)::attr\((.+)\)", spec).groups()
            tag = block.select_one(css)
            if tag and tag.has_attr(attr):
                raw = tag[attr]
                if attr=="style" and "background-image" in raw:
                    m=re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    raw=m.group(2) if m else raw
                return urljoin(base, raw)
        else:
            tag=block.select_one(spec)
            if tag and tag.has_attr("src"):
                return urljoin(base, tag["src"])
    return ""

def _parse(src:Dict)->List[Dict]:
    html=""
    try:
        html=fetch(src["list_url"])
    except requests.HTTPError as e:
        if src["name"]=="medias24_leboursier" and e.response.status_code==403:
            # fallback a jina.ai
            print("[DEBUG] downloading via jina.ai")
            jina=f"https://r.jina.ai/http://medias24.com/categorie/leboursier/actus/"
            html=fetch(jina)
        else:
            raise

    soup=BeautifulSoup(html,"html.parser")
    sel=src["selectors"]
    seen=set(); out=[]
    for b in soup.select(sel["container"]):
        a=b.select_one(sel["headline"])
        if not a: continue
        title=a.get_text(strip=True)
        link=urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or (src["name"]=="financesnews" and link in seen): continue
        seen.add(link)

        desc=""
        if sel.get("description"):
            d=b.select_one(sel["description"])
            if d: desc=d.get_text(strip=True)

        img=_extract_first(b, sel.get("image",""), src["base_url"]) if sel.get("image") else ""

        raw_date=""
        if sel.get("date"):
            dt=b.select_one(sel["date"])
            if dt: raw_date=dt.get_text(strip=True)

        parsed=""
        rx=src.get("date_regex")
        if rx and raw_date and (m:=re.search(rx,raw_date)):
            if src.get("month_map"):
                d,mon,y=m.groups(); mm=src["month_map"].get(mon)
                if mm: parsed=f"{y}-{mm}-{int(d):02d}"
            else:
                d,mn,y=m.groups(); parsed=f"{y}-{int(mn):02d}-{int(d):02d}"

        out.append({"title":title,"desc":desc,"link":link,"img":img,
                    "pdate":parsed or raw_date})
    return out

# ─────────────────────── Main ─────────────────────── #
def main():
    today=date.today().isoformat()
    yesterday=(date.today()-timedelta(days=1)).isoformat()   # ⬅️  NUEVO
    cache=_load_cache()
    sources=yaml.safe_load(open(SRC_FILE,encoding="utf-8"))

    ACTIVE={"financesnews","leconomiste_economie",
            "ecoactu_nationale","medias24_leboursier"}

    for src in sources:
        if src["name"] not in ACTIVE: continue
        print(f"— {src['name']} —")
        try:
            arts=_parse(src)
        except Exception as e:
            print(f"[WARN] {src['name']} – omitido por error:", e)
            continue

        # DEBUG
        print("DEBUG – lista completa parseada:")
        for a in arts:
            print(" •",a["title"][:70],"| pdate:",a["pdate"])
        print("------------------------------------------------\n")

        for a in arts:
            if a["link"] in cache: continue
            if a["pdate"] and a["pdate"] not in {today, yesterday}:   # ⬅️  hoy + ayer
                continue
            try:
                print(" Enviando:", a["title"][:60])
                _send_tg(a["title"], a["desc"], a["link"], a["img"])
                cache.add(a["link"]); time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__=="__main__":
    main()
