"""
Scraper multi‑fuente → Telegram (@MorrocanFinancialNews)
Fuentes: FinancesNews · L’Economiste · Médias24 · EcoActu · Bourse de Casablanca
"""

import os, re, time, urllib.parse, urllib3
from datetime import date
from urllib.parse import urljoin, urlparse

import requests, yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings()  # suprime warning SSL de Casablanca‑Bourse

# ───────────── HTTP session ───────────── #

def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:117.0) Gecko/20100101 Firefox/117.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://medias24.com/",
    })
    retry = Retry(
        total=5, backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _get_with_fallback(session: requests.Session, url: str,
                       timeout: float, verify: bool):
    """
    GET con dos niveles de fallback para Médias24.
    """
    resp = session.get(url, timeout=timeout, verify=verify)
    if resp.status_code == 403 and "medias24.com" in url:
        alt = url.rstrip("/") + "/amp/"
        print("[DEBUG] Medias24 403 – pruebo AMP:", alt)
        resp = session.get(alt, timeout=timeout, verify=verify)
        if resp.status_code == 403:
            alt2 = alt + "?outputType=amp&refresh=true"
            print("[DEBUG] Medias24 403 – pruebo AMP+OT:", alt2)
            resp = session.get(alt2, timeout=timeout, verify=verify)
    resp.raise_for_status()
    return resp


def fetch_url(url: str, timeout: float = 10.0) -> str:
    sess = _get_session()
    verify = not ("casablanca-bourse.com" in urlparse(url).netloc)
    return _get_with_fallback(sess, url, timeout, verify).text

# ───────────── Estado cache ───────────── #
from scrape_and_notify import load_sent, save_sent
with open("sources.yml", encoding="utf-8") as f:
    SOURCES = yaml.safe_load(f)

# ───────────── Telegram helpers ────────── #
def _escape_md(t: str) -> str:
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!\\])', r'\\\1', t)

def _tg_send_md(msg: str):
    tk, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    requests.post(
        f"https://api.telegram.org/bot{tk}/sendMessage",
        json={"chat_id": chat, "text": msg,
              "parse_mode": "MarkdownV2",
              "disable_web_page_preview": False},
        timeout=10,
    ).raise_for_status()

def send_article(a: dict):
    tk, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    head = _escape_md(a["headline"])
    desc = _escape_md(a["description"])
    link = _escape_md(a["link"])

    caption = "\n".join(
        filter(None, [
            f"*{head}*",
            "",
            desc,
            "",
            f"[Lire l’article complet]({link})",
            "",
            "@MorrocanFinancialNews",
        ])
    )

    photo = ""
    if a["image_url"]:
        try:
            url = a["image_url"].replace("(", "%28").replace(")", "%29")
            r = requests.get(url, stream=True, timeout=5)
            if r.ok and r.headers.get("Content-Type", "").startswith("image/"):
                photo = urllib.parse.quote(url, safe=":/?&=#")
        except Exception as e:
            print("[DEBUG] img check:", e)

    if photo:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tk}/sendPhoto",
                json={
                    "chat_id": chat,
                    "photo": photo,
                    "caption": caption,
                    "parse_mode": "MarkdownV2",
                },
                timeout=10,
            ).raise_for_status()
            return
        except Exception as e:
            print("[DEBUG] sendPhoto fallback:", e)

    _tg_send_md(caption)

# ───────────── Parsing helpers ────────── #
def _extract_img(block, sel_img: str, base_url: str) -> str:
    for s in [x.strip() for x in sel_img.split(",") if x.strip()]:
        if "::attr(" in s:
            css, attr = re.match(r"(.+)::attr\((.+)\)", s).groups()
            tag = block.select_one(css)
            if tag and tag.has_attr(attr):
                raw = tag[attr]
                if attr == "style" and "background-image" in raw:
                    m = re.search(r'url\((["\']?)(.*?)\1\)', raw)
                    raw = (
                        m.group(2)
                        if m
                        else raw.split("url(", 1)[-1].rstrip(")").strip("\"'")
                    )
                return urljoin(base_url, raw)
        else:
            tag = block.select_one(s)
            if tag and tag.has_attr("src"):
                return urljoin(base_url, tag["src"])
    return ""

def _meta_description(url: str) -> str:
    try:
        html = fetch_url(url, 5)
        m = re.search(
            r'<meta property="og:description" content="([^"]+)"', html, re.I
        )
        return m.group(1).strip() if m else ""
    except Exception:
        return ""

def parse_articles_generic(html: str, cfg: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    sel = cfg["selectors"]
    out = []
    for b in soup.select(sel["container"]):
        a = b.select_one(sel["headline"])
        if not a:
            continue
        head = a.get_text(strip=True)
        href = a.get(sel.get("link_attr", "href"), "")
        if not href:
            continue
        link = urljoin(cfg["base_url"], href)
        if link.rstrip("/") == cfg["list_url"].rstrip("/"):
            continue

        desc = ""
        if sel.get("description"):
            d = b.select_one(sel["description"])
            if d:
                desc = d.get_text(strip=True)
        if not desc and cfg["name"] == "leconomiste":
            desc = _meta_description(link)

        img = _extract_img(b, sel.get("image", ""), cfg["base_url"])

        date_txt = (
            b.select_one(sel["date"]).get_text(strip=True)
            if sel.get("date") and b.select_one(sel["date"])
            else ""
        )
        date_txt = date_txt.replace("\u00A0", " ")  # ← normalize NBSP --------------

        parsed = ""
        rex = cfg.get("date_regex")
        if rex and date_txt and (m := re.search(rex, date_txt)):
            if cfg.get("month_map"):
                d, mon, y = m.groups()
                mn = cfg["month_map"].get(mon, "")
                if mn:
                    parsed = f"{y}-{mn}-{int(d):02d}"
            else:
                g1, g2, g3 = m.groups()
                parsed = (
                    f"{g3}-{int(g2):02d}-{int(g1):02d}"
                    if "/" in date_txt
                    else f"{g1}-{g2}-{g3}"
                )

        out.append(
            {
                "headline": head,
                "description": desc,
                "link": link,
                "image_url": img,
                "date": date_txt,
                "parsed_date": parsed,
            }
        )
    return out

# ───────────── Main ───────────── #
def main():
    today = date.today().isoformat()
    sent = load_sent()
    print("[DEBUG] cache len:", len(sent))

    for src in SOURCES:
        print("[DEBUG] -->", src["name"])
        try:
            html = fetch_url(src["list_url"])
        except Exception as e:
            print("[ERROR]", src["name"], e)
            continue

        arts = parse_articles_generic(html, src)
        print(f"[DEBUG] {src['name']} total:", len(arts))

        new = [
            a
            for a in arts
            if (a["parsed_date"] == today or not a["parsed_date"])
            and a["link"] not in sent
        ]
        print(f"[DEBUG] {src['name']} nuevos:", len(new))

        for a in new:
            try:
                send_article(a)
                sent.add(a["link"])
                time.sleep(10)
            except Exception as e:
                print("[ERROR] envío", src["name"], e)

    save_sent(sent)
    print("[DEBUG] total cache:", len(sent))


if __name__ == "__main__":
    main()
