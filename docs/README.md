# Moroccan Finance Scraper

**What it is:**  
A Python + GitHub Actions solution that scrapes Moroccan stock-market news from BourseNews.ma and posts new items (with images) to a Telegram group hourly.

## Architecture

- **`scrape_and_notify.py`**: Main script  
  - Fetches HTML with retries (requests + backoff)  
  - Parses article data (BeautifulSoup, date normalization)  
  - Filters out already-sent URLs (`sent_articles.json`)  
  - Posts each new article via Telegram HTTP API, pacing posts with a configurable delay  
  - Commits updates back to the repo  

- **GitHub Actions workflow** (`.github/workflows/scrape.yml`)  
  - Triggers every hour + manual dispatch  
  - Runs on `ubuntu-latest`  
  - Uses built-in `GITHUB_TOKEN` to commit state file

## Setup

1. Create a GitHub repo with these files.  
2. Add secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.  
3. (Optional) Adjust `cron` schedule in `scrape.yml`.  
4. (Optional) Tweak delay and thresholds in `config.json` or via constants.

## Configuration

> (We’ll add `config.json` here if/when we externalize parameters.)

## Monitoring

- Check **Actions → Scrape Moroccan Finance News** for run history and logs.  
- Refer to `docs/FAILURE_LOG.md` for known issues and their fixes.
