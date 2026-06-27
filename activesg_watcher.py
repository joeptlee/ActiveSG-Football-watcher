#!/usr/bin/env python3
"""
ActiveSG football programme watcher.

Polls the ActiveSG programmes JSON API, detects newly listed programmes (and
programmes whose details change, e.g. registration opening or slots appearing),
and pushes a Telegram alert. Designed to run on GitHub Actions on a cron
schedule; state is persisted in state.json which the workflow commits back to
the repo between runs.

All config comes from environment variables (set as GitHub Actions secrets):
  ACTIVESG_ENDPOINT   - the JSON API URL your browser actually calls (required)
  TELEGRAM_BOT_TOKEN  - from @BotFather (required)
  TELEGRAM_CHAT_ID    - your chat id (required)
  ACTIVESG_BOOKING_URL- optional human link included in alerts
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import requests                       # used for the Telegram call
from curl_cffi import requests as cffi  # browser-impersonating fetch (beats WAF 403s)

ENDPOINT = os.environ.get("ACTIVESG_ENDPOINT", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BOOKING_URL = os.environ.get(
    "ACTIVESG_BOOKING_URL",
    "https://activesg.gov.sg/programmes?keywords=Football",
).strip()

# Optional extra request headers, if you find the endpoint needs them.
# Copy them from DevTools (rare for public listings). Example:
#   EXTRA_HEADERS = {"x-some-token": "abc123"}
EXTRA_HEADERS: dict[str, str] = {}

STATE_FILE = Path("state.json")
TIMEOUT = 30

# Keys whose values change every response and would cause false "updated"
# alerts. Add more here if you notice noisy alerts.
VOLATILE_KEYS = {
    "updatedat", "updated_at", "createdat", "created_at", "timestamp",
    "servertime", "server_time", "_ts", "lastmodified", "last_modified",
    "etag", "requestid", "request_id",
}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch() -> object:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-SG,en;q=0.9",
        "Referer": "https://activesg.gov.sg/",
        "Origin": "https://activesg.gov.sg",
    }
    headers.update(EXTRA_HEADERS)
    try:
        # impersonate makes the TLS handshake + headers look like real Chrome,
        # which is what defeats most "403 Forbidden" bot blocks.
        resp = cffi.get(
            ENDPOINT, headers=headers, timeout=TIMEOUT, impersonate="chrome"
        )
    except Exception as exc:  # network/DNS/TLS errors
        fail(f"Request to the endpoint failed: {exc}")

    if resp.status_code == 403:
        fail(
            "403 Forbidden — the server refused the request even with browser "
            "impersonation. This usually means ActiveSG is blocking by "
            "region/IP (GitHub's runners are outside Singapore). See the "
            "'If you get a 403' section in the README for options."
        )
    if resp.status_code == 404:
        fail(
            "404 Not Found — the endpoint URL is wrong. Re-grab it from the "
            "Network tab (the request that returns JSON, not the page HTML)."
        )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        fail(
            "Endpoint did not return JSON. Re-check the URL from the Network "
            "tab — it should be the request that returns programme data as "
            "JSON, not the page HTML."
        )


def find_programmes(data: object) -> list:
    """Return the most likely list of programme objects in the JSON.

    Recursively finds the largest array whose elements are all dicts, so it
    works without knowing ActiveSG's exact schema.
    """
    best: list = []

    def walk(node: object) -> None:
        nonlocal best
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                if len(node) > len(best):
                    best = node
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(data)
    return best


def prog_id(p: dict) -> str:
    """A stable identifier for one programme."""
    for key in ("id", "uuid", "programmeId", "programme_id", "slug", "code"):
        val = p.get(key)
        if val not in (None, ""):
            return str(val)
    return hashlib.sha1(
        json.dumps(p, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]


def signature(p: dict) -> str:
    """Fingerprint of a programme, ignoring volatile fields."""
    filtered = {k: v for k, v in p.items() if k.lower() not in VOLATILE_KEYS}
    return hashlib.sha1(
        json.dumps(filtered, sort_keys=True, default=str).encode()
    ).hexdigest()


def guess(p: dict, *needles: str) -> str | None:
    for k, v in p.items():
        if any(n in k.lower() for n in needles) and isinstance(v, (str, int, float)):
            return str(v)
    return None


def describe(p: dict) -> str:
    title = guess(p, "title", "name") or "(untitled programme)"
    lines = [f"⚽ {title}"]
    venue = guess(p, "venue", "location", "centre", "center", "facility")
    if venue:
        lines.append(f"Where: {venue}")
    when = guess(p, "regstart", "registration", "startdate", "start_date", "date")
    if when:
        lines.append(f"When: {when}")
    avail = guess(p, "avail", "vacanc", "slot", "capacity", "quota", "status")
    if avail:
        lines.append(f"Slots/Status: {avail}")
    return "\n".join(lines)


def telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        fail("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        timeout=TIMEOUT,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
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


def save_state(seen: dict) -> None:
    STATE_FILE.write_text(
        json.dumps({"initialised": True, "seen": seen}, indent=2, sort_keys=True)
    )


def main() -> None:
    if not ENDPOINT:
        fail("ACTIVESG_ENDPOINT is empty. See README for how to find it.")

    data = fetch()
    programmes = find_programmes(data)

    state = load_state()
    seen = state.get("seen", {})            # id -> signature from last run
    first_run = not state.get("initialised", False)

    # Guard against a transient empty response wiping the baseline and then
    # flooding you with "new" alerts when programmes reappear.
    if not programmes and seen:
        print("Zero programmes returned but we had some before — treating as a "
              "transient blip, keeping baseline, skipping this run.")
        return

    current = {prog_id(p): signature(p) for p in programmes}
    details = {prog_id(p): p for p in programmes}

    new_ids = [i for i in current if i not in seen]
    changed_ids = [i for i in current if i in seen and seen[i] != current[i]]

    if first_run:
        print(f"First run — recorded {len(current)} programmes as baseline. "
              "No alerts sent on the first run.")
    else:
        for i in new_ids:
            msg = "NEW football programme listed:\n\n" + describe(details[i])
            telegram(f"{msg}\n\nBook now: {BOOKING_URL}")
            print("Alert (new):\n" + msg + "\n")
        for i in changed_ids:
            msg = ("Football programme updated (registration or slots may have "
                   "changed):\n\n" + describe(details[i]))
            telegram(f"{msg}\n\nCheck: {BOOKING_URL}")
            print("Alert (changed):\n" + msg + "\n")

    save_state(current)
    print(f"Done. Tracking {len(current)} programmes "
          f"({len(new_ids)} new, {len(changed_ids)} changed).")


if __name__ == "__main__":
    main()
