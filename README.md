# ActiveSG football watcher

Polls the ActiveSG programmes API every ~10 minutes and sends you a **Telegram**
message the moment a new football programme is listed (or an existing one's
registration / slots change). Runs for free on **GitHub Actions** — no server,
no laptop left on.

## How it works

ActiveSG's site is a JavaScript app: the football listings load from a hidden
JSON API. This bot calls that API directly, remembers what it saw last time (in
`state.json`), and alerts you on anything new. The first run just records a
baseline silently — alerts start from the second run onward.

---

## Setup (about 15 minutes, one-time)

### 1. Get the API endpoint  ← the only fiddly bit

1. Open your ActiveSG football page in **Chrome on a computer**.
2. Press **F12** → click the **Network** tab → click **Fetch/XHR**.
3. Press **Ctrl/Cmd-R** to reload the page.
4. Click through the requests and find the one that returns the football
   programmes as JSON (you'll see programme titles in the **Response** tab).
5. Right-click it → **Copy** → **Copy link address**. That full URL is your
   `ACTIVESG_ENDPOINT`.

Tip: the request you want will usually have `activity-ids=...` (the football
activity id) somewhere in it, matching your original page link.

### 2. Make a Telegram bot

1. In Telegram, message **@BotFather** → send `/newbot` → follow the prompts.
2. It gives you a **bot token** like `1234567890:AAExxxxxxxx`. Save it.
3. Send your new bot any message (say "hi") so it's allowed to message you.
4. Get your **chat id** (the number that tells the bot which Telegram account
   to message — yours):
   1. In Telegram, tap the **search bar** at the top and type `userinfobot`.
   2. Open the result named **@userinfobot** (it has a blue verified tick —
      ignore look-alikes).
   3. Tap **Start** (or send `/start`).
   4. It instantly replies with a line like `Id: 123456789`. That number is
      your `TELEGRAM_CHAT_ID`.

### 3. Put this on GitHub

GitHub is a free website (https://github.com) for hosting code — that's where
this bot will live and run.

1. Create a free account and a repo (a "repo" is just a project folder):
   1. Go to **https://github.com** and **Sign up** if you don't have an account
      (free; you'll verify an email).
   2. Once signed in, click the **+** at the top-right → **New repository**
      (or just go to **https://github.com/new**).
   3. Give it any name (e.g. `activesg-watcher`), choose **Private**, and click
      **Create repository**.
2. Upload these files (keep the `.github/workflows/` folder structure):
   `activesg_watcher.py`, `requirements.txt`, `.github/workflows/watch.yml`,
   `README.md`.
   - Easiest way: on the new repo's page, click **uploading an existing file**
     (or the **Add file → Upload files** button), then drag the files in. To
     keep the workflow in its folder, type `.github/workflows/` in front of the
     filename when prompted, or drag the whole folder.
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, and add three secrets:
   - `ACTIVESG_ENDPOINT` — the URL from step 1
   - `TELEGRAM_BOT_TOKEN` — from step 2
   - `TELEGRAM_CHAT_ID` — from step 2
4. Go to the **Actions** tab, enable workflows if prompted, pick **ActiveSG
   football watcher**, and click **Run workflow** once to test. Check the run
   log — the first run should say it recorded a baseline.

That's it. It now checks every ~10 minutes on its own.

---

## Testing it actually alerts

To confirm Telegram works without waiting for a real release: temporarily
delete `state.json` from the repo (or edit a value inside it) and run the
workflow manually — programmes will look "new" and you'll get pinged.

## Notes & tuning

- **Polling speed:** `watch.yml` uses every 10 min. GitHub's scheduler is
  best-effort and won't reliably go below ~5 min, and may delay during busy
  periods. 10 min is a good balance and polite to a government server.
- **Staying alive:** GitHub pauses scheduled workflows after 60 days of repo
  inactivity — but this bot commits `state.json` on changes, which counts as
  activity, so it keeps itself running.
- **Noisy "updated" alerts?** Add the offending field name to `VOLATILE_KEYS`
  in `activesg_watcher.py`.
- **Endpoint needs special headers?** Rare for public listings, but if so, copy
  them from DevTools into `EXTRA_HEADERS` in the script.
- **If the endpoint gets blocked** (bot protection): the listing API is usually
  open, but if it starts returning errors, the page may need a real browser. Ask
  and I can switch the fetch step to a headless-browser (Playwright) version.
- Browsing programmes doesn't need a login; **booking** still needs Singpass —
  the bot tells you when to pounce, you book in the app as normal.
