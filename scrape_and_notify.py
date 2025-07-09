import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text

if __name__ == "__main__":
    test_url = "https://boursenews.ma/articles/marches"
    try:
        html = fetch_url(test_url)
        print("Fetched OK:", html[:200].replace("\n", " "))
    except Exception as e:
        print("Error fetching URL:", e)

from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin

BASE_URL = "https://boursenews.ma"

# Map French month names to numbers
MONTH_MAP = {
    "Janvier":"01","Février":"02","Mars":"03","Avril":"04","Mai":"05","Juin":"06",
    "Juillet":"07","Août":"08","Septembre":"09","Octobre":"10","Novembre":"11","Décembre":"12"
}

def parse_date(date_text: str) -> str:
    """
    Extracts a date in format 'Lundi 26 Mai 2025 - par bourse news'
    and returns 'YYYY-MM-DD'. Returns '' if parsing fails.
    """
    import re
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zéû]+)\s+(\d{4})\b", date_text)
    if not m:
        return ""
    day, mon_name, year = m.groups()
    mon_num = MONTH_MAP.get(mon_name, "")
    if not mon_num:
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"

def parse_articles(html: str) -> list[dict]:
    """
    Parses the HTML and returns a list of article dicts:
    {
      'headline': str,
      'description': str,
      'link': str,
      'image_url': str,
      'date': str,         # raw text
      'parsed_date': str,  # 'YYYY-MM-DD'
    }
    """
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(
        "div.list_item.margin_top_30.margin_bottom_30 div.row"
    )
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
import os
from datetime import date
import telegram  # make sure python-telegram-bot is installed

def format_message(articles: list[dict]) -> str:
    """
    Builds a Markdown-formatted message listing today's articles.
    """
    today = date.today().isoformat()
    if not articles:
        return f"No new articles for {today}."
    
    lines = [f"*📈 Moroccan Finance News – {today}*",""]
    for a in articles:
        # Each line: - [Headline](URL) (YYYY-MM-DD)
        lines.append(f"- [{a['headline']}]({a['link']}) ({a['parsed_date']})")
    return "\n".join(lines)

import requests

def send_telegram(message: str) -> None:
    """
    Sends the given message to the Telegram chat via HTTP API.
    """
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()

def main():
    URL = "https://boursenews.ma/articles/marches"
    html = fetch_url(URL)
    articles = parse_articles(html)
    
    # Filter only today's articles
    today_str = date.today().isoformat()
    todays = [a for a in articles if a["parsed_date"] == today_str]
    
    message = format_message(todays)
    send_telegram(message)

if __name__ == "__main__":
    main()



