#!/usr/bin/env python3
"""
Multi-source Moroccan Finance Scraper -> Telegram
Scrapes multiple news sources and posts to @MoroccanFinancialNews

Features:
- Multi-source support via sources.yml
- SSRF protection with per-source domain allowlists
- Atomic file writes for state persistence
- Fetch failure tracking with alerts
- Telegram retry logic (429, 5xx)
- HTML escaping for Telegram messages
"""

import html
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===== Configuration =====
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(_SCRIPT_DIR, "sources.yml")
SENT_FILE = os.path.join(_SCRIPT_DIR, "sent_articles.json")
FETCH_FAILURES_FILE = os.path.join(_SCRIPT_DIR, "fetch_failures.json")
FETCH_FAILURE_THRESHOLD = 3

# Track if a fatal alert was already sent this run to avoid duplicates
_fatal_alert_sent_this_run = False


# ===== Fetch Failure Tracking =====

def load_fetch_failures() -> int:
    """Load current consecutive fetch failure count."""
    try:
        with open(FETCH_FAILURES_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("count", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def save_fetch_failures(count: int) -> None:
    """Save updated fetch failure count atomically."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_SCRIPT_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"count": count}, f)
        os.replace(tmp_path, FETCH_FAILURES_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ===== Sent URLs State =====

def load_sent() -> set:
    """Load the set of URLs already sent today."""
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

    if isinstance(data, list):
        return set(data)

    if data.get("date") != date.today().isoformat():
        return set()

    urls = data.get("urls", [])
    if not isinstance(urls, list):
        return set()
    return set(urls)


def save_sent(urls: set) -> None:
    """Save today's date and the list of sent URLs atomically."""
    payload = {
        "date": date.today().isoformat(),
        "urls": sorted(urls)
    }
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_SCRIPT_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SENT_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ===== HTTP Session =====

def get_session(
    total_retries: int = 5,
    backoff_factor: float = 1.0,
    status_forcelist: tuple = (429, 500, 502, 503, 504),
) -> requests.Session:
    """Create HTTP session with retry logic."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    retry_strategy = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_url(url: str, timeout: float = 15.0) -> str:
    """Fetch URL with retry logic and failure tracking."""
    global _fatal_alert_sent_this_run
    try:
        session = get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        failures = load_fetch_failures() + 1
        save_fetch_failures(failures)
        print(f"ERROR: Fetch attempt failed ({failures}): {e}")
        if failures >= FETCH_FAILURE_THRESHOLD:
            send_alert(f"ALERT: {failures} consecutive fetch failures. Last error: {e}")
            save_fetch_failures(0)
            _fatal_alert_sent_this_run = True
        raise

    save_fetch_failures(0)
    return resp.text


def fetch_json(url: str, timeout: float = 15.0) -> dict:
    """Fetch JSON from URL with retry logic."""
    global _fatal_alert_sent_this_run
    try:
        session = get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        failures = load_fetch_failures() + 1
        save_fetch_failures(failures)
        print(f"ERROR: JSON fetch failed ({failures}): {e}")
        if failures >= FETCH_FAILURE_THRESHOLD:
            send_alert(f"ALERT: {failures} consecutive fetch failures. Last error: {e}")
            save_fetch_failures(0)
            _fatal_alert_sent_this_run = True
        raise


# ===== SSRF Protection =====

def is_safe_url(url: str, allowed_domains: list) -> bool:
    """Validate URL is from an allowed domain to prevent SSRF."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        # Check if domain matches any allowed domain
        netloc = parsed.netloc.lower()
        for domain in allowed_domains:
            if netloc == domain or netloc.endswith("." + domain):
                return True
        return False
    except Exception:
        return False


# ===== Source Loading =====

def load_sources() -> list:
    """Load source configurations from YAML file."""
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except Exception as e:
        print(f"ERROR: Failed to load sources.yml: {e}")
        return []


# ===== Date Parsing =====

def parse_date_french(date_text: str, month_map: dict) -> str:
    """Parse French date format: '24 Janvier 2026' -> '2026-01-24'"""
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z\u00c0-\u00ff]+)\s+(\d{4})\b", date_text)
    if not m:
        return ""
    day, mon_name, year = m.groups()
    mon_num = month_map.get(mon_name.capitalize(), "")
    if not mon_num:
        return ""
    return f"{year}-{mon_num}-{int(day):02d}"


def parse_date_dmy_slash(date_text: str) -> str:
    """Parse date format: 'dd/mm/yy' or 'dd/mm/yyyy' -> '2026-01-24'"""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", date_text)
    if not m:
        return ""
    day, month, year = m.groups()
    if len(year) == 2:
        year = "20" + year
    return f"{year}-{int(month):02d}-{int(day):02d}"


def parse_date(date_text: str, source: dict) -> str:
    """Parse date based on source configuration."""
    if not date_text:
        return ""

    date_format = source.get("date_format", "french")

    if date_format == "dmy_slash":
        return parse_date_dmy_slash(date_text)
    else:
        month_map = source.get("month_map", {})
        return parse_date_french(date_text, month_map)


# ===== HTML Parsing =====

