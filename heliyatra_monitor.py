#!/usr/bin/env python3
"""
IRCTC HeliYatra Booking Monitor - alerts on ANY text change
Monitors https://www.heliyatra.irctc.co.in/
"""

import os
import time
import random
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime

TARGET_URL    = "https://www.heliyatra.irctc.co.in/"
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")
MIN_INTERVAL  = int(os.getenv("MIN_INTERVAL_SECONDS", "60"))
MAX_INTERVAL  = int(os.getenv("MAX_INTERVAL_SECONDS", "90"))

DESTINATIONS = ["Shri Hemkund Sahib", "Shri Kedarnath Dham"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

MAX_RETRIES      = 4
RETRY_BASE_DELAY = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }


def fetch_page() -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(TARGET_URL, headers=random_headers(), timeout=25, allow_redirects=True)
            resp.raise_for_status()
            time.sleep(random.uniform(2, 5))
            return resp.text
        except requests.RequestException as exc:
            wait = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 10)
            if attempt < MAX_RETRIES:
                log.warning("Fetch attempt %d/%d failed (%s). Retrying in %.0fs...", attempt, MAX_RETRIES, exc, wait)
                time.sleep(wait)
            else:
                log.error("All %d fetch attempts failed.", MAX_RETRIES)
    return None


def extract_section_text(html: str) -> dict[str, str]:
    """Extract the text snippet for each destination section."""
    soup = BeautifulSoup(html, "html.parser")
    sections = {}

    for dest in DESTINATIONS:
        heading = soup.find(
            lambda tag: tag.name in ("h2", "h3", "h4", "h5", "p", "span", "div")
            and dest.lower() in tag.get_text(strip=True).lower()
        )
        if heading is not None:
            container = heading.find_parent(["div", "section", "article", "li"]) or heading
            sections[dest] = container.get_text(separator=" ", strip=True)
        else:
            # fallback: grab full page text
            sections[dest] = soup.get_text(separator=" ", strip=True)

    return sections


def send_slack_alert(destination: str, old_text: str, new_text: str) -> None:
    if not SLACK_WEBHOOK:
        log.error("SLACK_WEBHOOK_URL is not set!")
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    payload = {
        "text": (
            f":helicopter: *HeliYatra Page Changed!*\n\n"
            f"*{destination}* section just changed — booking may be opening!\n"
            f"Check now -> {TARGET_URL}\n\n"
            f"*Was:* {old_text[:200]}\n"
            f"*Now:* {new_text[:200]}\n\n"
            f"_Detected at {now}_"
        )
    }
    try:
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Slack alert sent for '%s'.", destination)
        else:
            log.warning("Slack returned %d: %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        log.error("Slack alert failed: %s", exc)


def send_startup_message(initial_sections: dict[str, str]) -> None:
    if not SLACK_WEBHOOK:
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    status_lines = "\n".join(f"• *{dest}:* {text[:150]}" for dest, text in initial_sections.items())
    payload = {
        "text": (
            f":white_check_mark: *HeliYatra Monitor Started*\n"
            f"Checking every {MIN_INTERVAL}-{MAX_INTERVAL}s — will alert on ANY text change\n"
            f"_Started at {now}_\n\n"
            f"*Current status:*\n{status_lines}"
        )
    }
    try:
        requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        log.info("Slack startup message sent.")
    except requests.RequestException as exc:
        log.error("Slack startup message failed: %s", exc)


def next_interval() -> float:
    return max(60, random.uniform(MIN_INTERVAL, MAX_INTERVAL) + random.uniform(-10, 10))


def main() -> None:
    if not SLACK_WEBHOOK:
        log.error("SLACK_WEBHOOK_URL is not set.")
        raise SystemExit(1)

    log.info("=" * 46)
    log.info("  IRCTC HeliYatra Monitor  (change detector)")
    log.info("=" * 46)
    log.info("URL      : %s", TARGET_URL)
    log.info("Interval : %d-%d s", MIN_INTERVAL, MAX_INTERVAL)

    # Get initial state
    log.info("Fetching initial page state...")
    while True:
        html = fetch_page()
        if html:
            break
        log.warning("Could not fetch initial page, retrying in 30s...")
        time.sleep(30)

    previous_sections = extract_section_text(html)
    for dest, text in previous_sections.items():
        log.info("  %-35s -> %s", dest, text[:80])

    send_startup_message(previous_sections)

    while True:
        sleep_secs = next_interval()
        log.info("Next check in %.0f s...\n", sleep_secs)
        time.sleep(sleep_secs)

        log.info("Checking for changes...")
        html = fetch_page()

        if not html:
            log.warning("Page fetch failed; will retry next cycle.")
            continue

        current_sections = extract_section_text(html)

        for dest in DESTINATIONS:
            old = previous_sections.get(dest, "")
            new = current_sections.get(dest, "")

            if old != new:
                log.info("  CHANGE DETECTED for '%s'!", dest)
                log.info("  Was: %s", old[:100])
                log.info("  Now: %s", new[:100])
                send_slack_alert(dest, old, new)
                previous_sections[dest] = new  # update baseline
            else:
                log.info("  %-35s -> no change", dest)


if __name__ == "__main__":
    main()
