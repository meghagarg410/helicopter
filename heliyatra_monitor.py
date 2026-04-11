#!/usr/bin/env python3
"""
IRCTC HeliYatra Booking Monitor - alerts on ANY text change
Monitors https://www.heliyatra.irctc.co.in/
"""

import os
import re
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


def extract_status_texts(html: str) -> dict[str, str | None]:
    """
    Extract booking status text for each destination.

    The site is Next.js SSR. The status text appears in two places:
    1. As visible HTML in <p class="mb-2 mt-0 text-xl leading-7"> tags
    2. As __html values inside <script> JSON blobs (dangerouslySetInnerHTML)

    We try method 1 first (most reliable), then fall back to regex on raw HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = {}

    for dest in DESTINATIONS:
        found = None

        # Method 1: Find the h3 heading for this destination,
        # then grab the <p> status text in the same card
        heading = soup.find("h3", string=lambda s: s and dest in s)
        if heading:
            card = heading.find_parent("div", class_=lambda c: c and "rounded-lg" in c)
            if card:
                # The status paragraph is the first <p> inside the content div
                p = card.find("p", class_=lambda c: c and "text-xl" in c)
                if p:
                    found = p.get_text(strip=True)

        # Method 2: Regex fallback — grab __html value right after dest name in script JSON
        if not found:
            # Look for the destination name followed shortly by __html":"..."
            pattern = re.compile(
                re.escape(dest) + r'.{0,500}?"__html"\s*:\s*"([^"]+)"',
                re.DOTALL
            )
            match = pattern.search(html)
            if match:
                raw = match.group(1)
                # Unescape \u003c etc and strip HTML tags
                raw = raw.encode().decode("unicode_escape")
                raw = re.sub(r"<[^>]+>", "", raw)
                found = raw.strip()

        if found and len(found) > 5:
            results[dest] = found
        else:
            results[dest] = None  # couldn't extract

    return results


def send_slack_alert(destination: str, old_text: str, new_text: str) -> None:
    if not SLACK_WEBHOOK:
        log.error("SLACK_WEBHOOK_URL is not set!")
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    payload = {
        "text": (
            f":helicopter: *HeliYatra Page Changed!*\n\n"
            f"*{destination}* section just changed!\n"
            f"Check now \u2192 {TARGET_URL}\n\n"
            f"*Was:* {old_text[:300]}\n"
            f"*Now:* {new_text[:300]}\n\n"
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


def send_startup_message(initial_sections: dict) -> None:
    if not SLACK_WEBHOOK:
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    status_lines = "\n".join(
        f"• *{dest}:* {text[:200] if text else '(could not read)'}"
        for dest, text in initial_sections.items()
    )
    payload = {
        "text": (
            f":white_check_mark: *HeliYatra Monitor Started*\n"
            f"Checking every {MIN_INTERVAL}-{MAX_INTERVAL}s \u2014 alerting on ANY text change\n"
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
    previous_sections = {dest: None for dest in DESTINATIONS}

    while any(v is None for v in previous_sections.values()):
        html = fetch_page()
        if html:
            sections = extract_status_texts(html)
            for dest, text in sections.items():
                if text and previous_sections[dest] is None:
                    previous_sections[dest] = text
                    log.info("  %-35s -> %s", dest, text[:120])
        missing = [d for d, v in previous_sections.items() if v is None]
        if missing:
            log.warning("Could not read status for: %s. Retrying in 15s...", missing)
            time.sleep(15)

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

        current_sections = extract_status_texts(html)

        for dest in DESTINATIONS:
            new = current_sections.get(dest)
            if new is None:
                log.info("  %-35s -> could not read, skipping", dest)
                continue

            old = previous_sections.get(dest, "")
            if old != new:
                log.info("  CHANGE DETECTED for '%s'!", dest)
                log.info("    Was: %s", old[:120])
                log.info("    Now: %s", new[:120])
                send_slack_alert(dest, old, new)
                previous_sections[dest] = new
            else:
                log.info("  %-35s -> no change (%s...)", dest, new[:60])


if __name__ == "__main__":
    main()
