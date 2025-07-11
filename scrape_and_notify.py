import os
import time
import re
import requests
import json

SENT_FILE = "sent_articles.json"
FETCH_FAILURES_FILE = "fetch_failures.json"
FETCH_FAILURE_THRESHOLD = 3  # alert after 3 consecutive failures

def load_fetch_failures() -> int:
    """Load current consecutive fetch failure count."""
    try:
        with open(FETCH_FAILURES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return int(data.get("count", 0))
    except FileNotFoundError:
        return 0

def save_fetch_failures(count: int) -> None:
    """Save updated fetch failure count."""
    with open(FETCH_FAILURES_FILE, "w", encoding="utf-8") as f:
        json.dump({"count": count}, f)
        
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import datetime, date
from urllib.parse import urljoin

def load_sent() -> set:
    """Load the set of URLs already sent, or return empty."""
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()

def save_sent(urls: set) -> None:
    """Save the updated set of sent URLs back to the JSON file."""
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(urls), f, ensure_ascii=False, indent=2)

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
    and sends an alert if threshold is exceeded.
    """
    try:
        session = get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        # Increment failure count
        failures = load_fetch_failures() + 1
        save_fetch_failures(failures)
        print(f"ERROR: Fetch attempt failed ({failures}): {e}")

        # If threshold reached, send alert and reset counter
        if failures >= FETCH_FAILURE_THRESHOLD:
            alert_msg = (
                f"⚠️ Fetch failed {failures} times in a row for {url}. "
                "Please check connectivity or site availability."
            )
            save_fetch_failures(0)
        # Re-raise to halt this run
        raise

    # On success, reset failure counter
    save_fetch_failures(0)
    return resp.text


# ===== Parsing HTML and dates =====

BASE_URL = "https://boursenews.ma"
MONTH_MAP = {
    "Janvier":"01","Février":"02","Mars":"03","Avril":"04","Mai":"05","Juin":"06",
    "Juillet":"07","Août":"08","Septembre":"09","Octobre":"10","Novembre":"11","Décembre":"12"
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
        if not (a_tag and p_tag and img_tag and span_date):
            continue

        headline    = a_tag.get_text(strip=True)
        description = p_tag.get_text(strip=True)
        link_raw    = a_tag.get("href", "")
        image_raw   = img_tag.get("src", "")
        date_text   = span_date.get_text(strip=True)

        link      = urljoin(BASE_URL, link_raw)
        image_url = urljoin(BASE_URL, image_raw)
        parsed    = parse_date(date_text)

        articles.append({
            "headline":    headline,
            "description": description,
            "link":        link,
            "image_url":   image_url,
            "date":        date_text,
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
    """
    Sends one article to Telegram as a photo message with caption,
    encoding the image URL to avoid HTTP errors.
    """
    import urllib.parse

    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    api_url = f"https://api.telegram.org/bot{token}/sendPhoto"

    # Encode URL to handle spaces and special characters
    raw_url = article["image_url"]
    photo_url = urllib.parse.quote(raw_url, safe=":/?&=#")

    # Escape HTML special chars in text
    headline = article["headline"].replace("<", "&lt;").replace(">", "&gt;")
    description = article["description"].replace("<", "&lt;").replace(">", "&gt;")
    link = article["link"]
    date_str = article["parsed_date"]

    caption = (
        f"<b>{headline}</b>\n"
        f"{description}\n\n"
        f'<a href="{link}">Read more</a> • {date_str}'
    )

    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    }

    print(f"DEBUG: Sending to {chat_id}, photo={photo_url}")
    try:
        resp = requests.post(api_url, json=payload, timeout=10)
        resp.raise_for_status()
        print("DEBUG: sendPhoto OK")
    except Exception as e:
        print(f"ERROR sending article '{headline}': {e}")
        if 'resp' in locals():
            print("API response:", resp.status_code, resp.text)

def send_alert(message: str) -> None:
    """
    Sends an alert message to your personal Telegram chat.
    """
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
    # Keep only those we haven’t sent yet:
    todays = [a for a in all_today if a["link"] not in sent_urls]

    # If nothing new to send, exit quietly
    if not todays:
        return

    print(f"DEBUG: Found {len(todays)} articles for {today_str}")

    for idx, article in enumerate(todays, 1):
        print(f"DEBUG: Sending article {idx}/{len(todays)}: {article['headline']}")
        try:
            send_article(article)
            print(f"DEBUG: Sent article {idx}/{len(todays)}")
            time.sleep(120) # 2 minutes delay between article sends.
        except Exception as e:
            print(f"ERROR: Failed to send article {idx}/{len(todays)}: {e}")

    # After sending all new articles, update sent list
    sent_urls.update(a["link"] for a in todays)
    save_sent(sent_urls)


if __name__ == "__main__":
    main()

