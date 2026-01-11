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
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def save_fetch_failures(count: int) -> None:
    """Save updated fetch failure count atomically."""
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=".", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"count": count}, f)
        os.replace(tmp_path, FETCH_FAILURES_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

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
    Save today's date and the list of sent URLs atomically.
    Always writes the new dict format.
    """
    import tempfile
    payload = {
        "date": date.today().isoformat(),
        "urls": sorted(urls)
    }
    tmp_fd, tmp_path = tempfile.mkstemp(dir=".", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SENT_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

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
            send_alert(f"ALERT: {failures} consecutive fetch failures for BourseNews scraper. Last error: {e}")
            save_fetch_failures(0)
        raise

    save_fetch_failures(0)
    return resp.text

# ===== Parsing HTML and dates =====

BASE_URL = "https://boursenews.ma"
ALLOWED_IMAGE_DOMAINS = {"boursenews.ma", "www.boursenews.ma"}


def is_safe_url(url: str) -> bool:
    """Validate URL is from an allowed domain to prevent SSRF."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc in ALLOWED_IMAGE_DOMAINS
    except Exception:
        return False


MONTH_MAP = {
    "Janvier": "01", "Février": "02", "Mars": "03", "Avril": "04",
    "Mai": "05", "Juin": "06", "Juillet": "07", "Août": "08",
    "Septembre": "09", "Octobre": "10", "Novembre": "11", "Décembre": "12"
}


def parse_date(date_text: str) -> str:
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zéû]+)\s+(\d{4})\b", date_text)
    if not m:
        print(f"DEBUG: Date regex failed for: {date_text!r}")
        return ""
    day, mon_name, year = m.groups()
    # Case-insensitive month lookup
    mon_num = MONTH_MAP.get(mon_name.capitalize(), "")
    if not mon_num:
        print(f"DEBUG: Unknown month name: {mon_name!r} in date: {date_text!r}")
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"


def parse_articles(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    # Simplified selector - avoid layout classes that may change
    containers = soup.select("div.list_item div.row")
    articles = []
    for c in containers:
        a_tag = c.select_one("h3 a")
        p_tag = c.select_one("p")
        img_tag = c.select_one("img")
        span_date = c.select_one("h3 a span")
        if not (a_tag and img_tag and span_date):
            continue

        date_text = span_date.get_text(strip=True)
        for span in a_tag.find_all("span"):
            span.decompose()
        headline = a_tag.get_text(strip=True)
        description = p_tag.get_text(strip=True) if p_tag else ""
        link_raw = a_tag.get("href", "")
        image_raw = img_tag.get("src", "")

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

def telegram_request(url: str, payload: dict, max_retries: int = 3) -> requests.Response:
    """Make a Telegram API request with retry logic for 429 rate limits."""
    for attempt in range(max_retries):
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            print(f"DEBUG: Telegram rate limit hit, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables are required")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    telegram_request(url, payload)


def send_article(article: dict) -> None:
    import urllib.parse

    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables are required")

    headline = article["headline"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    description = article["description"].strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    link = article["link"]

    # Telegram photo caption limit is 1024 characters
    MAX_CAPTION_LENGTH = 1024

    # Build caption without description first to calculate available space
    parts_without_desc = [
        f"<b>{headline}</b>",
        "",
        f'<a href="{link}">Lire l\'article complet</a>',
        "",
        "@MorrocanFinancialNews"
    ]
    base_caption = "\n".join(parts_without_desc)

    # Calculate space for description (2 newlines before it)
    available_for_desc = MAX_CAPTION_LENGTH - len(base_caption) - 2

    parts = [f"<b>{headline}</b>"]
    if description and available_for_desc > 20:
        if len(description) > available_for_desc:
            description = description[:available_for_desc - 3] + "..."
        parts.extend(["", description])
    parts.extend([
        "",
        f'<a href="{link}">Lire l\'article complet</a>',
        "",
        "@MorrocanFinancialNews"
    ])
    caption = "\n".join(parts)

    original_url = article["image_url"]
    try:
        if not is_safe_url(original_url):
            raise ValueError(f"URL not from allowed domain: {original_url}")
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
            telegram_request(api_url, payload)
            print("DEBUG: sendPhoto OK")
            return
    except Exception as e:
        print(f"DEBUG: Image HEAD check failed, falling back to text: {e}")

    print("DEBUG: Sending text-only fallback")
    send_telegram(caption)


def send_alert(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    alert_chat = os.getenv("TELEGRAM_ALERT_CHAT_ID")
    if not token or not alert_chat:
        print(f"WARNING: Cannot send alert (missing TELEGRAM_TOKEN or TELEGRAM_ALERT_CHAT_ID): {message}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": alert_chat,
        "text": message,
        "parse_mode": "HTML"
    }
    telegram_request(url, payload)


# ===== Main workflow =====

def main():
    URL = "https://boursenews.ma/articles/marches"
    html = fetch_url(URL)
    articles = parse_articles(html)

    print(f"DEBUG: Parsed {len(articles)} total articles")

    # Detect potential page structure change
    if len(articles) == 0:
        print("WARNING: Zero articles parsed - page structure may have changed")
        send_alert("WARNING: BourseNews scraper parsed 0 articles. The website structure may have changed. Please check the CSS selectors.")

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
            sent_urls.add(article["link"])
            save_sent(sent_urls)
            print(f"DEBUG: Sent article {idx}/{len(todays)}")
            time.sleep(10)
        except Exception as e:
            print(f"ERROR: Failed to send article {idx}/{len(todays)}: {e}")

    print(f"DEBUG: saved {len(sent_urls)} URLs to sent_articles.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: Unhandled exception in main(): {e}")
        try:
            send_alert(f"FATAL: BourseNews scraper crashed with unhandled exception: {e}")
        except Exception:
            pass
        raise
