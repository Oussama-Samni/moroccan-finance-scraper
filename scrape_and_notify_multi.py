#!/usr/bin/env python3
"""
Multi-fuente → Telegram (@MorrocanFinancialNews)
Versión “unificada v1.0”
────────────────────────────────────────────────────────
• FinancesNews, L’Economiste, EcoActu → HTML genérico
• Medias24 LeBoursier → API WP-JSON (cat 14389, _embed)
• Sólo envía artículos de HOY (UTC para Medias24; local para las otras)
• Inserta salto tras el primer “:” del título
• Filtra tablas markdown, etiquetas (“Marché de change”…), fechas sueltas,
  siglas “… Pts” y cabeceras sucias
• Normaliza URLs de imagen y desactiva vista previa de enlaces
"""

import json, os, re, time, tempfile, requests, yaml, html
from datetime     import date, datetime, timezone
from pathlib       import Path
from typing        import Dict, List, Set
from urllib.parse  import urljoin, urlsplit, urlunsplit, quote, quote_plus
from bs4           import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ───────── Config ───────── #
SRC_FILE   = "sources.yml"
CACHE_FILE = Path("sent_articles.json")
TG_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID")
TMP_DIR    = Path(tempfile.gettempdir()) / "mfn_cache"
TMP_DIR.mkdir(exist_ok=True)

TODAY_LOCAL = date.today().isoformat()
TODAY_UTC   = datetime.now(timezone.utc).date()

API_URL = (
    "https://medias24.com/wp-json/wp/v2/posts"
    "?categories=14389&per_page=30&_embed"
)

# ───────── HTTP ───────── #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MoroccanFinanceBot/unified)",
        "Accept-Language": "fr,en;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods={"GET","HEAD"})
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _safe_get(url: str, **kw) -> requests.Response:
    r = _session().get(url, **kw)
    r.raise_for_status()
    return r

# ───────── Telegram ───────── #
_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"  # MarkdownV2 specials
def _esc(t: str) -> str:
    return re.sub(f"([{re.escape(_SPECIAL)}])", r"\\\1", t)

def _newline_title(t: str) -> str:
    return re.sub(r"\s*:\s*", ":\n", t, count=1)

def _mk_msg(title: str, desc: str, link: str) -> str:
    parts = [
        f"*{_esc(_newline_title(title))}*",
        "",
        _esc(desc),
        "",
        f"[Lire l’article complet]({_esc(link)})",
        "",
        "@MorrocanFinancialNews",
    ]
    return "\n".join(p for p in parts if p.strip())

def _norm_img(url: str) -> str:
    sch, net, path, query, frag = urlsplit(url)
    return urlunsplit((sch, net,
                       quote(path,  safe="/%"),
                       quote_plus(query, safe="=&"),
                       frag))

def _send(title: str, desc: str, link: str, img: str):
    cap = _mk_msg(title, desc, link)[:1024]
    body = _mk_msg(title, desc, link)[:4096]
    if img:
        try:
            _session().head(img, timeout=5).raise_for_status()
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                json={"chat_id":TG_CHAT, "photo":_norm_img(img),
                      "caption":cap, "parse_mode":"MarkdownV2"},
                timeout=10
            ).raise_for_status()
            return
        except:
            pass
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id":TG_CHAT, "text":body,
              "parse_mode":"MarkdownV2",
              "disable_web_page_preview":True},
        timeout=10
    ).raise_for_status()

# ───────── Cache ───────── #
def _load_cache() -> Set[str]:
    return set(json.loads(CACHE_FILE.read_text())) if CACHE_FILE.exists() else set()

def _save_cache(c: Set[str]):
    CACHE_FILE.write_text(json.dumps(list(c), ensure_ascii=False, indent=2))

# ────── Filtros comunes ────── #
_PAT_SIGLAS_PTS = re.compile(r"^[A-ZÉÈÎÂÀÇ][A-Z0-9ÉÈÎÂÀÇ\s]{2,20}\s+Pts$")
_SKIP_TAGS_EXACT = {
    "marché de change",
    "la séance du jour",
    "la bourse",
}
_PAT_DATE_LINE   = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}$")
_PAT_INLINE_DATE = re.compile(r"^Le\s+\d+/\d+/\d+\s+à\s+\d")
_PAT_LINK        = re.compile(r"^\[.+\]\(https?://[^\s)]+\)$")

