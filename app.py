"""
999 AutoPost — GUI
Run: python app.py
"""
import asyncio
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

        for tab in ("active", "inactive"):
            tab_limit = max_active if tab == "active" else max_inactive
            tab_count = 0

            print(f"  Collecting {tab} listings (up to {tab_limit})…")
            url = f"{core.BASE_URL}/ro/cabinet/items/{acc['username']}?tab={tab}"
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)

            while tab_count < tab_limit:
                found_on_page = 0
                for a in await page.query_selector_all("a[href]"):
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=core.HEADLESS)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await core.login(page)

        results = []
        for listing in listings:
            result = await core.repost_listing(page, listing)
            results.append(result)
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
        self.geometry("340x230")
        self.resizable(False, False)
        self.grab_set()
        self.on_save = on_save
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Add 999.md Account",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(20, 12))

        ctk.CTkLabel(self, text="Username or Email", anchor="w").pack(
            anchor="w", padx=30)
        self.user_entry = ctk.CTkEntry(self, width=280,
                                        placeholder_text="your_username")
        self.user_entry.pack(padx=30, pady=(3, 10))

        ctk.CTkLabel(self, text="Password", anchor="w").pack(anchor="w", padx=30)
        self.pass_entry = ctk.CTkEntry(self, width=280, show="●",
                                        placeholder_text="••••••••")
        self.pass_entry.pack(padx=30, pady=(3, 14))

        ctk.CTkButton(self, text="Save Account", width=280,
                      command=self._save).pack(padx=30)

    def _save(self):
        u = self.user_entry.get().strip()
        p = self.pass_entry.get().strip()
        if u and p:
            self.on_save({"username": u, "password": p})
            self.destroy()


# ── Single listing row ────────────────────────────────────────────────────────
class ListingRow(ctk.CTkFrame):
    def __init__(self, parent, listing: dict, var: tk.BooleanVar, on_change):
        super().__init__(parent, corner_radius=8)
        self.listing = listing
        self.var = var
        self._build(on_change)

    def _build(self, on_change):
        # Checkbox
        ctk.CTkCheckBox(self, text="", variable=self.var,
                         width=32, command=on_change).pack(
            side="left", padx=(10, 4), pady=10)

        # Thumbnail
        thumb = ctk.CTkFrame(self, width=74, height=58,
                              fg_color="gray25", corner_radius=6)
        thumb.pack(side="left", padx=4, pady=8)
        thumb.pack_propagate(False)

        images = self.listing.get("local_images", [])
        loaded = False
        if images:
            p = Path(images[0])
            if p.exists():
                try:
                    img = Image.open(p)
                    img.thumbnail((70, 54))
                    ctk_img = ctk.CTkImage(img, size=(70, 54))
                    lbl = ctk.CTkLabel(thumb, image=ctk_img, text="")
                    lbl.image = ctk_img  # keep reference
                    lbl.pack(expand=True)
                    loaded = True
                except Exception:
                    pass
        if not loaded:
            ctk.CTkLabel(thumb, text="🖼",
                          font=ctk.CTkFont(size=22)).pack(expand=True)

        # Text info
        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=8)

        title = (self.listing.get("title") or "Untitled")[:70]
        ctk.CTkLabel(info, text=title, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            fill="x", pady=(8, 2))

        price = self.listing.get("price", "")
        uid = (self.listing.get("url") or "").rstrip("/").split("/")[-1]
        ctk.CTkLabel(info, text=f"{price}  ·  #{uid}",
                     anchor="w", font=ctk.CTkFont(size=11),
                     text_color="gray55").pack(fill="x")


