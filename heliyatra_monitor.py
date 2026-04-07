#!/usr/bin/env python3
"""
IRCTC HeliYatra Booking Monitor (lightweight - no Playwright needed)
Monitors https://www.heliyatra.irctc.co.in/ and sends Slack alerts
when booking opens for Kedarnath Dham or Hemkund Sahib.

Setup:
    pip install requests beautifulsoup4

Usage:
    export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
    python heliyatra_monitor.py
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

CLOSED_PHRASES = [
    "booking is currently closed",
    "will be notified as soon as it reopens",
    "booking closed",
    "not available",
    "coming soon",
]

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


def check_booking_status(html: str) -> dict[str, bool]:
    soup = BeautifulSoup(html, "html.parser")
    status = {}

    for dest in DESTINATIONS:
        heading = soup.find(
            lambda tag: tag.name in ("h2", "h3", "h4", "h5", "p", "span", "div")
            and dest.lower() in tag.get_text(strip=True).lower()
        )

        if heading is not None:
            container = heading.find_parent(["div", "section", "article", "li"]) or heading
            section_text = container.get_text(separator=" ", strip=True).lower()
            closed = any(phrase in section_text for phrase in CLOSED_PHRASES)
            status[dest] = not closed
        else:
            log.warning("No heading found for '%s' -- falling back to full-page scan.", dest)
            full_text = soup.get_text(separator=" ", strip=True).lower()
            if dest.lower() in full_text:
                closed = any(phrase in full_text for phrase in CLOSED_PHRASES)
                status[dest] = not closed
            else:
                log.warning("'%s' not found on page at all -- assuming closed.", dest)
                status[dest] = False

    return status


def send_slack_alert(destination: str) -> None:
    if not SLACK_WEBHOOK:
        log.error("SLACK_WEBHOOK_URL is not set!")
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    payload = {
        "text": (
            f":helicopter: *HeliYatra Booking OPEN!*\n\n"
            f"*{destination}* helicopter booking has just opened on IRCTC HeliYatra.\n"
            f"Book now -> {TARGET_URL}\n\n"
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


def send_startup_message() -> None:
    if not SLACK_WEBHOOK:
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    payload = {
        "text": (
            f":white_check_mark: *HeliYatra Monitor Started*\n"
            f"Watching: {', '.join(DESTINATIONS)}\n"
            f"Checking every {MIN_INTERVAL//60}-{MAX_INTERVAL//60} min\n"
            f"_Started at {now}_"
        )
    }
    try:
        requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        log.info("Slack startup message sent.")
    except requests.RequestException as exc:
        log.error("Slack startup message failed: %s", exc)


def next_interval() -> float:
    return max(60, random.uniform(MIN_INTERVAL, MAX_INTERVAL) + random.uniform(-15, 15))


def main() -> None:
    if not SLACK_WEBHOOK:
        log.error("SLACK_WEBHOOK_URL is not set. Export it before running.")
        raise SystemExit(1)

    log.info("=" * 46)
    log.info("  IRCTC HeliYatra Monitor  (lightweight)")
    log.info("=" * 46)
    log.info("URL         : %s", TARGET_URL)
    log.info("Interval    : %d-%d s", MIN_INTERVAL, MAX_INTERVAL)
    log.info("Destinations: %s", ", ".join(DESTINATIONS))

    send_startup_message()
    alerted: set[str] = set()

    while True:
        log.info("Checking booking status...")
        html = fetch_page()

        if html:
            status = check_booking_status(html)
            for dest, is_open in status.items():
                log.info("  %-35s -> %s", dest, "OPEN" if is_open else "closed")
                if is_open and dest not in alerted:
                    log.info("  '%s' is now OPEN! Sending Slack alert...", dest)
                    send_slack_alert(dest)
                    alerted.add(dest)
                elif not is_open and dest in alerted:
                    log.info("  '%s' closed again; resetting.", dest)
                    alerted.discard(dest)
        else:
            log.warning("Page fetch failed; will retry next cycle.")

        sleep_secs = next_interval()
        log.info("Next check in %.0f s (%.1f min)...\n", sleep_secs, sleep_secs / 60)
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
