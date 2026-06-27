#!/usr/bin/env python3
"""
ActiveSG football programme watcher (headless-browser edition).

ActiveSG sits behind Cloudflare's "managed challenge", so plain HTTP requests
get a 403. This version drives a real headless Chromium (via Playwright): it
loads the programmes page like a normal browser, Cloudflare hands it the
clearance cookie, and we capture the programme.list API response the page
makes. We then alert via Telegram when a new programme appears or the total
programme count rises (a new season/slot opening).

State is persisted in state.json, which the GitHub Actions workflow commits
back to the repo between runs.

Config via environment variables (GitHub Actions secrets / vars):
  TELEGRAM_BOT_TOKEN   - from @BotFather (required)
  TELEGRAM_CHAT_ID     - your chat id (required)
  ACTIVESG_PAGE_URL    - the football programmes page to load (optional;
                         a sensible default is built in)
  ACTIVESG_ENDPOINT    - the raw JSON API URL (optional; used only as a
                         fallback if page interception doesn't catch it)
  ACTIVESG_BOOKING_URL - optional human link included in alerts
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests  # used only for the Telegram call
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

PAGE_URL = os.environ.get(
    "ACTIVESG_PAGE_URL",
    "https://activesg.gov.sg/programmes"
    "?keywords=Football&activity-ids=mlhxSk7lNvZvXXSQXD7Ea"
    "&show-available-only=false",
).strip()

# Optional raw API URL, only used as a fallback if we don't intercept the
# page's own call. (This is the long /api/trpc/programme.list?input=... URL.)
ENDPOINT = os.environ.get("ACTIVESG_ENDPOINT", "").strip()

BOOKING_URL = os.environ.get(
    "ACTIVESG_BOOKING_URL",
    "https://activesg.gov.sg/programmes?keywords=Football",
).strip()

STATE_FILE = Path("state.json")
TELEGRAM_TIMEOUT = 30
PAGE_TIMEOUT_MS = 60000
CAPTURE_DEADLINE_S = 60


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Fetch: drive a real browser, clear Cloudflare, capture the API response.
# --------------------------------------------------------------------------- #
def fetch() -> object:
    captured: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-SG",
            timezone_id="Asia/Singapore",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        def on_response(resp):
            if "/api/trpc/programme.list" in resp.url and resp.status == 200:
                try:
                    captured["data"] = resp.json()
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            browser.close()
            fail(f"Failed to load the page (Cloudflare or network): {exc}")

        # Wait for Cloudflare to clear and the app to call the API.
        deadline = time.time() + CAPTURE_DEADLINE_S
        while "data" not in captured and time.time() < deadline:
            page.wait_for_timeout(1000)

        # Fallback: once the challenge is cleared, fetch the endpoint ourselves
        # from inside the page (same-origin, carries the clearance cookie).
        if "data" not in captured and ENDPOINT:
            try:
                result = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {
                          headers: {'accept': 'application/json, text/plain, */*'}
                        });
                        if (!r.ok) return {__http_error: r.status};
                        return await r.json();
                    }""",
                    ENDPOINT,
                )
                if isinstance(result, dict) and "__http_error" in result:
                    browser.close()
                    fail(
                        f"In-page fetch returned HTTP {result['__http_error']}. "
                        "Cloudflare likely did not clear on this IP (common on "
                        "datacenter IPs like GitHub's). See README."
                    )
                captured["data"] = result
            except Exception as exc:
                browser.close()
                fail(f"In-page fallback fetch failed: {exc}")

        browser.close()

    if "data" not in captured:
        fail(
            "Could not retrieve programme data — the Cloudflare challenge did "
            "not clear (common on datacenter IPs). If this persists on GitHub, "
            "we add a residential proxy. See the README."
        )
    return captured["data"]


