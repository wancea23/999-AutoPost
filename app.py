"""
999 AutoPost — GUI
Run: python app.py
"""
import asyncio
from datetime import datetime, timezone
from difflib import SequenceMatcher
import io
import json
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

import main as core
from playwright.async_api import async_playwright
import httpx


# ── Paths ─────────────────────────────────────────────────────────────────────
# When bundled with PyInstaller, __file__ is inside a temp dir; assets live
# next to the .exe in the _MEIPASS bundle or beside the script when running raw.
if getattr(sys, "frozen", False):
    _BASE = Path(sys._MEIPASS)
else:
    _BASE = Path(__file__).parent

ASSETS_DIR = _BASE / "assets"

DATA_DIR = _BASE / "data"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Keep core (main.py) module globals aligned to the same data directory.
# main.py initialises these relative to CWD at import time; we override them
# here before any core functions are called.
core.DATA_DIR = DATA_DIR
core.IMAGES_DIR = DATA_DIR / "images"
core.LISTINGS_FILE = DATA_DIR / "listings.json"
core.REPOST_LOG_FILE = DATA_DIR / "repost_log.json"
core.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# ── Credential helpers ────────────────────────────────────────────────────────
def load_accounts() -> list:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_accounts(accounts: list):
    ACCOUNTS_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Blacklist helpers ─────────────────────────────────────────────────────────
def load_blacklist(username: str) -> set[str]:
    path = DATA_DIR / username / "blacklist.json"
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_blacklist(username: str, urls: set[str]):
    path = DATA_DIR / username / "blacklist.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(urls), ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ── Background async helpers ──────────────────────────────────────────────────
def _listing_id(data: dict) -> int:
    """Return the numeric listing ID from its URL — higher = more recent repost."""
    last = (data.get("url") or "").rstrip("/").split("/")[-1]
    return int(last) if last.isdigit() else 0


def _fuzzy_dedup(raw: list[dict], threshold: float = 0.9) -> list[dict]:
    """
    Deduplicate listings by title similarity.
    When two listings have ≥ threshold similarity, keep the most recent one
    (highest numeric listing ID = latest repost).
    """
    result: list[dict] = []
    for data in raw:
        title = (data.get("title") or "").strip().lower()
        if not title:
            continue
        match_idx = None
        for i, kept in enumerate(result):
            kept_t = (kept.get("title") or "").strip().lower()
            if SequenceMatcher(None, title, kept_t).ratio() >= threshold:
                match_idx = i
                break
        if match_idx is None:
            result.append(data)
        elif _listing_id(data) > _listing_id(result[match_idx]):
            # This is a newer repost of the same item — replace the older one
            result[match_idx] = data
    return result