# ── Main window ───────────────────────────────────────────────────────────────
class App999(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("999 AutoPost")
        self.geometry("1060x700")
        self.minsize(820, 520)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.accounts: list = load_accounts()
        self.selected_account: dict | None = None
        self.listings: list = []
        self.listing_vars: list[tk.BooleanVar] = []

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
        hdr = ctk.CTkFrame(self, height=64, corner_radius=0,
                            fg_color=("#b71c1c", "#7f0000"))
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Logo in header
        logo_path = ASSETS_DIR / "logo.png"
        if logo_path.exists():
            try:
                logo_img = Image.open(logo_path)
                ctk_logo = ctk.CTkImage(logo_img, size=(44, 44))
                ctk.CTkLabel(hdr, image=ctk_logo, text="").pack(
                    side="left", padx=(14, 6), pady=10)
            except Exception:
                pass

        ctk.CTkLabel(
            hdr, text='999 AutoPost',
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="white"
        ).pack(side="left", pady=10)

        # ── Body ──────────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=10)

        # ── Sidebar ───────────────────────────────────────────────────────────
        self.sidebar = ctk.CTkFrame(body, width=215, corner_radius=10)
        self.sidebar.pack(side="left", fill="y", padx=(0, 10))
        self.sidebar.pack_propagate(False)

        ctk.CTkLabel(self.sidebar, text="Accounts",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
            pady=(14, 6), padx=12)

        self.acc_scroll = ctk.CTkScrollableFrame(self.sidebar)
        self.acc_scroll.pack(fill="both", expand=True, padx=6, pady=(0, 4))

        ctk.CTkButton(
            self.sidebar, text="＋  Add Account",
            command=self._open_add_dialog
        ).pack(fill="x", padx=10, pady=(4, 12))

        # ── Right panel ───────────────────────────────────────────────────────
        right = ctk.CTkFrame(body, corner_radius=10)
        right.pack(side="left", fill="both", expand=True)

        # Top bar inside right panel
        top_bar = ctk.CTkFrame(right, height=48, fg_color="transparent")
        top_bar.pack(fill="x", padx=14, pady=(10, 2))
        top_bar.pack_propagate(False)

        self.listings_lbl = ctk.CTkLabel(
            top_bar, text="← Select an account to load its listings",
            font=ctk.CTkFont(size=13), anchor="w"
        )
        self.listings_lbl.pack(side="left", fill="y")

        ctk.CTkButton(
            top_bar, text="Select All", width=108,
            command=self._toggle_all
        ).pack(side="right")

        self.refresh_btn = ctk.CTkButton(
            top_bar, text="⟳  Refresh", width=108,
            command=self._refresh_listings
        )
        self.refresh_btn.pack(side="right", padx=(0, 6))

        self.console_btn = ctk.CTkButton(
            top_bar, text="Console ▼", width=100,
            fg_color="transparent", border_width=1,
            command=self._toggle_console
        )
        self.console_btn.pack(side="right", padx=(0, 6))

        # Scrollable listing area
        self.listings_scroll = ctk.CTkScrollableFrame(right)
        self.listings_scroll.pack(fill="both", expand=True, padx=8, pady=4)

        # Console panel — hidden until toggled
        self.console_frame = ctk.CTkFrame(right, fg_color="transparent")
        # (not packed here — shown via _toggle_console)
        self.console_text = ctk.CTkTextbox(
            self.console_frame, height=160, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=("#111111", "#0a0a0a"),
            text_color=("#00dd55", "#00ff66"),
        )
        self.console_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # Bottom bar
        bot = ctk.CTkFrame(right, height=56, fg_color="transparent")
        bot.pack(fill="x", padx=14, pady=(2, 10))
        bot.pack_propagate(False)
        self._bot = bot   # saved for console insertion order

        self.status_lbl = ctk.CTkLabel(
            bot, text="Ready.", font=ctk.CTkFont(size=12))
        self.status_lbl.pack(side="left", fill="y")

        self.progress = ctk.CTkProgressBar(bot, width=160)
        self.progress.set(0)

        self.repost_btn = ctk.CTkButton(
            bot,
            text="Repost Selected  (0)",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=40, width=230,
            fg_color=("#b71c1c", "#7f0000"),
            hover_color=("#8b0000", "#5a0000"),
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
            row = ctk.CTkFrame(self.acc_scroll, corner_radius=6)
            row.pack(fill="x", pady=3)

            ctk.CTkButton(
                row,
                text=f"@{acc['username']}",
                font=ctk.CTkFont(size=12,
                                  weight="bold" if active else "normal"),
                fg_color=("#8b0000", "#5a0000") if active
                          else ("gray17", "gray17"),
                hover_color=("#b71c1c", "#7f0000"),
                anchor="w",
                command=lambda a=acc: self._load_account(a)
            ).pack(side="left", fill="x", expand=True,
                   padx=(6, 2), pady=5)

            ctk.CTkButton(
                row, text="✕", width=28, height=28,
                fg_color="transparent",
                text_color=("gray50", "gray45"),
                hover_color=("gray25", "gray25"),
                command=lambda a=acc: self._delete_account(a)
            ).pack(side="right", padx=(2, 6), pady=5)

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
            text="No cached listings. Press ⟳ Refresh to scan 999.md.")

    def _refresh_listings(self):
        """Scan 999.md for fresh listings for the selected account."""
        if not self.selected_account:
            return
        acc = self.selected_account
        self.refresh_btn.configure(state="disabled", text="Scanning…")
        self.listings_lbl.configure(text=f"Scanning @{acc['username']}…")
        self._show_progress(indeterminate=True)
        self.status_lbl.configure(text="Logging in and scanning 999.md…")

        threading.Thread(
            target=lambda: asyncio.run(self._bg_scrape(acc)),
            daemon=True
        ).start()

    async def _bg_scrape(self, acc: dict):
        try:
            listings = await scrape_account(acc, max_listings=10)
            self.after(0, lambda: self._on_listings_loaded(listings))
        except Exception as e:
            self.after(0, lambda err=e: self.status_lbl.configure(
                text=f"Scan error: {err}"))
        finally:
            self.after(0, self._hide_progress)
            self.after(0, lambda: self.refresh_btn.configure(
                state="normal", text="⟳  Refresh"))

    def _on_listings_loaded(self, listings: list, from_cache: bool = False):
        self.listings = listings
        self.listing_vars = []

        for w in self.listings_scroll.winfo_children():
            w.destroy()

        uname = self.selected_account["username"] if self.selected_account else "?"
        suffix = "  (cached)" if from_cache else ""
        self.listings_lbl.configure(
            text=f"@{uname} — {len(listings)} listing(s){suffix}")

        for listing in listings:
            var = tk.BooleanVar(value=False)
            self.listing_vars.append(var)
            ListingRow(self.listings_scroll, listing, var,
                       on_change=self._update_repost_btn).pack(
                fill="x", pady=3, padx=2)

        self._update_repost_btn()
        src = "cache" if from_cache else "999.md"
        self.status_lbl.configure(
            text=f"Loaded {len(listings)} listing(s) from {src}.")

    def _clear_listings(self):
        for w in self.listings_scroll.winfo_children():
            w.destroy()
        self.listings = []
        self.listing_vars = []
        self._update_repost_btn()

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
            success = sum(1 for r in results
                          if r.get("status") == "success")
            self.after(0, lambda s=success, t=len(results):
                       self._on_repost_done(s, t))
        except Exception as e:
            self.after(0, lambda err=e: self.status_lbl.configure(
                text=f"Repost error: {err}"))
        finally:
            self.after(0, self._hide_progress)
            self.after(0, lambda: self.repost_btn.configure(
                state="normal",
                text=f"Repost Selected  "
                     f"({sum(1 for v in self.listing_vars if v.get())})"
            ))

    def _on_repost_done(self, success: int, total: int):
        self.status_lbl.configure(
            text=f"Done: {success}/{total} reposted successfully.")
        messagebox.showinfo(
            "Repost Complete",
            f"Successfully reposted {success} of {total} listing(s).")

    # ── Console toggle ────────────────────────────────────────────────────────
    def _toggle_console(self):
        if self.console_frame.winfo_ismapped():
            self.console_frame.pack_forget()
            self.console_btn.configure(text="Console ▼")
        else:
            self.console_frame.pack(fill="x", padx=8, pady=(0, 4),
                                    before=self._bot)
            self.console_btn.configure(text="Console ▲")

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