# --------------------------------------------------------------------------- #
# Parsing helpers (tuned to ActiveSG's tRPC shape, with generic fallbacks).
# --------------------------------------------------------------------------- #
def find_programmes(data: object) -> list:
    """Find the list of programmes. Prefer an explicit 'programmes' key
    (ActiveSG's shape) so we don't accidentally pick a sessions array."""
    named: list = []

    def walk_named(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if (
                    k == "programmes"
                    and isinstance(v, list)
                    and v
                    and all(isinstance(x, dict) for x in v)
                ):
                    named.append(v)
                walk_named(v)
        elif isinstance(node, list):
            for item in node:
                walk_named(item)

    walk_named(data)
    if named:
        return max(named, key=len)

    # Fallback: largest array of dicts anywhere.
    best: list = []

    def walk(node):
        nonlocal best
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node) and len(node) > len(best):
                best = node
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(data)
    return best


def find_total(data: object):
    """Return meta.totalCount if present (counts ALL matching programmes,
    not just the first page)."""
    totals: list = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in ("totalcount", "total") and isinstance(v, int):
                    totals.append(v)
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(data)
    return max(totals) if totals else None


def prog_id(p: dict) -> str:
    for key in ("id", "uuid", "programmeId", "slug", "code"):
        val = p.get(key)
        if val not in (None, ""):
            return str(val)
    return hashlib.sha1(
        json.dumps(p, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]


def guess(p: dict, *needles: str):
    for k, v in p.items():
        if any(n in k.lower() for n in needles) and isinstance(v, (str, int, float)):
            return str(v)
    return None


def describe(p: dict) -> str:
    title = p.get("title") or guess(p, "title", "name") or "(untitled programme)"
    lines = [f"⚽ {title}"]

    venue = None
    if isinstance(p.get("venue"), dict):
        venue = p["venue"].get("name")
    venue = venue or guess(p, "venue", "location")
    if venue:
        lines.append(f"Where: {venue}")

    sessions = p.get("sessions")
    if isinstance(sessions, list) and sessions:
        start = sessions[0].get("startDateTime")
        if isinstance(start, str):
            lines.append(f"Starts: {start[:10]}")
        lines.append(f"Sessions: {len(sessions)}")

    cap = p.get("maxCapacity")
    pc = p.get("participantCount")
    if isinstance(cap, int) and isinstance(pc, int):
        lines.append(f"Spots: {max(cap - pc, 0)} of {cap} free")
    elif isinstance(cap, int):
        lines.append(f"Capacity: {cap}")

    rate = p.get("minRate")
    if rate:
        try:
            lines.append(f"From: S${int(rate) / 100:.2f}")
        except (ValueError, TypeError):
            pass

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Telegram + state
# --------------------------------------------------------------------------- #
def telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        fail("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        timeout=TELEGRAM_TIMEOUT,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "disable_web_page_preview": True},
    )
    if not resp.ok:
        print(f"Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def save_state(seen_ids, total) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {"initialised": True, "seen_ids": sorted(seen_ids), "total": total},
            indent=2,
        )
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    data = fetch()
    programmes = find_programmes(data)
    total = find_total(data)
    if total is None:
        total = len(programmes)

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    prev_total = state.get("total")
    first_run = not state.get("initialised", False)

    # Guard against a transient empty/blocked response wiping the baseline.
    if not programmes and seen_ids:
        print("Zero programmes returned but we had some before — treating as a "
              "transient blip, keeping baseline, skipping this run.")
        return

    current_ids = {prog_id(p) for p in programmes}
    details = {prog_id(p): p for p in programmes}
    new_ids = [i for i in current_ids if i not in seen_ids]

    if first_run:
        print(f"First run — baseline recorded: {len(current_ids)} programmes "
              f"on the first page, total={total}. No alerts on first run.")
    else:
        for i in new_ids:
            msg = "NEW football programme listed:\n\n" + describe(details[i])
            telegram(f"{msg}\n\nBook: {BOOKING_URL}")
            print("Alert (new programme):\n" + msg + "\n")

        # totalCount rose but the new items are beyond the first page.
        if prev_total is not None and total > prev_total and not new_ids:
            telegram(
                f"ActiveSG football: listings rose {prev_total} → {total}. "
                f"A new season or slot may have opened.\n\nCheck: {BOOKING_URL}"
            )
            print(f"Alert (total rose {prev_total} -> {total})")

    save_state(seen_ids | current_ids, total)
    print(f"Done. First page: {len(current_ids)} programmes "
          f"({len(new_ids)} new). Total listings: {total}.")


if __name__ == "__main__":
    main()