async def scrape_account(acc: dict, max_active: int = 30,
                         max_inactive: int = 15) -> list:
    """
    Login as acc, collect up to max_active active listing URLs and up to
    max_inactive inactive listing URLs, scrape them all, then fuzzy-deduplicate
    by title (≥90% similarity → same item; keep the most recent repost).
    """
    core.EMAIL = acc["username"]
    core.PASSWORD = acc["password"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await core.login(page)

        _BAD_SEGMENTS = {"profile", "cabinet", "category", "list", "add",
                         "search", "favorites", "history", "settings", "items"}

        ad_urls: list[str] = []

        cabinet_url = f"{core.BASE_URL}/ro/cabinet/items/{acc['username']}"
        await page.goto(cabinet_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # Dismiss IntroJS tour overlay if present (blocks all clicks)
        await page.evaluate("""() => {
            document.querySelectorAll(
                '.introjs-overlay, .introjs-tour, .introjs-helperLayer'
            ).forEach(el => el.remove());
        }""")

        for tab, tab_testid in (
            ("active",   "ads-cabinet-tab-active"),
            ("inactive", "ads-cabinet-tab-not-active"),
        ):
            tab_limit = max_active if tab == "active" else max_inactive
            tab_count = 0

            print(f"  Collecting {tab} listings (up to {tab_limit})…")
            # For active tab the page already shows active listings by default,
            # so skip the click; for other tabs, click the tab button.
            if tab != "active":
                try:
                    btn = page.locator(f"[data-testid='{tab_testid}']").first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(1000)
                    else:
                        print(f"  [WARN] Tab button '{tab_testid}' not visible, skipping")
                        continue
                except Exception as e:
                    print(f"  [WARN] Could not click tab '{tab_testid}': {e}")
                    continue

            print(f"  Tab URL: {page.url}")

            while tab_count < tab_limit:
                found_on_page = 0
                # Use specific selector for listing title links; fall back to all links
                listing_links = await page.query_selector_all(
                    "a[class*='advert__title']"
                )
                if not listing_links:
                    listing_links = await page.query_selector_all("a[href]")
                print(f"  Found {len(listing_links)} candidate links on page")
                for a in listing_links:
                    href = (await a.get_attribute("href")) or ""
                    if not href.startswith("http"):
                        href = core.BASE_URL + href
                    clean = href.split("?")[0].rstrip("/")
                    parts = clean.split("/")
                    last = parts[-1]
                    if (last.isdigit()
                            and len(parts) >= 2
                            and parts[-2] not in _BAD_SEGMENTS
                            and clean not in ad_urls):
                        ad_urls.append(clean)
                        tab_count += 1
                        found_on_page += 1
                        if tab_count >= tab_limit:
                            break

                if tab_count >= tab_limit or found_on_page == 0:
                    break

                # Next page
                try:
                    nxt = page.locator(
                        "a[data-testid='pagination-next'], a[aria-label='Next']"
                    ).first
                    if await nxt.is_visible(timeout=2000):
                        await nxt.click()
                        await page.wait_for_timeout(2000)
                    else:
                        break
                except Exception:
                    break

            print(f"  Collected {tab_count} {tab} URLs")

        # Scrape every URL (no early exit — we need all data for fuzzy dedup)
        raw: list[dict] = []
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for url in ad_urls:
                try:
                    data = await core.scrape_listing_detail(page, url, client)
                    if (data.get("title") or "").strip():
                        raw.append(data)
                except Exception as e:
                    print(f"  [WARN] scrape {url}: {e}")

        await browser.close()

        # Fuzzy-deduplicate: same item ≥90% title similarity → keep newest repost
        listings = _fuzzy_dedup(raw, threshold=0.9)
        print(f"  {len(raw)} scraped → {len(listings)} unique listings after dedup")

        # Cache per-account
        acc_dir = DATA_DIR / acc["username"]
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "listings.json").write_text(
            json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return listings


async def repost_account(acc: dict, listings: list) -> list:
    """Login as acc and repost the given listings."""
    core.EMAIL = acc["username"]
    core.PASSWORD = acc["password"]

    log_path = DATA_DIR / acc["username"] / "repost_log.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        repost_log = json.loads(log_path.read_text(encoding="utf-8"))
    except Exception:
        repost_log = []

    def _persist(entry: dict):
        repost_log.append(entry)
        log_path.write_text(
            json.dumps(repost_log, ensure_ascii=False, indent=2),
            encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=core.HEADLESS)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await core.login(page)

        results = []
        for listing in listings:
            # One ad crashing (page closed, network drop) must not lose the
            # log of what already happened, nor kill the remaining ads.
            try:
                result = await core.repost_listing(page, listing)
            except Exception as e:
                result = {
                    "original_url": listing.get("url", ""),
                    "title": listing.get("title", "Untitled"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "error",
                    "new_url": "",
                    "error": str(e),
                }
            results.append(result)
            _persist(result)
            await asyncio.sleep(2)

        await browser.close()
        return results


# ── In-app console logger ────────────────────────────────────────────────────
class GuiLogger(io.TextIOBase):
    """Redirect print() to the in-app console text box; also echo to real stdout."""
    def __init__(self, widget):
        self._widget = widget
        self._orig = sys.__stdout__

    def write(self, s):
        if self._orig:
            try:
                self._orig.write(s)
                self._orig.flush()
            except Exception:
                pass
        if s:
            self._widget.after(0, lambda msg=s: self._append(msg))
        return len(s)

    def flush(self):
        pass

    def _append(self, msg):
        try:
            self._widget.configure(state="normal")
            self._widget.insert("end", msg)
            self._widget.see("end")
            self._widget.configure(state="disabled")
        except Exception:
            pass


# ── Add-account dialog ────────────────────────────────────────────────────────
class AddAccountDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_save):
        super().__init__(parent)
        self.title("Add Account")
        self.geometry("380x260")
        self.resizable(False, False)
        self.configure(fg_color=_CARD_DARK)
        self.grab_set()
        self.on_save = on_save
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Add 999.md Account",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            pady=(24, 16))

        ctk.CTkLabel(self, text="Username or Email", anchor="w",
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTLE_TEXT).pack(anchor="w", padx=36)
        self.user_entry = ctk.CTkEntry(self, width=308, height=36,
                                        corner_radius=8,
                                        placeholder_text="your_username",
                                        border_color=_DIVIDER)
        self.user_entry.pack(padx=36, pady=(4, 12))

        ctk.CTkLabel(self, text="Password", anchor="w",
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTLE_TEXT).pack(anchor="w", padx=36)
        self.pass_entry = ctk.CTkEntry(self, width=308, height=36,
                                        corner_radius=8, show="*",
                                        placeholder_text="password",
                                        border_color=_DIVIDER)
        self.pass_entry.pack(padx=36, pady=(4, 18))

        ctk.CTkButton(self, text="Save Account", width=308, height=38,
                      corner_radius=8,
                      fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self._save).pack(padx=36)

    def _save(self):
        u = self.user_entry.get().strip()
        p = self.pass_entry.get().strip()
        if u and p:
            self.on_save({"username": u, "password": p})
            self.destroy()


# ── Theme constants ──────────────────────────────────────────────────────────
_ACCENT       = "#e53935"          # vibrant red
_ACCENT_HOVER = "#c62828"
_ACCENT_DARK  = "#b71c1c"
_BG_DARK      = "#121212"
_CARD_DARK    = "#1e1e1e"
_CARD_HOVER   = "#262626"
_SIDEBAR_BG   = "#181818"
_SUBTLE_TEXT  = "#9e9e9e"
_DIVIDER      = "#2a2a2a"


def _make_thumb(parent, listing: dict):
    """Build and return a thumbnail CTkFrame for a listing."""
    thumb = ctk.CTkFrame(parent, width=80, height=60,
                          fg_color=("#2a2a2a", "#2a2a2a"), corner_radius=8)
    thumb.pack_propagate(False)
    images = listing.get("local_images", [])
    loaded = False
    if images:
        p = Path(images[0])
        if p.exists():
            try:
                img = Image.open(p)
                img.thumbnail((76, 56))
                ctk_img = ctk.CTkImage(img, size=(76, 56))
                lbl = ctk.CTkLabel(thumb, image=ctk_img, text="")
                lbl.image = ctk_img
                lbl.pack(expand=True)
                loaded = True
            except Exception:
                pass
    if not loaded:
        ctk.CTkLabel(thumb, text="", font=ctk.CTkFont(size=20),
                      text_color=_SUBTLE_TEXT).pack(expand=True)
    return thumb


# ── Single listing row ────────────────────────────────────────────────────────
class ListingRow(ctk.CTkFrame):
    def __init__(self, parent, listing: dict, var: tk.BooleanVar,
                 on_change, on_blacklist):
        super().__init__(parent, corner_radius=10,
                         fg_color=(_CARD_DARK, _CARD_DARK),
                         border_width=1, border_color=(_DIVIDER, _DIVIDER))
        self.listing = listing
        self.var = var
        self._build(on_change, on_blacklist)

    def _build(self, on_change, on_blacklist):
        # Checkbox
        ctk.CTkCheckBox(self, text="", variable=self.var,
                         width=28, checkbox_width=20, checkbox_height=20,
                         fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
                         border_color=_SUBTLE_TEXT,
                         command=on_change).pack(
            side="left", padx=(12, 6), pady=12)

        # Thumbnail
        _make_thumb(self, self.listing).pack(side="left", padx=(0, 8), pady=10)

        # Text info
        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 4))

        title = (self.listing.get("title") or "Untitled")[:75]
        ctk.CTkLabel(info, text=title, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            fill="x", pady=(10, 3))

        price = self.listing.get("price", "")
        uid = (self.listing.get("url") or "").rstrip("/").split("/")[-1]
        meta_parts = ([price] if price else []) + [f"#{uid}"]
        ctk.CTkLabel(info, text="  ·  ".join(meta_parts),
                     anchor="w", font=ctk.CTkFont(size=11),
                     text_color=_SUBTLE_TEXT).pack(fill="x", pady=(0, 8))

        # Blacklist button
        ctk.CTkButton(
            self, text="Ban", width=46, height=26,
            corner_radius=6,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            border_width=1, border_color=(_DIVIDER, _DIVIDER),
            text_color=_SUBTLE_TEXT,
            hover_color=("#3a1010", "#3a1010"),
            command=on_blacklist
        ).pack(side="right", padx=(4, 12))


