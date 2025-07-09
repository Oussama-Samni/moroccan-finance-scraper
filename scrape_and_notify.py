import os
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import datetime, date
from urllib.parse import urljoin

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
    session = get_session()
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
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
    Sends one article to Telegram as a photo message with caption, using HTML parse mode.
    """
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendPhoto"

    # Build caption in HTML
    # Wrap headline in <b>…</b> and the link in <a href="…">Read more</a>
    headline = article['headline'].replace('<', '&lt;').replace('>', '&gt;')
    description = article['description'].replace('<', '&lt;').replace('>', '&gt;')
    link = article['link']
    date_str = article['parsed_date']

    caption = (
        f"<b>{headline}</b>\n"
        f"{description}\n\n"
        f'<a href="{link}">Read more</a> • {date_str}'
    )

    payload = {
        "chat_id": chat_id,
        "photo": article["image_url"],
        "caption": caption,
        "parse_mode": "HTML"
    }

    # Optional debugging
    print(f"DEBUG: Sending to {chat_id}, photo={article['image_url']}")
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("DEBUG: sendPhoto OK")
    except Exception as e:
        print(f"ERROR sending article '{headline}': {e}")
        if 'r' in locals():
            print("API response:", r.status_code, r.text)


# ===== Main workflow =====

def main():
    URL = "https://boursenews.ma/articles/marches"
    html = fetch_url(URL)
    articles = parse_articles(html)

    print(f"DEBUG: Parsed {len(articles)} total articles")
    today_str = date.today().isoformat()
    todays = [a for a in articles if a["parsed_date"] == today_str]
    print(f"DEBUG: Found {len(todays)} articles for {today_str}")

    if not todays:
        send_telegram(f"No new articles for {today_str}.")
        return

    for article in todays:
        send_article(article)

if __name__ == "__main__":
    main()