def extract_image_url(tag, image_attrs: list, base_url: str) -> str:
    """Extract image URL checking multiple attributes for lazy-loading."""
    if not tag:
        return ""
    for attr in image_attrs:
        val = tag.get(attr)
        if val:
            # Handle background-image in style attribute
            if attr == "style" and "background-image" in val:
                m = re.search(r'url\(["\']?([^"\')\s]+)["\']?\)', val)
                if m:
                    return urljoin(base_url, m.group(1))
            else:
                return urljoin(base_url, val)
    return ""


def parse_html_source(source: dict) -> list:
    """Parse articles from an HTML source using CSS selectors."""
    try:
        html_content = fetch_url(source["list_url"])
    except Exception as e:
        print(f"ERROR: Failed to fetch {source['name']}: {e}")
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    sel = source.get("selectors", {})
    base_url = source.get("base_url", "")
    allowed_domains = source.get("allowed_domains", [])
    image_attrs = source.get("image_attrs", ["src"])

    articles = []
    seen_links = set()

    containers = soup.select(sel.get("container", ""))
    for container in containers:
        # Extract headline and link
        headline_tag = container.select_one(sel.get("headline", ""))
        if not headline_tag:
            continue

        link_attr = sel.get("link_attr", "href")
        link_raw = headline_tag.get(link_attr, "")
        link = urljoin(base_url, link_raw)

        if not link or link in seen_links:
            continue
        seen_links.add(link)

        # Extract date first (for sources like BourseNews where date is in headline)
        date_text = ""
        date_sel = sel.get("date", "")
        if date_sel:
            date_tag = container.select_one(date_sel)
            if date_tag:
                date_text = date_tag.get_text(strip=True)

        # Remove date spans from headline before extracting text
        headline_clone = headline_tag
        for span in headline_clone.find_all("span"):
            span.decompose()
        headline = headline_clone.get_text(strip=True)

        if not headline:
            continue

        # Extract description
        description = ""
        desc_sel = sel.get("description", "")
        if desc_sel:
            desc_tag = container.select_one(desc_sel)
            if desc_tag:
                description = desc_tag.get_text(strip=True)

        # Extract image URL with lazy-loading support
        image_url = ""
        img_sel = sel.get("image", "")
        if img_sel:
            # Support multiple selectors separated by comma
            for img_selector in img_sel.split(","):
                img_tag = container.select_one(img_selector.strip())
                if img_tag:
                    image_url = extract_image_url(img_tag, image_attrs, base_url)
                    if image_url:
                        break

        # Validate image URL against allowed domains
        if image_url and not is_safe_url(image_url, allowed_domains):
            print(f"DEBUG: Rejected image URL (SSRF): {image_url}")
            image_url = ""

        # Parse date
        parsed_date = parse_date(date_text, source)

        articles.append({
            "source": source["name"],
            "headline": headline,
            "description": description,
            "link": link,
            "image_url": image_url,
            "date_text": date_text,
            "parsed_date": parsed_date,
        })

    return articles


# ===== WP-JSON Parsing (Medias24) =====