# ── Blacklist dialog ──────────────────────────────────────────────────────────
class BlacklistDialog(ctk.CTkToplevel):
    def __init__(self, parent, username: str, blacklist: set[str],
                 all_listings: list, on_remove):
        super().__init__(parent)
        self.title(f"Blacklist — @{username}")
        self.geometry("640x480")
        self.configure(fg_color=_BG_DARK)
        self.grab_set()
        self._on_remove = on_remove
        self._build(username, blacklist, all_listings)

    def _build(self, username, blacklist, all_listings):
        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(hdr, text=f"Blacklisted listings for @{username}",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        ctk.CTkLabel(hdr, text=f"{len(blacklist)} item(s)",
                     font=ctk.CTkFont(size=12),
                     text_color=_SUBTLE_TEXT).pack(side="right")

        ctk.CTkFrame(self, height=1, fg_color=(_DIVIDER, _DIVIDER)).pack(
            fill="x", padx=20, pady=(4, 8))

        if not blacklist:
            ctk.CTkLabel(self, text="No blacklisted listings.",
                         text_color=_SUBTLE_TEXT,
                         font=ctk.CTkFont(size=13)).pack(expand=True)
            return

        # Build lookup: url → listing data (for thumbnail/title/price)
        url_map = {l.get("url", "").rstrip("/"): l for l in all_listings}

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent",
                                         scrollbar_button_color=_DIVIDER)
        scroll.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        for url in sorted(blacklist):
            listing = url_map.get(url.rstrip("/"), {"url": url})
            self._add_row(scroll, listing, url)

    def _add_row(self, parent, listing, url):
        row = ctk.CTkFrame(parent, corner_radius=10,
                            fg_color=(_CARD_DARK, _CARD_DARK),
                            border_width=1, border_color=(_DIVIDER, _DIVIDER))
        row.pack(fill="x", pady=3)

        _make_thumb(row, listing).pack(side="left", padx=(10, 8), pady=8)

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True)

        title = (listing.get("title") or url)[:70]
        ctk.CTkLabel(info, text=title, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            fill="x", pady=(8, 2))

        price = listing.get("price", "")
        uid = url.rstrip("/").split("/")[-1]
        meta = ("  ·  ".join(([price] if price else []) + [f"#{uid}"]))
        ctk.CTkLabel(info, text=meta, anchor="w",
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTLE_TEXT).pack(fill="x", pady=(0, 8))

        ctk.CTkButton(
            row, text="Remove", width=72, height=28,
            corner_radius=6,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            border_width=1, border_color=(_ACCENT, _ACCENT),
            text_color=_ACCENT,
            hover_color=("#3a1010", "#3a1010"),
            command=lambda u=url, r=row: self._remove(u, r)
        ).pack(side="right", padx=12)

    def _remove(self, url, row_widget):
        self._on_remove(url)
        row_widget.destroy()


