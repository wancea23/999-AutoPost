<p align="center">
  <img src="assets/logo.png" alt="999 AutoPost Logo" width="120"/>
</p>

<h1 align="center">999 AutoPost</h1>

<p align="center">
  Automatically scrape and repost your listings on <a href="https://999.md">999.md</a> — desktop GUI with multi-account support.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/playwright-chromium-green?style=flat-square"/>
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey?style=flat-square"/>
</p>

---

## Features

- **Multi-account** — add as many 999.md accounts as you need; switch between them instantly
- **Listing thumbnails** — browse your ads with photos, title, price, and ID at a glance
- **One-click repost** — select listings and repost them all automatically
- **Smart scraping** — deduplicates listings by title, filters junk URLs, anchors price to the listing header so related-listing widgets don't interfere
- **In-app console** — toggle a live log panel to see exactly what the scraper is doing
- **Cached listings** — results saved per account so reopening is instant; refresh only when you need fresh data
- Credentials stored locally and never committed to git

---

## Requirements

- **Python 3.10+** — [python.org/downloads](https://www.python.org/downloads/)
- A **999.md** account (Simpals ID)

---

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Playwright's Chromium browser (one-time, ~150 MB)
python -m playwright install chromium
```

---

## Getting the Desktop App

Run the build script once to create a **Desktop shortcut** that launches the app silently (no console window):

```bash
python build.py
```

After it completes, a **999AutoPost** shortcut appears on your Desktop. Double-click it to open the app. That's it — no `.exe` packaging needed.

> The shortcut uses `pythonw.exe` (Python's no-console launcher), so the app opens like a normal desktop program.

---

## How to Use

1. Click **＋ Add Account** and enter your 999.md username/password
2. Click an account in the sidebar to load its cached listings
3. Press **⟳ Refresh** to scan 999.md and pull the latest listings (~1–2 min)
4. Check the listings you want to repost (or press **Select All**)
5. Click **Repost Selected** — the browser opens and reposts each one automatically

**Console panel:** click **Console ▼** in the top bar to open the live log and see login, scraping, and repost progress in real time.

---

## Running Directly (no shortcut)

```bash
python app.py
```

---

## Data Files

All user data is stored in `data/` (gitignored — never committed):

| Path | Contents |
|------|----------|
| `data/accounts.json` | Saved account credentials |
| `data/<username>/listings.json` | Per-account scraped listing cache |
| `data/images/` | Downloaded listing photos |
| `data/listings.json` | Last CLI scrape output |
| `data/repost_log.json` | Repost attempts log (status + new URL) |

---

## CLI Usage

For scripted/automated use without the GUI:

```powershell
# Windows (PowerShell)
$env:NNN_EMAIL    = "your_username"
$env:NNN_PASSWORD = "yourpassword"

python main.py scrape   # scrape active listings → data/listings.json
python main.py repost   # repost all scraped listings
```

| Variable | Default | Description |
|----------|---------|-------------|
| `NNN_EMAIL` | *(required)* | Your Simpals ID username or email |
| `NNN_PASSWORD` | *(required)* | Your Simpals ID password |
| `HEADLESS` | `false` | Set to `true` to run the browser invisibly |

---

## Notes

- `data/` is gitignored — credentials and scraped data are never committed
- A 2-second delay is added between reposts to avoid rate-limiting
- Photos are cached locally; refreshing won't re-download existing images
- The browser runs visibly by default so you can monitor progress
- 999.md uses Simpals ID for authentication (`v2.simpalsid.com`)
