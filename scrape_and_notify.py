import os
import time
import re
import requests
import json

from datetime import date
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ===== State files =====
SENT_FILE = "sent_articles.json"
FETCH_FAILURES_FILE = "fetch_failures.json"
FETCH_FAILURE_THRESHOLD = 3  # alert after 3 consecutive failures

# ===== Fetch failure tracking =====

def load_fetch_failures() -> int:
    """Load current consecutive fetch failure count."""
    try:
        with open(FETCH_FAILURES_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("count", 0))
    except FileNotFoundError:
        return 0


def save_fetch_failures(count: int) -> None:
    """Save updated fetch failure count."""
    with open(FETCH_FAILURES_FILE, "w", encoding="utf-8") as f:
        json.dump({"count": count}, f)

# ===== Sent URLs state =====

def load_sent() -> set:
    """
    Load the set of URLs already sent *today*.
    Supports legacy list format or new dict format with date scoping.
    """
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

    if isinstance(data, list):
        # Legacy format
        return set(data)

    if data.get("date") != date.today().isoformat():
        return set()
    return set(data.get("urls", []))


def save_sent(urls: set) -> None:
    """
    Save today’s date and the list of sent URLs.
    Always writes the new dict format.
    """
    payload = {
        "date": date.today().isoformat(),
        "urls": sorted(urls)
    }
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

# ===== HTTP fetching with retries =====

def get_session(
    total_retries: int = 5,
    backoff_factor: float = 1.0,
    status_forcelist: tuple = (429, 500, 502, 503, 504),
    user_agent: str = "Mozilla/5.0 (compatible; FinanceScraper/1.0; +https://github.com/yourusername/moroccan-finance-scraper)"
) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    retry_strategy = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_url(url: str, timeout: float = 10.0) -> str:
    """
    Fetches the given URL with retry logic. Tracks consecutive failures
    and re-raises on failure after logging.
    """
    try:
        session = get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        failures = load_fetch_failures() + 1
        save_fetch_failures(failures)
        print(f"ERROR: Fetch attempt failed ({failures}): {e}")
        if failures >= FETCH_FAILURE_THRESHOLD:
            save_fetch_failures(0)
        raise

    save_fetch_failures(0)
    return resp.text

# ===== Parsing HTML and dates =====

BASE_URL = "https://boursenews.ma"
MONTH_MAP = {
    "Janvier": "01", "Février": "02", "Mars": "03", "Avril": "04",
    "Mai": "05", "Juin": "06", "Juillet": "07", "Août": "08",
    "Septembre": "09", "Octobre": "10", "Novembre": "11", "Décembre": "12"
}


def parse_date(date_text: str) -> str:
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zéû]+)\s+(\d{4})\b", date_text)
    if not m:
        return ""
    day, mon_name, year = m.groups()
    mon_num = MONTH_MAP.get(mon_name, "")
    if not mon_num:
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"


def parse_articles(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select("div.list_item.margin_top_30.margin_bottom_30 div.row")
    articles = []
    for c in containers:
        a_tag = c.select_one("h3 a")
        p_tag = c.select_one("p")
        img_tag = c.select_one("img")
        span_date = c.select_one("h3 a span")
        if not (a_tag and img_tag and span_date):
            continue

        headline = a_tag.get_text(strip=True)
        description = p_tag.get_text(strip=True) if p_tag else ""
        link_raw = a_tag.get("href", "")
        image_raw = img_tag.get("src", "")
        date_text = span_date.get_text(strip=True)

        link = urljoin(BASE_URL, link_raw)
        image_url = urljoin(BASE_URL, image_raw)
        parsed = parse_date(date_text)

        if not headline or not link.startswith("http"):
            continue

        articles.append({
            "headline": headline,
            "description": description,
            "link": link,
            "image_url": image_url,
            "date": date_text,
            "parsed_date": parsed,
        })

    return articles

# ===== Telegram sending =====

def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()


def send_article(article: dict) -> None:
    import urllib.parse

    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    headline = article["headline"].replace("<", "&lt;").replace(">", "&gt;")
    description = article["description"].strip().replace("<", "&lt;").replace(">", "&gt;")
    link = article["link"]

    parts = [f"<b>{headline}</b>"]
    if description:
        parts.extend(["", description])
    parts.extend([
        "",
        f'<a href="{link}">Lire l’article complet</a>',
        "",
        "@MorrocanFinancialNews"
    ])
    caption = "\n".join(parts)

    original_url = article["image_url"]
    try:
        head_resp = requests.head(original_url, timeout=5)
        content_type = head_resp.headers.get("Content-Type", "")
        if head_resp.status_code == 200 and content_type.startswith("image/"):
            photo_url = urllib.parse.quote(original_url, safe=":/?&=#")
            api_url = f"https://api.telegram.org/bot{token}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML"
            }
            print(f"DEBUG: Sending photo to {chat_id}, photo={photo_url}")
            resp = requests.post(api_url, json=payload, timeout=10)
            resp.raise_for_status()
            print("DEBUG: sendPhoto OK")
            return
    except Exception as e:
        print(f"DEBUG: Image HEAD check failed, falling back to text: {e}")

    print("DEBUG: Sending text-only fallback")
    send_telegram(caption)


def send_alert(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    alert_chat = os.getenv("TELEGRAM_ALERT_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": alert_chat,
        "text": message,
        "parse_mode": "HTML"
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()


# ===== Main workflow =====

def main():
    URL = "https://boursenews.ma/articles/marches"
    html = fetch_url(URL)
    articles = parse_articles(html)

    print(f"DEBUG: Parsed {len(articles)} total articles")
    today_str = date.today().isoformat()
    all_today = [a for a in articles if a["parsed_date"] == today_str]

    sent_urls = load_sent()
    print(f"DEBUG: Loaded {len(sent_urls)} sent URLs from cache")
    todays = [a for a in all_today if a["link"] not in sent_urls]

    if not todays:
        return

    print(f"DEBUG: Found {len(todays)} articles for {today_str}")

    for idx, article in enumerate(todays, 1):
        print(f"DEBUG: Sending article {idx}/{len(todays)}: {article['headline']}")
        try:
            send_article(article)
            print(f"DEBUG: Sent article {idx}/{len(todays)}")
            time.sleep(10)
        except Exception as e:
            print(f"ERROR: Failed to send article {idx}/{len(todays)}: {e}")

    sent_urls.update(a["link"] for a in todays)
    save_sent(sent_urls)
    print(f"DEBUG: saved {len(sent_urls)} URLs to sent_articles.json")


if __name__ == "__main__":
    main()