def _skip(txt: str) -> bool:
    low = txt.lower().strip()
    if not low: return True
    if low in _SKIP_TAGS_EXACT: return True
    if _PAT_SIGLAS_PTS.match(txt): return True
    if _PAT_DATE_LINE.match(txt): return True
    if _PAT_INLINE_DATE.match(txt): return True
    if _PAT_LINK.match(txt): return True
    if txt.startswith("|") and txt.count("|")>=2: return True
    if re.fullmatch(r"=+", txt): return True
    return False

def _strip_md_links(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

# ── Parser genérico HTML ── #
def _parse_generic(src: Dict) -> List[Dict]:
    soup = BeautifulSoup(_safe_get(src["list_url"]).text, "html.parser")
    sel  = src["selectors"]
    out  = []
    seen = set()
    for b in soup.select(sel["container"]):
        a = b.select_one(sel["headline"])
        if not a: continue
        title = a.get_text(strip=True)
        link  = urljoin(src["base_url"], a.get(sel.get("link_attr","href"),""))
        if not link or (src["name"]=="financesnews" and link in seen): continue
        seen.add(link)
        desc = ""
        if sel.get("description"):
            d = b.select_one(sel["description"])
            if d: desc = d.get_text(strip=True)
        img = ""
        if sel.get("image"):
            # pega primer src/img/style...
            tag = b.select_one(sel["image"])
            if tag:
                img = tag.get("src") or ""
        # fecha opcional
        pd=""
        if sel.get("date"):
            dt=b.select_one(sel["date"])
            if dt: pd=dt.get_text(strip=True)
            # parsed si regex...
        out.append({"title":title,"desc":desc,"link":link,"img":img,"pdate":pd})
    return out

# ── Parser Medias24 JSON ── #
def fetch_medias24_json() -> list:
    cache = TMP_DIR/"medias24_wp.json"
    if cache.exists() and cache.stat().st_mtime > time.time()-900:
        return json.loads(cache.read_text(encoding="utf-8"))
    data = _safe_get(API_URL, timeout=15).json()
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data

def _clean_html(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(txt).strip()

def _parse_medias24_posts(j: list) -> List[Dict]:
    out=[]
    for post in j:
        d = datetime.fromisoformat(post["date_gmt"].replace("Z","")).date()
        if d != TODAY_UTC: continue
        title = html.unescape(post["title"]["rendered"]).strip()
        link  = post["link"]
        desc  = _clean_html(post.get("excerpt",{}).get("rendered",""))
        low   = desc.lower()
        if low in _SKIP_TAGS_EXACT or _PAT_SIGLAS_PTS.match(desc) or f"journée du {TODAY_UTC.strftime('%d-%m-%Y')}".lower()==low:
            desc=""
        img=""
        m=post.get("_embedded",{}).get("wp:featuredmedia",[])
        if m and m[0].get("source_url"): img=m[0]["source_url"]
        out.append({"title":title,"desc":desc or " ","link":link,"img":img,"pdate":str(d)})
    return out

# ───────── Main ───────── #
def main():
    cache   = _load_cache()
    sources = yaml.safe_load(open(SRC_FILE, encoding="utf-8"))
    ACTIVE  = {"financesnews","leconomiste_economie","ecoactu_nationale","medias24_leboursier"}

    for src in sources:
        name = src["name"]
        if name not in ACTIVE: continue
        print(f"— {name} —")
        if name=="medias24_leboursier":
            data = fetch_medias24_json()
            arts = _parse_medias24_posts(data)
        else:
            arts = _parse_generic(src)

        for a in arts:
            print(" •",a["title"][:70],"| pdate:",a["pdate"])
        print("─"*40)

        for a in arts:
            if a["link"] in cache: continue
            # fecha local vs UTC
            if name=="medias24_leboursier":
                # ya hemos filtrado por UTC arriba
                pass
            else:
                # genéricas: comparo con fecha local YYYY-MM-DD
                if a["pdate"] and a["pdate"][:10]!=TODAY_LOCAL:
                    continue
            print(" Enviando:",a["title"][:60])
            _send(a["title"],a["desc"],a["link"],a["img"])
            cache.add(a["link"])
            time.sleep(8)

    _save_cache(cache)

if __name__=="__main__":
    main()