def clean_html_text(raw: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_wpjson_source(source: dict) -> list:
    """Parse articles from a WordPress JSON API."""
    try:
        data = fetch_json(source["api_url"])
    except Exception as e:
        print(f"ERROR: Failed to fetch {source['name']}: {e}")
        return []

    allowed_domains = source.get("allowed_domains", [])
    today_utc = datetime.now(timezone.utc).date()

    articles = []
    for post in data:
        # Parse date from API
        try:
            post_date = datetime.fromisoformat(
                post.get("date_gmt", "").replace("Z", "")
            ).date()
        except (ValueError, AttributeError):
            continue

        # Only include today's articles
        if post_date != today_utc:
            continue

        title = clean_html_text(post.get("title", {}).get("rendered", ""))
        link = post.get("link", "")
        excerpt = clean_html_text(post.get("excerpt", {}).get("rendered", ""))

        # Filter out generic/boilerplate descriptions
        excerpt_lower = excerpt.lower()
        boilerplate = [
            "marché de change", "la séance du jour", "la bourse",
            f"journée du {today_utc:%d-%m-%Y}".lower()
        ]
        if any(bp in excerpt_lower for bp in boilerplate):
            excerpt = ""
        # Filter stock ticker patterns
        if re.match(r"^[A-Z\s]{2,20}\s+Pts$", excerpt):
            excerpt = ""

        # Extract featured image
        image_url = ""
        # Try WordPress featured media first
        embedded = post.get("_embedded", {})
        featured = embedded.get("wp:featuredmedia", [])
        if featured and len(featured) > 0:
            image_url = featured[0].get("source_url", "")
        # Fallback: Try Yoast SEO og_image (used by L'Economiste)
        if not image_url:
            yoast = post.get("yoast_head_json", {})
            og_images = yoast.get("og_image", [])
            if og_images and len(og_images) > 0:
                image_url = og_images[0].get("url", "")

        # Validate image URL
        if image_url and not is_safe_url(image_url, allowed_domains):
            print(f"DEBUG: Rejected image URL (SSRF): {image_url}")
            image_url = ""

        articles.append({
            "source": source["name"],
            "headline": title,
            "description": excerpt or "",
            "link": link,
            "image_url": image_url,
            "date_text": str(post_date),
            "parsed_date": str(post_date),
        })

    return articles


# ===== Telegram Sending =====

def telegram_request(url: str, payload: dict, max_retries: int = 3) -> requests.Response:
    """Make Telegram API request with retry logic."""
    retry_codes = {429, 500, 502, 503, 504}
    last_error = None

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code in retry_codes:
                retry_after = int(resp.headers.get("Retry-After", 5))
                print(f"DEBUG: Telegram {resp.status_code}, waiting {retry_after}s (attempt {attempt + 1})")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_error = e
            print(f"DEBUG: Telegram request failed: {e} (attempt {attempt + 1})")
            time.sleep(5)
            continue

    if last_error:
        raise last_error
    resp.raise_for_status()
    return resp


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_article(article: dict) -> None:
    """Send article to Telegram channel."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")

    headline = escape_html(article["headline"])
    description = escape_html(article["description"].strip())
    # Escape URL for HTML href attribute
    link = article["link"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # Build caption with length limit (1024 for photos)
    MAX_CAPTION = 1024

    parts_base = [
        f"<b>{headline}</b>",
        "",
        f'<a href="{link}">Lire l\'article complet</a>',
        "",
        "@MoroccanFinancialNews"
    ]
    base_caption = "\n".join(parts_base)
    available_for_desc = MAX_CAPTION - len(base_caption) - 2

    parts = [f"<b>{headline}</b>"]
    if description and available_for_desc > 20:
        if len(description) > available_for_desc:
            description = description[:available_for_desc - 3] + "..."
        parts.extend(["", description])
    parts.extend([
        "",
        f'<a href="{link}">Lire l\'article complet</a>',
        "",
        "@MoroccanFinancialNews"
    ])
    caption = "\n".join(parts)

    # Try sending with photo
    image_url = article.get("image_url", "")
    if image_url:
        try:
            head_resp = requests.head(image_url, timeout=5, allow_redirects=True)
            content_type = head_resp.headers.get("Content-Type", "")
            if head_resp.status_code == 200 and content_type.startswith("image/"):
                api_url = f"https://api.telegram.org/bot{token}/sendPhoto"
                payload = {
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML"
                }
                telegram_request(api_url, payload)
                return
        except Exception as e:
            print(f"DEBUG: Image check failed, using text fallback: {e}")

    # Fallback to text message
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    telegram_request(api_url, payload)


def send_alert(message: str) -> None:
    """Send alert to admin channel."""
    token = os.getenv("TELEGRAM_TOKEN")
    alert_chat = os.getenv("TELEGRAM_ALERT_CHAT_ID")
    if not token or not alert_chat:
        print(f"WARNING: Cannot send alert (missing env vars): {message}")
        return

    escaped = escape_html(message)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": alert_chat,
        "text": escaped,
        "parse_mode": "HTML"
    }
    try:
        telegram_request(url, payload)
    except Exception as e:
        print(f"ERROR: Failed to send alert: {e}")


# ===== Main Workflow =====

def main():
    """Main scraping workflow."""
    sources = load_sources()
    if not sources:
        print("ERROR: No sources configured")
        send_alert("ERROR: No sources configured in sources.yml")
        return

    sent_urls = load_sent()
    print(f"DEBUG: Loaded {len(sent_urls)} previously sent URLs")

    today_str = date.today().isoformat()
    total_sent = 0

    for source in sources:
        name = source.get("name", "unknown")
        source_type = source.get("type", "html")
        print(f"\n=== Processing: {name} ({source_type}) ===")

        # Parse articles based on source type
        if source_type == "wp-json":
            articles = parse_wpjson_source(source)
        else:
            articles = parse_html_source(source)

        print(f"DEBUG: Parsed {len(articles)} articles from {name}")

        # Alert on zero articles (possible site structure change)
        if len(articles) == 0:
            print(f"WARNING: Zero articles from {name}")
            # Don't alert for every source, just log it

        # Filter to today's articles not yet sent
        new_articles = []
        for a in articles:
            if a["link"] in sent_urls:
                continue
            if a["parsed_date"] and a["parsed_date"] != today_str:
                continue
            new_articles.append(a)

        print(f"DEBUG: {len(new_articles)} new articles to send from {name}")

        # Send articles
        for idx, article in enumerate(new_articles, 1):
            print(f"  Sending {idx}/{len(new_articles)}: {article['headline'][:50]}...")
            try:
                send_article(article)
                sent_urls.add(article["link"])
                save_sent(sent_urls)
                total_sent += 1
                time.sleep(8)  # Rate limiting
            except Exception as e:
                print(f"  ERROR: Failed to send: {e}")

    print(f"\n=== Summary: Sent {total_sent} articles across {len(sources)} sources ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: Unhandled exception: {e}")
        if not _fatal_alert_sent_this_run:
            try:
                send_alert(f"FATAL: Scraper crashed: {e}")
            except Exception:
                pass
        raise