# ── Main window ───────────────────────────────────────────────────────────────
class App999(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("999 AutoPost")
        self.geometry("1100x720")
        self.minsize(860, 540)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=_BG_DARK)

        self.accounts: list = load_accounts()
        self.selected_account: dict | None = None
        self.listings: list = []
        self.listing_vars: list[tk.BooleanVar] = []
        self.blacklist: set[str] = set()   # per-account, loaded on account select

        self._build_ui()
        sys.stdout = GuiLogger(self.console_text)
        self._refresh_sidebar()

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Set window icon if logo exists
        logo_path = ASSETS_DIR / "logo.png"
        if logo_path.exists():
            try:
                logo_img = Image.open(logo_path)
                logo_img.thumbnail((256, 256))
                from PIL import ImageTk
                self._icon_img = ImageTk.PhotoImage(logo_img)
                self.wm_iconphoto(True, self._icon_img)
            except Exception:
                pass

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, height=56, corner_radius=0,
                            fg_color=(_CARD_DARK, _CARD_DARK),
                            border_width=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Accent strip at top
        accent_strip = ctk.CTkFrame(hdr, height=3, corner_radius=0,
                                     fg_color=(_ACCENT, _ACCENT))
        accent_strip.pack(fill="x", side="top")

        hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
        hdr_inner.pack(fill="both", expand=True, padx=16)

        # Logo in header
        logo_path = ASSETS_DIR / "logo.png"
        if logo_path.exists():
            try:
                logo_img = Image.open(logo_path)
                ctk_logo = ctk.CTkImage(logo_img, size=(36, 36))
                ctk.CTkLabel(hdr_inner, image=ctk_logo, text="").pack(
                    side="left", padx=(0, 10))
            except Exception:
                pass

        ctk.CTkLabel(
            hdr_inner, text="999 AutoPost",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color="white"
        ).pack(side="left")

        ctk.CTkLabel(
            hdr_inner, text="v1.0",
            font=ctk.CTkFont(size=11),
            text_color=_SUBTLE_TEXT
        ).pack(side="left", padx=(8, 0), pady=(4, 0))

        # ── Body ──────────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=0, pady=0)

        # ── Sidebar ───────────────────────────────────────────────────────────
        self.sidebar = ctk.CTkFrame(body, width=230, corner_radius=0,
                                     fg_color=(_SIDEBAR_BG, _SIDEBAR_BG),
                                     border_width=0)
        self.sidebar.pack(side="left", fill="y", padx=0)
        self.sidebar.pack_propagate(False)

        # Sidebar header
        sidebar_hdr = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        sidebar_hdr.pack(fill="x", padx=16, pady=(18, 10))
        ctk.CTkLabel(sidebar_hdr, text="Accounts",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=_SUBTLE_TEXT).pack(side="left")

        self.acc_scroll = ctk.CTkScrollableFrame(
            self.sidebar, fg_color="transparent",
            scrollbar_button_color=_DIVIDER,
            scrollbar_button_hover_color=_SUBTLE_TEXT)
        self.acc_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        ctk.CTkButton(
            self.sidebar, text="+ Add Account",
            font=ctk.CTkFont(size=12),
            height=36, corner_radius=8,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
            command=self._open_add_dialog
        ).pack(fill="x", padx=14, pady=(6, 16))

        # ── Right panel ───────────────────────────────────────────────────────
        right = ctk.CTkFrame(body, corner_radius=0,
                              fg_color=(_BG_DARK, _BG_DARK))
        right.pack(side="left", fill="both", expand=True)

        # Top bar inside right panel
        top_bar = ctk.CTkFrame(right, height=52, fg_color="transparent")
        top_bar.pack(fill="x", padx=20, pady=(14, 6))
        top_bar.pack_propagate(False)

        self.listings_lbl = ctk.CTkLabel(
            top_bar, text="Select an account to load listings",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
        )
        self.listings_lbl.pack(side="left", fill="y")

        # Action buttons — right side
        btn_frame = ctk.CTkFrame(top_bar, fg_color="transparent")
        btn_frame.pack(side="right")

        self.console_btn = ctk.CTkButton(
            btn_frame, text="Console", width=90,
            height=32, corner_radius=8,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            border_width=1, border_color=(_DIVIDER, _DIVIDER),
            text_color=_SUBTLE_TEXT,
            hover_color=(_CARD_HOVER, _CARD_HOVER),
            command=self._toggle_console
        )
        self.console_btn.pack(side="left", padx=(0, 6))

        self.refresh_btn = ctk.CTkButton(
            btn_frame, text="Refresh", width=90,
            height=32, corner_radius=8,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            border_width=1, border_color=(_DIVIDER, _DIVIDER),
            text_color=_SUBTLE_TEXT,
            hover_color=(_CARD_HOVER, _CARD_HOVER),
            command=self._refresh_listings
        )
        self.refresh_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_frame, text="Select All", width=90,
            height=32, corner_radius=8,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            border_width=1, border_color=(_DIVIDER, _DIVIDER),
            text_color=_SUBTLE_TEXT,
            hover_color=(_CARD_HOVER, _CARD_HOVER),
            command=self._toggle_all
        ).pack(side="left", padx=(0, 6))

        self.blacklist_btn = ctk.CTkButton(
            btn_frame, text="Blacklist (0)", width=110,
            height=32, corner_radius=8,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            border_width=1, border_color=(_DIVIDER, _DIVIDER),
            text_color=_SUBTLE_TEXT,
            hover_color=(_CARD_HOVER, _CARD_HOVER),
            command=self._open_blacklist
        )
        self.blacklist_btn.pack(side="left")

        # Divider line
        ctk.CTkFrame(right, height=1, fg_color=(_DIVIDER, _DIVIDER)).pack(
            fill="x", padx=20, pady=(0, 4))

        # Scrollable listing area
        self.listings_scroll = ctk.CTkScrollableFrame(
            right, fg_color="transparent",
            scrollbar_button_color=_DIVIDER,
            scrollbar_button_hover_color=_SUBTLE_TEXT)
        self.listings_scroll.pack(fill="both", expand=True, padx=14, pady=4)

        # Console panel — hidden until toggled
        self.console_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.console_text = ctk.CTkTextbox(
            self.console_frame, height=150, state="disabled",
            corner_radius=8,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=("#0d0d0d", "#0d0d0d"),
            text_color=("#4caf50", "#66bb6a"),
            border_width=1, border_color=(_DIVIDER, _DIVIDER),
        )
        self.console_text.pack(fill="both", expand=True, padx=14, pady=(0, 4))

        # Bottom bar
        bot = ctk.CTkFrame(right, height=60, fg_color="transparent")
        bot.pack(fill="x", padx=20, pady=(4, 14))
        bot.pack_propagate(False)
        self._bot = bot

        self.status_lbl = ctk.CTkLabel(
            bot, text="Ready",
            font=ctk.CTkFont(size=12),
            text_color=_SUBTLE_TEXT)
        self.status_lbl.pack(side="left", fill="y")

        self.progress = ctk.CTkProgressBar(bot, width=160,
                                            progress_color=_ACCENT)
        self.progress.set(0)

        self.repost_btn = ctk.CTkButton(
            bot,
            text="Repost Selected  (0)",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=42, width=220,
            corner_radius=10,
            fg_color=_ACCENT,
            hover_color=_ACCENT_HOVER,
            command=self._start_repost
        )
        self.repost_btn.pack(side="right")

    # ── Sidebar helpers ───────────────────────────────────────────────────────
    def _refresh_sidebar(self):
        for w in self.acc_scroll.winfo_children():
            w.destroy()

        for acc in self.accounts:
            active = (self.selected_account and
                      self.selected_account["username"] == acc["username"])
            row = ctk.CTkFrame(self.acc_scroll, corner_radius=8,
                                fg_color=(_ACCENT_DARK if active
                                          else _CARD_DARK,
                                          _ACCENT_DARK if active
                                          else _CARD_DARK),
                                border_width=1 if active else 0,
                                border_color=(_ACCENT, _ACCENT))
            row.pack(fill="x", pady=3)

            ctk.CTkButton(
                row,
                text=f"  @{acc['username']}",
                font=ctk.CTkFont(size=12,
                                  weight="bold" if active else "normal"),
                fg_color="transparent",
                hover_color=(_CARD_HOVER, _CARD_HOVER),
                text_color="white" if active else _SUBTLE_TEXT,
                anchor="w",
                height=34,
                command=lambda a=acc: self._load_account(a)
            ).pack(side="left", fill="x", expand=True,
                   padx=(4, 0), pady=4)

            ctk.CTkButton(
                row, text="x", width=26, height=26,
                corner_radius=6,
                font=ctk.CTkFont(size=11),
                fg_color="transparent",
                text_color=(_SUBTLE_TEXT, _SUBTLE_TEXT),
                hover_color=("#3a1010", "#3a1010"),
                command=lambda a=acc: self._delete_account(a)
            ).pack(side="right", padx=(0, 6), pady=4)

    def _open_add_dialog(self):
        AddAccountDialog(self, on_save=self._on_account_saved)

    def _on_account_saved(self, acc: dict):
        if not any(a["username"] == acc["username"] for a in self.accounts):
            self.accounts.append(acc)
            save_accounts(self.accounts)
            self._refresh_sidebar()

    def _delete_account(self, acc: dict):
        self.accounts = [a for a in self.accounts
                         if a["username"] != acc["username"]]
        save_accounts(self.accounts)
        if (self.selected_account and
                self.selected_account["username"] == acc["username"]):
            self.selected_account = None
            self._clear_listings()
        self._refresh_sidebar()

    # ── Listings ──────────────────────────────────────────────────────────────
    def _load_account(self, acc: dict):
        """Select an account and load its cached listings (no network call)."""
        self.selected_account = acc
        self.blacklist = load_blacklist(acc["username"])
        self._update_blacklist_btn()
        self._refresh_sidebar()
        self._clear_listings()

        cached = DATA_DIR / acc["username"] / "listings.json"
        if cached.exists():
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                if data:
                    self._on_listings_loaded(data, from_cache=True)
                    return
            except Exception:
                pass

        # No cache yet — prompt user to refresh
        self.listings_lbl.configure(
            text=f"@{acc['username']} — no cache yet")
        self.status_lbl.configure(
            text="No cached listings. Press Refresh to scan 999.md.")

    def _refresh_listings(self):
        """Scan 999.md for fresh listings for the selected account."""
        if not self.selected_account:
            return
        acc = self.selected_account
        self.refresh_btn.configure(state="disabled", text="Scanning...")
        self.listings_lbl.configure(text=f"Scanning @{acc['username']}...")
        self._show_progress(indeterminate=True)
        self.status_lbl.configure(text="Logging in and scanning 999.md…")

        threading.Thread(
            target=lambda: asyncio.run(self._bg_scrape(acc)),
            daemon=True
        ).start()

    async def _bg_scrape(self, acc: dict):
        try:
            listings = await scrape_account(acc)
            self.after(0, lambda: self._on_listings_loaded(listings))
        except Exception as e:
            self.after(0, lambda err=e: self.status_lbl.configure(
                text=f"Scan error: {err}"))
        finally:
            self.after(0, self._hide_progress)
            self.after(0, lambda: self.refresh_btn.configure(
                state="normal", text="Refresh"))

    def _on_listings_loaded(self, listings: list, from_cache: bool = False):
        # Filter out blacklisted listings
        visible = [l for l in listings
                   if l.get("url", "").rstrip("/") not in self.blacklist]
        self.listings = visible
        self.listing_vars = []

        for w in self.listings_scroll.winfo_children():
            w.destroy()

        uname = self.selected_account["username"] if self.selected_account else "?"
        suffix = "  (cached)" if from_cache else ""
        hidden = len(listings) - len(visible)
        hidden_txt = f", {hidden} hidden" if hidden else ""
        self.listings_lbl.configure(
            text=f"@{uname} — {len(visible)} listing(s){hidden_txt}{suffix}")

        for listing in visible:
            var = tk.BooleanVar(value=False)
            self.listing_vars.append(var)
            ListingRow(self.listings_scroll, listing, var,
                       on_change=self._update_repost_btn,
                       on_blacklist=lambda l=listing: self._blacklist_listing(l)
                       ).pack(fill="x", pady=3, padx=2)

        self._update_repost_btn()
        src = "cache" if from_cache else "999.md"
        self.status_lbl.configure(
            text=f"Loaded {len(visible)} listing(s) from {src}.")

    def _clear_listings(self):
        for w in self.listings_scroll.winfo_children():
            w.destroy()
        self.listings = []
        self.listing_vars = []
        self._update_repost_btn()

    # ── Blacklist ─────────────────────────────────────────────────────────────
    def _blacklist_listing(self, listing: dict):
        url = listing.get("url", "").rstrip("/")
        if not url or not self.selected_account:
            return
        self.blacklist.add(url)
        save_blacklist(self.selected_account["username"], self.blacklist)
        self._update_blacklist_btn()
        # Remove from displayed listings
        idx = next((i for i, l in enumerate(self.listings)
                    if l.get("url", "").rstrip("/") == url), None)
        if idx is not None:
            self.listings.pop(idx)
            var = self.listing_vars.pop(idx)
            var.set(False)
        # Rebuild the scroll area
        rows = list(self.listings_scroll.winfo_children())
        if idx is not None and idx < len(rows):
            rows[idx].destroy()
        self._update_repost_btn()
        uname = self.selected_account["username"]
        self.listings_lbl.configure(
            text=f"@{uname} — {len(self.listings)} listing(s)")

    def _unblacklist(self, url: str):
        self.blacklist.discard(url.rstrip("/"))
        if self.selected_account:
            save_blacklist(self.selected_account["username"], self.blacklist)
        self._update_blacklist_btn()

    def _update_blacklist_btn(self):
        n = len(self.blacklist)
        self.blacklist_btn.configure(
            text=f"Blacklist ({n})",
            text_color=_ACCENT if n else _SUBTLE_TEXT,
            border_color=(_ACCENT if n else _DIVIDER,
                          _ACCENT if n else _DIVIDER))

    def _open_blacklist(self):
        if not self.selected_account:
            return
        # Collect all known listing data (current + cached) for lookup
        all_listings = self.listings.copy()
        cached_path = DATA_DIR / self.selected_account["username"] / "listings.json"
        if cached_path.exists():
            try:
                all_listings = json.loads(cached_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        BlacklistDialog(
            self,
            username=self.selected_account["username"],
            blacklist=self.blacklist,
            all_listings=all_listings,
            on_remove=self._unblacklist
        )

    def _toggle_all(self):
        all_on = all(v.get() for v in self.listing_vars)
        for v in self.listing_vars:
            v.set(not all_on)
        self._update_repost_btn()

    def _update_repost_btn(self):
        n = sum(1 for v in self.listing_vars if v.get())
        self.repost_btn.configure(text=f"Repost Selected  ({n})")

    # ── Repost ────────────────────────────────────────────────────────────────
    def _start_repost(self):
        selected = [self.listings[i]
                    for i, v in enumerate(self.listing_vars) if v.get()]
        if not selected:
            messagebox.showwarning(
                "Nothing selected",
                "Check at least one listing to repost.")
            return
        if not self.selected_account:
            return

        self.repost_btn.configure(state="disabled", text="Reposting…")
        self.status_lbl.configure(
            text=f"Reposting {len(selected)} listing(s)…")
        self._show_progress(indeterminate=True)

        acc = self.selected_account
        threading.Thread(
            target=lambda: asyncio.run(self._bg_repost(acc, selected)),
            daemon=True
        ).start()

    async def _bg_repost(self, acc: dict, listings: list):
        try:
            results = await repost_account(acc, listings)
            self.after(0, lambda r=results: self._on_repost_done(r))
        except Exception as e:
            self.after(0, lambda err=e: self.status_lbl.configure(
                text=f"Repost error: {err}"))
            self.after(0, lambda err=e: messagebox.showerror(
                "Repost Failed",
                f"The repost run could not start:\n\n{err}"))
        finally:
            self.after(0, self._hide_progress)
            self.after(0, lambda: self.repost_btn.configure(
                state="normal",
                text=f"Repost Selected  "
                     f"({sum(1 for v in self.listing_vars if v.get())})"
            ))

    def _on_repost_done(self, results: list):
        success = sum(1 for r in results if r.get("status") == "success")
        # Paid categories (e.g. Autoturisme) are never paid for: known ones
        # are skipped upfront; unexpected ones get their created ad deleted.
        # Both end up as status "skipped_paid". "needs_payment" only remains
        # if that automatic delete failed (ad sits in cabinet → Neachitate).
        skipped_paid = sum(1 for r in results
                           if r.get("status") == "skipped_paid")
        needs_pay = sum(1 for r in results
                        if r.get("status") == "needs_payment")
        total = len(results)
        summary = f"Done: {success}/{total} reposted successfully."
        if skipped_paid:
            summary += f" {skipped_paid} skipped (paid category)."
        if needs_pay:
            summary += f" {needs_pay} stuck unpaid in cabinet!"
        self.status_lbl.configure(text=summary)

        lines = []
        for r in results:
            title = (r.get("title") or "Untitled")[:60]
            if r.get("status") == "success":
                lines.append(f"[OK]  {title}")
            elif r.get("status") == "skipped_paid":
                lines.append(f"[SKIP]  {title}\n     → {r.get('error', '')}")
            elif r.get("status") == "needs_payment":
                lines.append(f"[PAY]  {title}\n     → {r.get('error', '')}")
            else:
                reason = r.get("error") or "form validation failed"
                lines.append(f"[FAIL]  {title}\n     → {reason}")
        detail = "\n".join(lines)
        print("\n=== REPOST RESULTS ===\n" + detail + "\n")
        messagebox.showinfo(
            "Repost Complete",
            f"Reposted {success} of {total} listing(s)."
            + (f"\n{skipped_paid} skipped — paid category, never paying."
               if skipped_paid else "")
            + (f"\n{needs_pay} could not be cleaned up — check cabinet "
               f"tab Neachitate." if needs_pay else "")
            + f"\n\n{detail}")

    # ── Console toggle ────────────────────────────────────────────────────────
    def _toggle_console(self):
        if self.console_frame.winfo_ismapped():
            self.console_frame.pack_forget()
            self.console_btn.configure(text="Console",
                                       border_color=(_DIVIDER, _DIVIDER))
        else:
            self.console_frame.pack(fill="x", padx=6, pady=(0, 4),
                                    before=self._bot)
            self.console_btn.configure(text="Console",
                                       border_color=(_ACCENT, _ACCENT))

    # ── Progress bar helpers ──────────────────────────────────────────────────
    def _show_progress(self, indeterminate: bool = False):
        self.progress.pack(side="right", padx=(0, 10), before=self.repost_btn)
        if indeterminate:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.configure(mode="determinate")
            self.progress.set(0)

    def _hide_progress(self):
        self.progress.stop()
        self.progress.pack_forget()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App999()
    app.mainloop()
