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
