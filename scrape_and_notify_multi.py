#!/usr/bin/env python3
"""
Finances News · L’Economiste · EcoActu · Médias24 → Telegram
Baseline robusto v1.3‑b (M24‑dual‑test)
"""

import json, os, re, time, urllib.parse, requests, yaml, sys
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

# ───────────────────── Session ───────────────────── #
def _session(extra_headers:dict|None=None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.3-b; "
            "+https://github.com/OussamaSamni/moroccan-finance-scraper)"
        ),
        "Accept-Language": "fr,en;q=0.8",
    })
    if extra_headers:
        s.headers.update(extra_headers)
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset(["GET","HEAD"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _get_with_fallback(url:str, timeout:float):
    """
    • Para el feed RSS de Médias24: cabeceras amigables.
    • Si falla → intenta HTML vía proxy jina.ai.
    """
    # ----- caso especial Médias24 RSS -----
    if "medias24.com" in url and "/feed" in url:
        try:
            sess=_session({
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                "Referer": "https://medias24.com/"
            })
            r=sess.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            print("[DEBUG] RSS directo falló:", e, file=sys.stderr)
            # fallback a HTML vía proxy jina.ai
            alt = "https://r.jina.ai/http://medias24.com/categorie/leboursier/actus/"
            try:
                sess=_session()
                r=sess.get(alt, timeout=timeout)
                r.raise_for_status()
                return r
            except Exception as e2:
                print("[WARN] fallback jina.ai también falló:", e2, file=sys.stderr)
                raise  # propaga para que _parse lo registre y omita
    # ----- genérico -----
    r=_session().get(url, timeout=timeout)
    r.raise_for_status()
    return r

def fetch(url:str, timeout:float=10.0) -> str:
    return _get_with_fallback(url, timeout).text

# ────────────── Cache URLs enviadas ───────────── #
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set)->None:
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ────────────────── Telegram ─────────────────── #
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _escape_md(t:str)->str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _build_msg(head:str, desc:str, link:str)->str:
    return "\n".join([
        f"*{_escape_md(head)}*",
        "",                                        # línea en blanco
        _escape_md(desc),
        "",                                        # otra
        f"[Lire l’article complet]({_escape_md(link)})",
        "",
        "@MorrocanFinancialNews"
    ])

def _truncate(text:str, limit:int)->str:
    return text if len(text)<=limit else text[:limit-1]+"…"

def _norm_img_url(url:str)->str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net, quote(path, safe="/%"), quote_plus(query, safe="=&"), frag))

def _send_telegram(head:str, desc:str, link:str, img:str|None):
    caption=_truncate(_build_msg(head,desc,link), 1_024)
    fullmsg=_truncate(_build_msg(head,desc,link), 4_096)
    if img:
        try:
            r=requests.head(img, timeout=5)
            if r.ok and r.headers.get("Content-Type","").startswith("image/"):
                safe=_norm_img_url(img)
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                              json={"chat_id":TG_CHAT,"photo":safe,
                                    "caption":caption,"parse_mode":"MarkdownV2"},
                              timeout=10).raise_for_status()
                return
        except Exception as e:
            print("[WARN] sendPhoto falló; texto:", e, file=sys.stderr)
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id":TG_CHAT,"text":fullmsg,
                        "parse_mode":"MarkdownV2","disable_web_page_preview":False},
                  timeout=10).raise_for_status()

# ─────────────── Parsing genérico ────────────── #
def _extract_first(block:BeautifulSoup, specs:str, base_url:str)->str:
    for spec in [s.strip() for s in specs.split(",") if s.strip()]:
        if "::attr(" in spec:
            css, attr = re.match(r"(.+)::attr\((.+)\)", spec).groups()
            tag=block.select_one(css)
            if tag and tag.has_attr(attr):
                raw=tag[attr]
                if attr=="style" and "background-image" in raw:
                    m=re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    raw=m.group(2) if m else raw
                return urljoin(base_url, raw)
        else:
            tag=block.select_one(spec)
            if tag and tag.has_attr("src"):
                return urljoin(base_url, tag["src"])
    return ""

def _parse(src:Dict)->List[Dict]:
    try:
        html=fetch(src["list_url"])
    except Exception as e:
        print(f"[WARN] {src['name']} – omitido por error: {e}")
        return []

    # ¿es RSS? => usar parser 'xml'
    is_rss = html.lstrip().startswith("<?xml")
    soup = BeautifulSoup(html, "xml" if is_rss else "html.parser")
    sel=src["selectors"]; seen=set(); out=[]
    for bloc in soup.select(sel["container"]):
        a=bloc.select_one(sel["headline"])
        if not a: continue
        title=a.get_text(strip=True)
        link=urljoin(src["base_url"], bloc.select_one(sel.get("link_attr","link")).get_text(strip=True)
                     if is_rss else a.get(sel.get("link_attr","href"),""))
        if not link or (src["name"]=="financesnews" and link in seen): continue
        seen.add(link)

        desc=""
        if sel.get("description"):
            d=bloc.select_one(sel["description"])
            if d: desc=d.get_text(strip=True)

        img=""
        if sel.get("image"):
            img=_extract_first(bloc, sel["image"], src["base_url"])

        raw_date=""
        if sel.get("date"):
            dt=bloc.select_one(sel["date"])
            if dt: raw_date=dt.get_text(strip=True)

        parsed=""
        if (rx:=src.get("date_regex")) and raw_date and (m:=re.search(rx,raw_date)):
            if src.get("month_map"):
                d,mon,y=m.groups(); mm=src["month_map"].get(mon); 
                if mm: parsed=f"{y}-{mm}-{int(d):02d}"
            else:
                d,mn,y=m.groups(); parsed=f"{y}-{int(mn):02d}-{int(d):02d}"

        out.append({"title":title,"desc":desc,"link":link,
                    "img":img,"pdate":parsed or raw_date})
    return out

# ─────────────────── Main ──────────────────── #
def main():
    today=date.today().isoformat()
    cache=_load_cache()
    active={"medias24_leboursier"}  # sólo test
    sources=yaml.safe_load(open(SRC_FILE,encoding="utf-8"))
    for src in sources:
        if src["name"] not in active: continue
        print(f"— {src['name']} —")
        arts=_parse(src)
        print("DEBUG – lista completa parseada:")
        for a in arts: print(" •",a["title"][:70],"| pdate:",a["pdate"])
        print("------------------------------------------------\n")
        for a in arts:
            if a["link"] in cache: continue
            if a["pdate"] and a["pdate"]!=today: continue
            try:
                print(" Enviando:", a["title"][:60])
                _send_telegram(a["title"], a["desc"], a["link"], a["img"])
                cache.add(a["link"]); time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e, file=sys.stderr)
    _save_cache(cache)

if __name__=="__main__":
    main()
