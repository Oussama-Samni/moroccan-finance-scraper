"""
Scraper multi‑fuente.
Lee `sources.yml`, procesa medios financieros marroquíes
y publica las noticias del día en Telegram evitando duplicados.
"""

import os, re, time, urllib.parse
from datetime import date
from urllib.parse import urljoin, urlparse

import requests, yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Sesión HTTP con UA propio ──────────────────────────────────────────────── #

def _get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; OussamaSamniBot/1.0; "
            "+https://github.com/OussamaSamni/moroccan-finance-scraper)"
        ),
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(
        total=5, backoff_factor=1,
        status_forcelist=(429,500,502,503,504),
        allowed_methods=frozenset(["GET","HEAD"])
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter); s.mount("http://", adapter)
    return s


def fetch_url(url: str, timeout: float = 10.0) -> str:
    s = _get_session()
    verify = False if "casablanca-bourse.com" in url else True
    r = s.get(url, timeout=timeout, verify=verify)
    r.raise_for_status()
    return r.text

# ── Estado de URLs enviadas ───────────────────────────────────────────────── #

from scrape_and_notify import load_sent, save_sent

with open("sources.yml", encoding="utf-8") as f:
    SOURCES = yaml.safe_load(f)

# ── Telegram helpers ──────────────────────────────────────────────────────── #

def _escape_md(t:str)->str:
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!\\])', r'\\\1', t)

def _tg_send_md(msg:str):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={
        "chat_id":chat, "text":msg,
        "parse_mode":"MarkdownV2",
        "disable_web_page_preview":False
    }, timeout=10).raise_for_status()

def send_article(a:dict):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    head, desc = _escape_md(a["headline"]), _escape_md(a["description"])
    link  = _escape_md(a["link"])
    caption = "\n".join(filter(None,[
        f"*{head}*", "", desc, "", f"[Lire l’article complet]({link})", "", "@MorrocanFinancialNews"
    ]))

    photo = ""
    if a["image_url"]:
        try:
            url = a["image_url"].replace("(", "%28").replace(")", "%29")
            r = requests.get(url, stream=True, timeout=5)
            if r.ok and r.headers.get("Content-Type","").startswith("image/"):
                photo = urllib.parse.quote(url, safe=":/?&=#")
        except Exception as e:
            print("[DEBUG] img check:", e)

    if photo:
        api=f"https://api.telegram.org/bot{token}/sendPhoto"
        try:
            requests.post(api, json={
                "chat_id":chat, "photo":photo,
                "caption":caption, "parse_mode":"MarkdownV2"
            }, timeout=10).raise_for_status(); return
        except Exception as e:
            print("[DEBUG] sendPhoto failed:", e)

    _tg_send_md(caption)

# ── Parsing genérico ──────────────────────────────────────────────────────── #

def _extract_img(block, sel_img:str, base_url:str)->str:
    for s in [x.strip() for x in sel_img.split(",") if x.strip()]:
        if "::attr(" in s:
            css, attr = re.match(r"(.+)::attr\((.+)\)", s).groups()
            tag = block.select_one(css)
            if tag and tag.has_attr(attr):
                raw = tag[attr]
                if attr == "style" and "background-image" in raw:
                    m = re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    if m: raw = m.group(2)
                return urljoin(base_url, raw)
        else:
            tag = block.select_one(s)
            if tag and tag.has_attr("src"):
                return urljoin(base_url, tag["src"])
    return ""

def _fetch_meta_description(url:str)->str:
    try:
        html = fetch_url(url, timeout=5)
        m = re.search(r'<meta property="og:description" content="([^"]+)"', html, re.I)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""

def parse_articles_generic(html:str, cfg:dict)->list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    sel  = cfg["selectors"]
    arts=[]
    for b in soup.select(sel["container"]):
        a_tag = b.select_one(sel["headline"]); 
        if not a_tag: continue
        head  = a_tag.get_text(strip=True)
        href  = a_tag.get(sel.get("link_attr","href"),""); 
        if not href: continue
        link  = urljoin(cfg["base_url"], href)
        if link.rstrip("/") == cfg["list_url"].rstrip("/"): continue  # cabecera

        desc=""
        if sel.get("description"):
            d=b.select_one(sel["description"])
            if d: desc=d.get_text(strip=True)
        if not desc and cfg["name"]=="leconomiste":
            desc=_fetch_meta_description(link)

        img=_extract_img(b, sel.get("image",""), cfg["base_url"])

        date_txt=""
        if sel.get("date"):
            dt=b.select_one(sel["date"])
            if dt: date_txt=dt.get_text(strip=True)

        parsed=""
        rex=cfg.get("date_regex")
        if rex and date_txt:
            m=re.search(rex, date_txt)
            if m:
                if cfg.get("month_map"):
                    d,mon,y=m.groups(); mn=cfg["month_map"].get(mon,"")
                    if mn: parsed=f"{y}-{mn}-{int(d):02d}"
                else:
                    g1,g2,g3=m.groups()
                    if "/" in date_txt:
                        parsed=f"{g3}-{int(g2):02d}-{int(g1):02d}"
                    else:
                        parsed=f"{g1}-{g2}-{g3}"

        arts.append({
            "headline":head, "description":desc, "link":link,
            "image_url":img, "date":date_txt, "parsed_date":parsed
        })
    return arts

# ── Main loop ─────────────────────────────────────────────────────────────── #

def main():
    today=date.today().isoformat(); sent=load_sent()
    print("[DEBUG] cache len:",len(sent))

    for src in SOURCES:
        print("[DEBUG] -->", src["name"])
        try:
            html=fetch_url(src["list_url"])
        except Exception as e:
            print("[ERROR]",e); continue

        arts=parse_articles_generic(html, src)
        print(f"[DEBUG] tot {len(arts)}")

        new=[a for a in arts
             if (a["parsed_date"]==today or not a["parsed_date"])
             and a["link"] not in sent]

        print(f"[DEBUG] nuevos {len(new)}")
        for i,a in enumerate(new,1):
            print(f"[INFO] {src['name']} {i}/{len(new)} ⇒ {a['headline'][:60]}")
            try:
                send_article(a)
                sent.add(a["link"]); time.sleep(10)
            except Exception as e:
                print("[ERROR] envío:",e)

    save_sent(sent); print("[DEBUG] total cache:",len(sent))

if __name__=="__main__":
    main()
