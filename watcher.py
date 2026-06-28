#!/usr/bin/env python3
"""
ActiveSG multi-sport programme watcher (headless-browser edition).

ActiveSG sits behind Cloudflare's "managed challenge", so plain HTTP requests
get a 403. This bot drives a real headless Chromium (Playwright): it loads each
sport's programmes page like Chrome, Cloudflare hands it the clearance cookie
(obtained once, reused across sports), and the bot captures the programme.list
API response each page makes. It then alerts via Telegram when a new programme
appears or the total listing count rises (a new season / slot opening).

Each sport keeps its own state-<sport>.json file so they never overwrite each
other. The GitHub Actions workflow commits those files back between runs.

Config via environment variables (GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN   - from @BotFather (required)
  TELEGRAM_CHAT_ID     - your chat id (required)

To add/remove a sport, edit the SPORTS list below (name + activity id).
"""

import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path

import requests  # used only for the Telegram call
from playwright.sync_api import sync_playwright

import os
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ---- Sports to watch. Add a line (name, emoji, ActiveSG activity id). ------ #
SPORTS = [
    {"name": "Football",   "emoji": "\u26bd", "id": "mlhxSk7lNvZvXXSQXD7Ea"},
    {"name": "Badminton",  "emoji": "\U0001f3f8", "id": "YLONatwvqJfikKOmB5N9U"},
    {"name": "Basketball", "emoji": "\U0001f3c0", "id": "CyIu0PE42fqR0SHD7XwMB"},
    {"name": "Tennis",     "emoji": "\U0001f3be", "id": "B0KovYOcQun1mA4VowDq0"},
]

TELEGRAM_TIMEOUT = 30
PAGE_TIMEOUT_MS = 60000
CAPTURE_DEADLINE_S = 45


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def page_url(sport: dict) -> str:
    return (
        "https://activesg.gov.sg/programmes"
        f"?keywords={urllib.parse.quote(sport['name'])}"
        f"&activity-ids={sport['id']}&show-available-only=false"
    )


def api_url(sport: dict) -> str:
    """The raw tRPC URL for a sport — used only as an in-page fallback."""
    payload = {
        "json": {
            "activityIds": [sport["id"]],
            "searchQuery": None, "minAgeFilter": None, "maxAgeFilter": None,
            "venueId": None, "postalCode": None, "sexFilter": None,
            "showAvailableOnlyFilter": False,
            "firstSessionFromDate": None, "lastSessionTillDate": None,
            "limit": 10, "direction": "forward",
        },
        "meta": {"values": {"venueId": ["undefined"]}},
    }
    q = urllib.parse.quote(json.dumps(payload, separators=(",", ":")))
    return f"https://activesg.gov.sg/api/trpc/programme.list?input={q}"


def booking_link(sport: dict) -> str:
    return f"https://activesg.gov.sg/programmes?keywords={urllib.parse.quote(sport['name'])}"


# --------------------------------------------------------------------------- #
# Fetch every sport in one browser session (Cloudflare cleared once, reused).
# --------------------------------------------------------------------------- #
def fetch_all() -> dict:
    results: dict = {}
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
                for s in SPORTS:
                    if s["id"] in resp.url:
                        try:
                            captured[s["id"]] = resp.json()
                        except Exception:
                            pass

        page.on("response", on_response)

        for s in SPORTS:
            captured.pop(s["id"], None)
            try:
                page.goto(page_url(s), wait_until="domcontentloaded",
                          timeout=PAGE_TIMEOUT_MS)
            except Exception as exc:
                print(f"WARN: {s['name']} page failed to load: {exc}",
                      file=sys.stderr)
                continue

            deadline = time.time() + CAPTURE_DEADLINE_S
            while s["id"] not in captured and time.time() < deadline:
                page.wait_for_timeout(1000)

            if s["id"] in captured:
                results[s["name"]] = captured[s["id"]]
                continue

            # Fallback: fetch this sport's API directly from inside the cleared page.
            try:
                result = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {
                          headers: {'accept': 'application/json, text/plain, */*'}
                        });
                        if (!r.ok) return {__http_error: r.status};
                        return await r.json();
                    }""",
                    api_url(s),
                )
                if isinstance(result, dict) and "__http_error" in result:
                    print(f"WARN: {s['name']} in-page fetch HTTP "
                          f"{result['__http_error']}", file=sys.stderr)
                else:
                    results[s["name"]] = result
            except Exception as exc:
                print(f"WARN: {s['name']} fallback fetch failed: {exc}",
                      file=sys.stderr)

        browser.close()

    return results


# --------------------------------------------------------------------------- #
# Parsing helpers (tuned to ActiveSG's tRPC shape, with generic fallbacks).
# --------------------------------------------------------------------------- #
def find_programmes(data: object) -> list:
    named: list = []

    def walk_named(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if (k == "programmes" and isinstance(v, list) and v
                        and all(isinstance(x, dict) for x in v)):
                    named.append(v)
                walk_named(v)
        elif isinstance(node, list):
            for item in node:
                walk_named(item)

    walk_named(data)
    if named:
        return max(named, key=len)

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
    lines = [title]

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
# Telegram + per-sport state
# --------------------------------------------------------------------------- #
def telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        fail("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url, timeout=TELEGRAM_TIMEOUT,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "disable_web_page_preview": True},
    )
    if not resp.ok:
        print(f"Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def state_path(name: str) -> Path:
    return Path(f"state-{name.lower()}.json")


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def save_state(path: Path, seen_ids, total) -> None:
    path.write_text(json.dumps(
        {"initialised": True, "seen_ids": sorted(seen_ids), "total": total},
        indent=2,
    ))


# --------------------------------------------------------------------------- #
def process_sport(sport: dict, data: object) -> None:
    name, emoji = sport["name"], sport["emoji"]
    path = state_path(name)
    link = booking_link(sport)

    programmes = find_programmes(data)
    total = find_total(data)
    if total is None:
        total = len(programmes)

    state = load_state(path)
    seen_ids = set(state.get("seen_ids", []))
    prev_total = state.get("total")
    first_run = not state.get("initialised", False)

    if not programmes and seen_ids:
        print(f"{name}: zero programmes but had some before — transient, skipping.")
        return

    current_ids = {prog_id(p) for p in programmes}
    details = {prog_id(p): p for p in programmes}
    new_ids = [i for i in current_ids if i not in seen_ids]

    if first_run:
        print(f"{name}: first run — baseline {len(current_ids)} on page, "
              f"total {total}. No alerts.")
    else:
        for i in new_ids:
            msg = f"{emoji} {name}: NEW programme listed\n\n" + describe(details[i])
            telegram(f"{msg}\n\nBook: {link}")
            print(f"{name} alert (new): {details[i].get('title')}")

        if prev_total is not None and total > prev_total and not new_ids:
            telegram(f"{emoji} {name}: listings rose {prev_total} \u2192 {total}. "
                     f"A new season or slot may have opened.\n\nCheck: {link}")
            print(f"{name} alert (total {prev_total}->{total})")

    save_state(path, seen_ids | current_ids, total)
    print(f"{name}: done. {len(current_ids)} on page ({len(new_ids)} new), "
          f"total {total}.")


def main() -> None:
    data_by_sport = fetch_all()
    if not data_by_sport:
        fail("No data captured for any sport — Cloudflare likely did not clear. "
             "See README ('If it can't get past Cloudflare').")

    for sport in SPORTS:
        data = data_by_sport.get(sport["name"])
        if data is None:
            print(f"{sport['name']}: no data this cycle, skipping.")
            continue
        process_sport(sport, data)


if __name__ == "__main__":
    main()
