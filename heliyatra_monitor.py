#!/usr/bin/env python3
"""
IRCTC HeliYatra Booking Monitor  (anti-block + Playwright fallback edition)
Monitors https://www.heliyatra.irctc.co.in/ and sends Slack alerts
when booking opens for Kedarnath Dham or Hemkund Sahib.

Setup:
    pip install -r requirements.txt
    playwright install chromium --with-deps

Usage:
    export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
    python heliyatra_monitor.py

Optional env vars:
    SLACK_WEBHOOK_URL        Your Slack Incoming Webhook URL  (required)
    MIN_INTERVAL_SECONDS     Min wait between checks          (default: 240 = 4 min)
    MAX_INTERVAL_SECONDS     Max wait between checks          (default: 360 = 6 min)
    USE_PLAYWRIGHT           Force Playwright mode            (default: auto)
"""

import os
import time
import random
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_URL    = "https://www.heliyatra.irctc.co.in/"
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")

MIN_INTERVAL = int(os.getenv("MIN_INTERVAL_SECONDS", "240"))   # 4 min
MAX_INTERVAL = int(os.getenv("MAX_INTERVAL_SECONDS", "360"))   # 6 min
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "auto").lower()   # auto / true / false

# Text that signals booking is CLOSED
CLOSED_PHRASES = [
    "booking is currently closed",
    "will be notified as soon as it reopens",
    "booking closed",
    "not available",
    "coming soon",
]

DESTINATIONS = ["Shri Kedarnath Dham", "Shri Hemkund Sahib"]

# Rotate across real desktop browser User-Agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

READ_DELAY_MIN   = 2
READ_DELAY_MAX   = 8
MAX_RETRIES      = 4
RETRY_BASE_DELAY = 30

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Playwright availability check ─────────────────────────────────────────────

def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False

def should_use_playwright() -> bool:
    if USE_PLAYWRIGHT == "true":
        return True
    if USE_PLAYWRIGHT == "false":
        return False
    return playwright_available()   # auto: use if installed

# ── Helpers ───────────────────────────────────────────────────────────────────

def random_headers() -> dict:
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def fetch_page_requests() -> str | None:
    """Fetch using plain requests with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(TARGET_URL, headers=random_headers(), timeout=25, allow_redirects=True)
            resp.raise_for_status()
            time.sleep(random.uniform(READ_DELAY_MIN, READ_DELAY_MAX))
            return resp.text
        except requests.RequestException as exc:
            wait = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 10)
            if attempt < MAX_RETRIES:
                log.warning("Fetch attempt %d/%d failed (%s). Retrying in %.0fs…", attempt, MAX_RETRIES, exc, wait)
                time.sleep(wait)
            else:
                log.error("All %d fetch attempts failed.", MAX_RETRIES)
    return None


def fetch_page_playwright() -> str | None:
    """Fetch using Playwright headless Chromium — handles JS-rendered pages."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="en-IN",
                extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            )
            page = ctx.new_page()
            try:
                page.goto(TARGET_URL, timeout=30_000, wait_until="networkidle")
                # Wait a moment for any late JS renders
                page.wait_for_timeout(random.randint(2000, 4000))
                html = page.content()
            except PWTimeout:
                log.warning("Playwright page load timed out.")
                html = None
            finally:
                browser.close()
            return html
    except Exception as exc:
        log.error("Playwright fetch failed: %s", exc)
        return None


def fetch_page() -> str | None:
    if should_use_playwright():
        log.debug("Using Playwright to fetch page.")
        return fetch_page_playwright()
    log.debug("Using requests to fetch page.")
    return fetch_page_requests()


def check_booking_status(html: str) -> dict[str, bool]:
    """
    Parse the page and return {destination: is_open}.
    Strategy 1: look for destination heading, check nearby text.
    Strategy 2: full-page fallback scan for closed phrases.
    """
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(separator=" ", strip=True).lower()
    status = {}

    for dest in DESTINATIONS:
        # Strategy 1: find the section for this destination
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
            # Strategy 2: fallback — if destination name appears anywhere on page
            # check if the full page contains any closed phrase nearby
            log.warning("No heading found for '%s' — falling back to full-page scan.", dest)
            dest_found = dest.lower() in full_text
            if dest_found:
                closed = any(phrase in full_text for phrase in CLOSED_PHRASES)
                status[dest] = not closed
            else:
                log.warning("'%s' not found on page at all — assuming closed.", dest)
                status[dest] = False

    return status


def send_slack_alert(destination: str) -> None:
    if not SLACK_WEBHOOK:
        log.error("SLACK_WEBHOOK_URL is not set — cannot send alert!")
        return
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    payload = {
        "text": (
            f":helicopter: *HeliYatra Booking OPEN!*\n\n"
            f"*{destination}* helicopter booking has just opened on IRCTC HeliYatra.\n"
            f"Book now \u2192 {TARGET_URL}\n\n"
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
    mode = "Playwright (JS-capable)" if should_use_playwright() else "requests (HTML-only)"
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    payload = {
        "text": (
            f":white_check_mark: *HeliYatra Monitor Started*\n"
            f"Watching: {', '.join(DESTINATIONS)}\n"
            f"Checking every {MIN_INTERVAL//60}\u2013{MAX_INTERVAL//60} min | Mode: {mode}\n"
            f"_Started at {now}_"
        )
    }
    try:
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Slack startup message sent.")
    except requests.RequestException as exc:
        log.error("Slack startup message failed: %s", exc)


def next_interval() -> float:
    base   = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
    jitter = random.uniform(-15, 15)
    return max(60, base + jitter)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    if not SLACK_WEBHOOK:
        log.error(
            "SLACK_WEBHOOK_URL is not set.\n"
            "  export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ"
        )
        raise SystemExit(1)

    pw = should_use_playwright()
    log.info("=" * 50)
    log.info("  IRCTC HeliYatra Monitor  (anti-block + Playwright)")
    log.info("=" * 50)
    log.info("URL         : %s", TARGET_URL)
    log.info("Interval    : %d-%d s (random + jitter)", MIN_INTERVAL, MAX_INTERVAL)
    log.info("Destinations: %s", ", ".join(DESTINATIONS))
    log.info("Fetch mode  : %s", "Playwright (headless Chromium)" if pw else "requests")
    log.info("UA pool     : %d user-agents", len(USER_AGENTS))

    send_startup_message()

    alerted: set[str] = set()

    while True:
        log.info("Checking booking status...")
        html = fetch_page()

        if html:
            status = check_booking_status(html)
            for dest, is_open in status.items():
                label = "OPEN ✅" if is_open else "closed"
                log.info("  %-35s -> %s", dest, label)

                if is_open and dest not in alerted:
                    log.info("  '%s' is now OPEN! Sending Slack alert...", dest)
                    send_slack_alert(dest)
                    alerted.add(dest)
                elif not is_open and dest in alerted:
                    log.info("  '%s' closed again; resetting alert state.", dest)
                    alerted.discard(dest)
        else:
            log.warning("Page fetch failed entirely; will retry next cycle.")

        sleep_secs = next_interval()
        log.info("Next check in %.0f s (%.1f min)...\n", sleep_secs, sleep_secs / 60)
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
