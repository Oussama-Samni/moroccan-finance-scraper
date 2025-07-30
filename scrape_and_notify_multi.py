#!/usr/bin/env python3
"""
Finances News, L’Economiste, EcoActu, Médias24 → Telegram
Baseline robusto v1.3.2
• Formato con líneas en blanco garantizadas
• 403 de Médias24 ignorado de forma segura
• Omite artículos ‘Premium’
"""

import json, os, re, time, urllib.parse, requests, yaml
from datetime import date
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

# ───────────── Session ───────────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.3.2; "
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

def _get_with_fallback(url:str, timeout:float)->requests.Response:
    sess=_session(); r=sess.get(url,timeout=timeout)
    if r.status_code==403 and "medias24.com" in url:
        alt=url.rstrip("/")+"/amp/"; print("[DEBUG] Médias24 403 – pruebo AMP:",alt)
        r=sess.get(alt,timeout=timeout)
        if r.status_code==403:
            alt2=alt+"?outputType=amp&refresh=true"
            print("[DEBUG] Médias24 403 – pruebo AMP+OT:",alt2)
            r=sess.get(alt2,timeout=timeout)
    return r

def fetch(url:str, timeout:float=10.0)->str|None:
    r=_get_with_fallback(url,timeout)
    if r.status_code==403:
        return None
    r.raise_for_status()
    return r.text

# ───────── cache ───────── #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set)->None:
    CACHE_FILE.write_text(json.dumps(list(c),ensure_ascii=False,indent=2))

# ───────── Telegram helpers ───────── #
_MD_SPECIAL=r"_*[]()~`>#+-=|{}.!\\"
def _escape_md(t:str)->str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _build_msg(head:str, desc:str, link:str)->str:
    return (
        f"*{_escape_md(head)}*\n\n"
        f"{_escape_md(desc)}\n\n"
        f"[Lire l’article complet]({_escape_md(link)})\n\n"
        "@MorrocanFinancialNews"
    )

def _truncate(t:str, limit:int)->str:
    return t if len(t)<=limit else t[:limit-1]+"…"

def _norm_img_url(u:str)->str:
    sch,net,path,q,frag=urlsplit(u)
    return urlunsplit((sch,net,quote(path,safe='/%'),quote_plus(q,safe='=&'),frag))

def _send_telegram(head:str,desc:str,link:str,img:str|None):
    caption=_truncate(_build_msg(head,desc,link),1024)
    fullmsg=_truncate(_build_msg(head,desc,link),4096)
    if img:
        try:
            h=requests.head(img,timeout=5)
            if h.ok and h.headers.get("Content-Type","").startswith("image/"):
                safe=_norm_img_url(img)
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={"chat_id":TG_CHAT,"photo":safe,
                          "caption":caption,"parse_mode":"MarkdownV2"},
                    timeout=10).raise_for_status()
                return
        except Exception as e:
            print("[WARN] sendPhoto falló → texto:",e)
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":fullmsg,
              "parse_mode":"MarkdownV2","disable_web_page_preview":False},
        timeout=10).raise_for_status()

# ───────── Parsing genérico ───────── #
def _extract_first(block:BeautifulSoup,specs:str,base:str)->str:
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if "::attr(" in spec:
            css,attr=re.match(r"(.+)::attr\((.+)\)",spec).groups()
            tag=block.select_one(css)
            if tag and tag.has_attr(attr):
                raw=tag[attr]
                if attr=="style" and "background-image" in raw:
                    m=re.search(r'url\((["\']?)(.*?)\1\)',raw); raw=m.group(2) if m else raw
                return urljoin(base,raw)
        else:
            tag=block.select_one(spec)
            if tag and tag.has_attr("src"):
                return urljoin(base,tag["src"])
    return ""

def _is_premium_medias24(bloc:BeautifulSoup)->bool:
    return bool(bloc.select_one("span.premium-post"))

def _parse(src:Dict)->List[Dict]:
    html=fetch(src["list_url"])
    if html is None:
        print(f"[WARN] {src['name']} – omitido por 403"); return []
    soup=BeautifulSoup(html,"html.parser"); sel=src["selectors"]
    seen:set[str]=set(); out=[]
    for bloc in soup.select(sel["container"]):
        if src["name"]=="medias24_leboursier" and _is_premium_medias24(bloc):
            continue
        a = bloc.select_one(sel["headline"])
        if not a:
            continue
        title=a.get_text(strip=True)
        link=urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or (src["name"]=="financesnews" and link in seen):
            continue
        seen.add(link)

        desc=""
        if sel.get("description"):
            d=bloc.select_one(sel["description"])
            if d: desc=d.get_text(strip=True)

        img=_extract_first(bloc, sel.get("image",""), src["base_url"]) if sel.get("image") else ""

        raw_date=""
        if sel.get("date"):
            dt=bloc.select_one(sel["date"])
            if dt: raw_date=dt.get_text(strip=True)

        parsed=""
        if (rx:=src.get("date_regex")) and raw_date and (m:=re.search(rx,raw_date)):
            if src.get("month_map"):
                d,mon,y=m.groups(); mm=src["month_map"].get(mon)
                if mm: parsed=f"{y}-{mm}-{int(d):02d}"
            else:
                d,mn,y=m.groups(); parsed=f"{y}-{int(mn):02d}-{int(d):02d}"

        out.append({"title":title,"desc":desc,"link":link,
                    "img":img,"pdate":parsed or raw_date})
    return out

# ───────── Main ───────── #
def main():
    today=date.today().isoformat(); cache=_load_cache()
    sources=yaml.safe_load(open(SRC_FILE,encoding="utf-8"))
    ACTIVE={"financesnews","leconomiste_economie","ecoactu_nationale","medias24_leboursier"}
    for src in sources:
        if src["name"] not in ACTIVE: continue
        print(f"— {src['name']} —"); arts=_parse(src)
        print("DEBUG – lista completa parseada:")
        for a in arts: print(" •",a["title"][:70],"| pdate:",a["pdate"])
        print("------------------------------------------------\n")
        for a in arts:
            if a["link"] in cache: continue
            if a["pdate"] and a["pdate"]!=today: continue
            try:
                print(" Enviando:", a["title"][:60])
                _send_telegram(a["title"],a["desc"],a["link"],a["img"])
                cache.add(a["link"]); time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:",e)
    _save_cache(cache)

if __name__=="__main__":
    main()
