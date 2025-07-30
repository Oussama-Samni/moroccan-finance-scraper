#!/usr/bin/env python3
"""
Finances News · L’Economiste · EcoActu · Médias24 LeBoursier  → Telegram
------------------------------------------------------------------------
Baseline robusto v1.4‑b2
• Fallback jina.ai → HTML de /actus/ si el RSS de Médias24 devuelve 4xx
• Normaliza URLs de imagen, escapa Markdown V2, controla longitudes
"""
import json, os, re, time, urllib.parse, requests, yaml
from datetime      import date
from pathlib       import Path
from typing        import Dict, List
from bs4           import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse  import urljoin, urlsplit, urlunsplit, quote, quote_plus

# ──────────────── Config ────────────────
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")

# ───────────── HTTP helpers ─────────────
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; MoroccanFinanceBot/1.4-b2; "
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

def fetch(url: str, timeout: float = 10.0) -> str:
    """
    GET con retry + fallback jina.ai para Médias24 feed:
       rss → 403  ⟶  https://r.jina.ai/http://medias24.com/categorie/leboursier/actus/
    """
    sess = _session()
    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.HTTPError as e:
        # fallback sólo para el feed de Médias24
        if (e.response is not None and e.response.status_code in (401,403) and
            "medias24.com" in url and "/feed" in url):
            proxy = "https://r.jina.ai/http://medias24.com/categorie/leboursier/actus/"
            try:
                print("[DEBUG] RSS directo falló – pruebo jina.ai:", proxy)
                r2 = sess.get(proxy, timeout=timeout)
                r2.raise_for_status()
                print("[DEBUG] jina.ai fallback OK")
                return r2.text
            except Exception as e2:
                print("[WARN] jina.ai también falló:", e2)
                raise
        raise                               # re‑lanza lo que no sea manejado

# ─────────── Cache URL enviadas ──────────
def _load_cache()->set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()
def _save_cache(c:set)->None:
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ───────────── Telegram helpers ──────────
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def _escape_md(t:str)->str:
    return re.sub(f"([{re.escape(_MD_SPECIAL)}])", r"\\\1", t)

def _build_msg(head:str, desc:str, link:str)->str:
    parts=[f"*{_escape_md(head)}*", "",
           _escape_md(desc), "",
           f"[Lire l’article complet]({_escape_md(link)})", "",
           "@MorrocanFinancialNews"]
    return "\n".join(filter(None, parts))

def _truncate(text:str, limit:int)->str:
    return text if len(text)<=limit else text[:limit-1]+"…"

def _norm_img_url(url:str)->str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net,
                       quote(path, safe="/%"),
                       quote_plus(query, safe="=&"),
                       frag))

def _send_telegram(head:str, desc:str, link:str, img:str|None):
    caption=_truncate(_build_msg(head, desc, link), 1_024)
    fullmsg=_truncate(_build_msg(head, desc, link), 4_096)
    if img:
        try:
            if requests.head(img, timeout=5).headers.get("Content-Type","").startswith("image/"):
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                    json={"chat_id":TG_CHAT,"photo":_norm_img_url(img),
                          "caption":caption,"parse_mode":"MarkdownV2"},
                    timeout=10).raise_for_status()
                return
        except Exception as e:
            print("[WARN] sendPhoto falló → fallback texto:", e)
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT,"text":fullmsg,
              "parse_mode":"MarkdownV2","disable_web_page_preview":False},
        timeout=10).raise_for_status()

# ─────────── Parsing helpers ────────────
def _extract_first(block:BeautifulSoup, specs:str, base:str)->str:
    for s in [x.strip() for x in specs.split(",") if x.strip()]:
        if "::attr(" in s:
            css, attr = re.match(r"(.+)::attr\((.+)\)", s).groups()
            tag = block.select_one(css)
            if tag and tag.has_attr(attr):
                raw = tag[attr]
                if attr=="style" and "background-image" in raw:
                    m = re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    raw = m.group(2) if m else raw
                return urljoin(base, raw)
        else:
            tag = block.select_one(s)
            if tag and tag.has_attr("src"):
                return urljoin(base, tag["src"])
    return ""

def _parse(src:Dict)->List[Dict]:
    soup=BeautifulSoup(fetch(src["list_url"]), "html.parser")
    sel=src["selectors"]; seen=set(); out=[]
    for bloc in soup.select(sel["container"]):
        a=bloc.select_one(sel["headline"]); 
        if not a: continue
        title=a.get_text(strip=True)
        link=urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or (src["name"]=="financesnews" and link in seen): continue
        seen.add(link)
        desc=""
        if sel.get("description") and (d:=bloc.select_one(sel["description"])):
            desc=d.get_text(strip=True)
        img=_extract_first(bloc, sel.get("image",""), src["base_url"]) if sel.get("image") else ""
        raw_date=(bloc.select_one(sel["date"]).get_text(strip=True)
                  if sel.get("date") and bloc.select_one(sel["date"]) else "")
        parsed=""
        if (rx:=src.get("date_regex")) and raw_date and (m:=re.search(rx,raw_date)):
            if src.get("month_map"):
                d,mon,y=m.groups(); mm=src["month_map"].get(mon)
                if mm: parsed=f"{y}-{mm}-{int(d):02d}"
            else:
                d,mn,y=m.groups(); parsed=f"{y}-{int(mn):02d}-{int(d):02d}"
        out.append({"title":title,"desc":desc,"link":link,"img":img,"pdate":parsed or raw_date})
    return out

# ─────────────────── Main ───────────────────
def main():
    today=date.today().isoformat()
    cache=_load_cache()
    active={"medias24_leboursier"}                     # rama test sólo esta fuente

    for src in yaml.safe_load(open(SRC_FILE,encoding="utf-8")):
        if src["name"] not in active: continue
        print(f"— {src['name']} —")
        try:
            arts=_parse(src)
        except Exception as e:
            print(f"[WARN] {src['name']} – omitido por error:", e)
            continue

        print("DEBUG – lista completa parseada:")
        for a in arts: print(" •",a["title"][:70],"| pdate:",a["pdate"])
        print("------------------------------------------------")

        for a in arts:
            if a["link"] in cache: continue
            if a["pdate"] and a["pdate"]!=today: continue
            try:
                print(" Enviando:", a["title"][:60])
                _send_telegram(a["title"], a["desc"], a["link"], a["img"])
                cache.add(a["link"]); time.sleep(8)
            except Exception as e:
                print("[ERROR] Telegram:", e)

    _save_cache(cache)

if __name__=="__main__":
    main()
