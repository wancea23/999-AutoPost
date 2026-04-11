"""
999.md AutoPost CLI
Usage:
  python main.py scrape  — scrape all active listings from your profile
  python main.py repost  — repost all scraped listings as new ads
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ──────────────────────────────────────────────────────────────────
# Credentials — set via environment variables for CLI use.
# The GUI app (app.py) sets these at runtime before calling core functions.
EMAIL = os.environ.get("NNN_EMAIL", "")
PASSWORD = os.environ.get("NNN_PASSWORD", "")
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"

BASE_URL = "https://999.md"
LOGIN_URL = (
    "https://v2.simpalsid.com/sid/ro/user/login"
    "?projectId=999a46c6-e6a6-11e1-a45f-28376188709b"
    "&redirectUrl=https%3A%2F%2F999.md%2Fro"
)

DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"
LISTINGS_FILE = DATA_DIR / "listings.json"
REPOST_LOG_FILE = DATA_DIR / "repost_log.json"

DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def download_image(client: httpx.AsyncClient, url: str, dest: Path) -> str:
    if dest.exists():
        return str(dest)
    try:
        r = await client.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"    Downloaded image → {dest.name}")
        return str(dest)
    except Exception:
        # Silently skip images that 404 or fail — not critical
        return ""



# ── Design version ───────────────────────────────────────────────────────────
async def ensure_new_design(page):
    """
    Fresh Playwright browsers have no cookies, so 999.md defaults to the old
    design. Detect the old site and switch to the new one.
    New design has #avatar-circle-header; old one doesn't.
    """
    await page.wait_for_timeout(1000)
    if await page.locator("#avatar-circle-header").count() > 0:
        print("New design already active.")
        return

    print("Old design detected — switching to new design …")
    switched = False
    for selector in [
        "a:has-text('versiunea nouă')",
        "a:has-text('new version')",
        "button:has-text('versiunea nouă')",
        "[data-testid='new-design-switch']",
    ]:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=2000):
                await el.click()
                await page.wait_for_timeout(1500)
                switched = True
                break
        except Exception:
            pass

    if not switched:
        print("  Setting new-design cookie and reloading …")
        await page.context.add_cookies([{
            "name": "newDesign",
            "value": "true",
            "domain": "999.md",
            "path": "/",
        }])
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

    if await page.locator("#avatar-circle-header").count() > 0:
        print("New design active.")
    else:
        print("[WARN] Could not switch to new design automatically.")


# ── Login ────────────────────────────────────────────────────────────────────
async def login(page):
    print("Navigating to login page …")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    await page.fill('input[type="text"]', EMAIL)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')

    # Auth-confirm step ("Accesați site-ul 999.md" button)
    try:
        await page.wait_for_url("**/auth-confirm**", timeout=8_000)
        print("Auth-confirm page detected, clicking redirect button …")
        confirm_btn = page.locator("button:has-text('Accesați'), a:has-text('Accesați'), button.Button_solid__gEcaH").first
        await confirm_btn.wait_for(timeout=5_000)
        await confirm_btn.click()
    except PlaywrightTimeout:
        pass

    try:
        await page.wait_for_url("https://999.md/**", timeout=20_000)
        print(f"Logged in successfully. URL: {page.url}")
    except PlaywrightTimeout:
        if "999.md" in page.url:
            print(f"Logged in. URL: {page.url}")
        else:
            print(f"[WARN] Login may have failed. Current URL: {page.url}")

    await ensure_new_design(page)


# ── Scrape listing detail ─────────────────────────────────────────────────────
async def scrape_listing_detail(page, url: str, client: httpx.AsyncClient) -> dict:
    """Scrape a single listing page (new 999.md design) and return a dict."""
    clean_url = url.split("?")[0]
    print(f"  Scraping: {clean_url}")
    await page.goto(clean_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    listing = {"url": clean_url}

    # Extract everything via JavaScript — avoids CSS visibility issues
    extracted = await page.evaluate("""() => {
        // Title: try h1, then h2, then any element with 'title' in class
        let title = '';
        for (const sel of ['h1', 'h2', '[class*="title"]']) {
            const el = document.querySelector(sel);
            if (el && el.innerText.trim()) { title = el.innerText.trim(); break; }
        }

        // Price: anchor search near the <h1> title so we never pick up prices
        // from "similar listings" widgets or market-value estimates that appear
        // earlier in the DOM than the actual listing price.
        let price = '';
        const priceRe = /\d[\d\s\xa0]*(?:MDL|€|\$|lei)/i;
        const h1 = document.querySelector('h1');
        if (h1) {
            // Walk UP from h1 up to 6 levels; find the nearest ancestor that
            // also contains a price element — that ancestor is the listing header.
            let container = h1.parentElement;
            for (let i = 0; i < 6 && container; i++, container = container.parentElement) {
                for (const sel of [
                    '[data-testid="price"]', '[data-testid*="price"]',
                    '[class*="price__value"]', '[class*="PriceBlock"]',
                    '[class*="price-block"]', '[class*="Price"]', '[class*="price"]',
                ]) {
                    const el = container.querySelector(sel);
                    if (!el || el === container) continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 40 && priceRe.test(t)) { price = t; break; }
                }
                if (price) break;
            }
        }
        // Fallback: first price-like string anywhere on the page
        if (!price) {
            for (const sel of ['[class*="Price"]', '[class*="price"]']) {
                for (const el of document.querySelectorAll(sel)) {
                    const t = (el.innerText || '').trim();
                    if (t && t.length < 40 && priceRe.test(t)) { price = t; break; }
                }
                if (price) break;
            }
        }

        // Description
        let desc = '';
        for (const sel of ['[data-testid="advert-description"]','[class*="description__text"]','[class*="description"]']) {
            const el = document.querySelector(sel);
            if (el && el.innerText.trim()) { desc = el.innerText.trim(); break; }
        }

        // Breadcrumb
        const crumbEls = document.querySelectorAll(
            'nav[aria-label*="breadcrumb"] a, [class*="breadcrumb"] a, [class*="Breadcrumb"] a'
        );
        const crumbs = Array.from(crumbEls).map(e => e.innerText.trim()).filter(Boolean);
        const crumbLinks = Array.from(crumbEls).map(e => e.getAttribute('href') || '');
        const categoryHref = [...crumbLinks].reverse().find(h => h.includes('/category/') || h.includes('/list/')) || '';

        // Attributes — features section uses CSS accordion, read innerText directly
        const attrs = {};
        const featuresEl = document.querySelector('[class*="features"]');
        if (featuresEl) {
            const groups = featuresEl.querySelectorAll('[data-testid]');
            groups.forEach(group => {
                const lines = group.innerText
                    .split('\\n')
                    .map(l => l.trim())
                    .filter(l => l);
                // lines[0] = group header, then key/value pairs
                for (let i = 1; i + 1 < lines.length; i += 2) {
                    attrs[lines[i]] = lines[i + 1];
                }
            });
        }

        return { title, price, desc, crumbs, categoryHref, attrs };
    }""")

    listing["title"] = extracted.get("title", "")
    listing["price"] = extracted.get("price", "")
    listing["description"] = extracted.get("desc", "")
    listing["category_breadcrumb"] = extracted.get("crumbs", [])
    listing["category_url"] = extracted.get("categoryHref", "")
    listing["attributes"] = extracted.get("attrs", {})
    print(f"    title={listing['title']!r}  price={listing['price']!r}  attrs={len(listing['attributes'])}")

    # ── Photos ───────────────────────────────────────────────────────────────
    photo_urls = []
    try:
        imgs = await page.query_selector_all(
            "[class*='photo'] img, [class*='Photo'] img, "
            "[class*='gallery'] img, [class*='Gallery'] img, "
            "[class*='slider'] img, [class*='Slider'] img, "
            "[class*='viewer'] img, [class*='Viewer'] img"
        )
        seen = set()
        for img in imgs:
            src = (await img.get_attribute("src") or
                   await img.get_attribute("data-src") or "")
            if not src or any(x in src for x in ("icon", "logo", "avatar", "svg")):
                continue
            # Upgrade to largest size
            src = src.replace("/320x240/", "/original/").replace("/thumbs/", "/big/")
            if src not in seen:
                seen.add(src)
                photo_urls.append(src)
    except Exception:
        pass

    # ── Download images ──────────────────────────────────────────────────────
    listing_id = clean_url.rstrip("/").split("/")[-1]
    local_images = []
    async with httpx.AsyncClient(follow_redirects=True) as dl_client:
        for idx, img_url in enumerate(photo_urls):
            if not img_url.startswith("http"):
                img_url = BASE_URL + img_url
            ext = img_url.split("?")[0].rsplit(".", 1)[-1] or "jpg"
            fname = IMAGES_DIR / f"{listing_id}_{idx}.{ext}"
            local_path = await download_image(dl_client, img_url, fname)
            if local_path:
                local_images.append(local_path)

    listing["photo_urls"] = photo_urls
    listing["local_images"] = local_images
    return listing


# ── Navigate to My Ads ────────────────────────────────────────────────────────
async def navigate_to_my_ads(page):
    """Navigate directly to the 'Anunțurile mele' cabinet page."""
    url = f"{BASE_URL}/ro/cabinet/items/{EMAIL}?tab=active"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    print(f"  My Ads page: {page.url}")


# ── Scrape command ────────────────────────────────────────────────────────────
async def cmd_scrape():
    print("=== SCRAPE MODE ===")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await login(page)

        print("Navigating to My Ads …")
        await navigate_to_my_ads(page)

        # Collect listing URLs across all pages
        ad_urls = []
        page_num = 1
        while True:
            print(f"Scanning page {page_num} …")
            all_a = await page.query_selector_all("a[href]")
            hrefs = []
            for a in all_a:
                href = await a.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = BASE_URL + href
                # Strip query params for canonical form
                clean = href.split("?")[0].rstrip("/")
                # New 999.md listing URLs: 999.md/ro/<pure_number>
                last = clean.split("/")[-1]
                if last.isdigit() and clean not in ad_urls:
                    hrefs.append(clean)
                    ad_urls.append(clean)
            print(f"  Found {len(hrefs)} ads on page {page_num}")

            # Next page
            try:
                next_btn = page.locator(
                    "a[data-testid='pagination-next'], "
                    "a[aria-label='Next'], "
                    "[class*='pagination'] a:has-text('›'), "
                    "[class*='pagination'] a:has-text('»')"
                ).first
                if await next_btn.is_visible(timeout=2000):
                    await next_btn.click()
                    await page.wait_for_timeout(2000)
                    page_num += 1
                else:
                    break
            except Exception:
                break

        print(f"Total ads found: {len(ad_urls)}")
        if not ad_urls:
            print("[WARN] No ads found. Check that you are logged in and have active listings.")
            await browser.close()
            return

        listings = []
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for url in ad_urls:
                try:
                    data = await scrape_listing_detail(page, url, client)
                    listings.append(data)
                except Exception as e:
                    print(f"  [ERROR] Failed to scrape {url}: {e}")

        save_json(LISTINGS_FILE, listings)
        print(f"\nSaved {len(listings)} listings to {LISTINGS_FILE}")
        await browser.close()


# ── Form helpers ─────────────────────────────────────────────────────────────
async def fill_input(page, placeholder: str, value: str) -> bool:
    """Fill a text input or textarea by its placeholder text."""
    if not value:
        return False
    el = page.get_by_placeholder(placeholder).first
    if await el.count():
        await el.fill(str(value))
        return True
    return False


async def fill_dropdown(page, label_text: str, value: str) -> bool:
    """
    Fill a custom React dropdown/combobox by its label text.
    Finds all 'Selectează' triggers on the page — checking both innerText
    (div-based dropdowns) AND placeholder attribute (input-based comboboxes) —
    then picks the one whose screen centre is closest to the label.
    Clicks via page.mouse.click() so React synthetic events fire correctly.
    """
    if not value:
        return False
    value = str(value).strip()
    safe_label = label_text.replace("'", "\\'")

    result = await page.evaluate(f"""() => {{
        const labelText = '{safe_label}';

        // ── 1. Locate the label element (any tag) ────────────────────────────
        let labelRect = null;
        for (const el of document.querySelectorAll(
            'label, span, div, p, td, dt, b, strong, h4, h5, h6'
        )) {{
            const t = (el.innerText || '').trim().replace(/\\s*\\*$/, '').trim();
            if (t !== labelText) continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.width > 500 || r.height > 100) continue;
            labelRect = r;
            break;
        }}

        if (!labelRect) {{
            // Debug: collect short visible text snippets from the page
            const dbg = Array.from(document.querySelectorAll('label, span, div'))
                .filter(el => {{
                    const t = (el.innerText || '').trim();
                    const r = el.getBoundingClientRect();
                    return t.length > 0 && t.length < 40 && r.width > 0 &&
                           r.width < 400 && r.height < 60;
                }})
                .map(el => (el.innerText || '').trim().replace(/\\s*\\*$/, ''))
                .filter(Boolean).slice(0, 25);
            return {{ error: 'label_not_found', dbg }};
        }}

        const labelCY = labelRect.top  + labelRect.height / 2;
        const labelCX = labelRect.left + labelRect.width  / 2;

        // ── 2. Collect every "Selectează" trigger on the page ─────────────────
        // Check BOTH innerText (div dropdowns) and placeholder attr (input comboboxes)
        const seen = new Set();
        const triggers = [];
        for (const el of document.querySelectorAll(
            'div, button, span, input, [role="combobox"], [role="listbox"]'
        )) {{
            if (seen.has(el)) continue;
            seen.add(el);
            const inner = (el.innerText || '').trim();
            const ph    = (el.getAttribute('placeholder') || '').trim();
            const txt   = inner || ph;
            if (!txt.includes('Selectea')) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 10 || r.width > 900) continue;
            if (r.top < -200) continue;
            const dist = Math.abs((r.top + r.height/2) - labelCY)
                       + Math.abs((r.left + r.width/2) - labelCX) * 0.3;
            triggers.push({{ el, r, dist, area: r.width * r.height,
                             isInput: el.tagName === 'INPUT' }});
        }}

        if (!triggers.length) {{
            return {{ error: 'no_triggers', labelY: labelRect.top }};
        }}

        // Sort: closest first; break ties by smallest area (most specific element)
        triggers.sort((a, b) => {{
            if (Math.abs(a.dist - b.dist) > 5) return a.dist - b.dist;
            return a.area - b.area;
        }});

        const best = triggers[0];
        return {{
            x: best.r.left + best.r.width  / 2,
            y: best.r.top  + best.r.height / 2,
            isInput: best.isInput,
            dist: Math.round(best.dist),
            total: triggers.length,
        }};
    }}""")

    if not result or result.get("error"):
        err = (result or {}).get("error", "null")
        print(f"      [DBG] '{label_text}': {err}")
        if err == "label_not_found":
            print(f"             candidates: {(result or {}).get('dbg', [])}")
        return False

    # Real mouse click — fires React's synthetic event system
    await page.mouse.click(result["x"], result["y"])
    await page.wait_for_timeout(700)

    safe_value = value.replace("'", "\\'")

    # All option selectors — covers li-based, role-based, class-based,
    # AND plain div/span options (common on 999.md, e.g. Stare: "Nou"/"Uzat").
    # :text-is() matches exact visible text only, not parent containers.
    opt_sels = [
        f"li:has-text('{safe_value}')",
        f"[role='option']:has-text('{safe_value}')",
        f"[class*='option']:has-text('{safe_value}')",
        f"[class*='item']:has-text('{safe_value}')",
        f"div:text-is('{safe_value}')",
        f"span:text-is('{safe_value}')",
    ]

    # Poll up to 3 seconds for options to appear.
    # This handles both normal dropdowns (fast) and dependent dropdowns like
    # "Tip" which only populate after a parent field (Fel) finishes updating.
    opts_visible = False
    for _ in range(6):  # 6 × 500ms = 3s max
        for opt_sel in opt_sels:
            try:
                if await page.locator(opt_sel).first.is_visible(timeout=400):
                    opts_visible = True
                    break
            except Exception:
                pass
        if opts_visible:
            break
        await page.wait_for_timeout(500)

    # If still nothing, try typing (combobox / autocomplete path, e.g. Regiune)
    if not opts_visible:
        await page.keyboard.type(value[:5])
        await page.wait_for_timeout(700)

    # Click the matching option
    for opt_sel in opt_sels:
        try:
            opt = page.locator(opt_sel).first
            if await opt.is_visible(timeout=1500):
                await opt.click()
                await page.wait_for_timeout(400)
                return True
        except Exception:
            pass

    await page.keyboard.press("Escape")
    return False


# ── Repost command ────────────────────────────────────────────────────────────
async def repost_listing(page, listing: dict) -> dict:
    title = listing.get("title", "Untitled")
    print(f"  Reposting: {title}")

    # Build add URL with category pre-selected.
    # category_url is like /ro/list/transport/spare-parts-for-cars
    # The add form expects: /ro/add?category=transport&subcategory=transport%2Fspare-parts-for-cars
    category_url = listing.get("category_url", "")
    add_url = f"{BASE_URL}/ro/add"
    if category_url:
        # Strip leading /ro/list/ or /ro/category/
        path = category_url
        for prefix in ("/ro/list/", "/ro/category/", "/list/", "/category/"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            cat = parts[0]
            subcat = "/".join(parts)          # e.g. transport/spare-parts-for-cars
            add_url = f"{BASE_URL}/ro/add?category={cat}&subcategory={quote(subcat, safe='')}"
        elif len(parts) == 1:
            add_url = f"{BASE_URL}/ro/add?category={parts[0]}"

    await page.goto(add_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    log_entry = {
        "original_url": listing.get("url", ""),
        "title": title,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "new_url": "",
        "error": "",
    }

    try:
        # ── Category wizard (if not pre-set via URL) ─────────────────────────
        if not category_url:
            for crumb in listing.get("category_breadcrumb", [])[1:]:
                try:
                    cat = page.locator(f"text={crumb}").first
                    if await cat.is_visible(timeout=3000):
                        await cat.click()
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass

        await page.wait_for_timeout(1000)

        # ── Region (always Chișinău) ─────────────────────────────────────────
        ok = await fill_dropdown(page, "Regiune", "Chișinău")
        print(f"    Regiune='Chișinău' → {'✓' if ok else '✗'}")

        # ── Price ────────────────────────────────────────────────────────────
        price_num = re.sub(r"[^\d.]", "", listing.get("price", "") or "")
        ok = await fill_input(page, "Introdu prețul", price_num)
        print(f"    Preț={price_num!r} → {'✓' if ok else '✗'}")

        # ── Characteristics ──────────────────────────────────────────────────
        attrs = listing.get("attributes", {})
        for attr_key, attr_val in attrs.items():
            if attr_key == "An de fabricație":
                ok = await fill_input(page, "Introdu an de fabricație", attr_val)
            else:
                ok = await fill_dropdown(page, attr_key, attr_val)
            print(f"    {attr_key}={attr_val!r} → {'✓' if ok else '✗'}")

        # ── Titles (RO + RU both required) ───────────────────────────────────
        title_inputs = await page.query_selector_all(
            "input[placeholder*='titlul'], input[placeholder*='Titlul'], "
            "input[placeholder*='anunțului'], input[placeholder*='anuntului']"
        )
        for inp in title_inputs:
            await inp.fill(title)
        print(f"    Titles filled: {len(title_inputs)} input(s)")

        # ── Description (RO + RU textareas) ──────────────────────────────────
        desc = listing.get("description", "")
        if desc:
            desc_areas = await page.query_selector_all(
                "textarea[placeholder*='detalii'], textarea[placeholder*='Detalii']"
            )
            for ta in desc_areas:
                await ta.fill(desc)
            print(f"    Description filled: {len(desc_areas)} textarea(s)")

        # ── Photos ───────────────────────────────────────────────────────────
        local_images = [p for p in listing.get("local_images", []) if Path(p).exists()]
        if local_images:
            try:
                file_input = page.locator('input[type="file"]').first
                await file_input.set_input_files(local_images[:20])
                await page.wait_for_timeout(3000)
                print(f"    Photos: {len(local_images)} uploaded")
            except Exception as e:
                print(f"    [WARN] Photo upload failed: {e}")

        # ── Submit ───────────────────────────────────────────────────────────
        submit = page.locator(
            'button:has-text("Publică anunțul"), '
            'button:has-text("Publică"), '
            'button[type="submit"]'
        ).last
        await submit.click()
        await page.wait_for_timeout(4000)

        # Success = URL changed to a numeric listing ID
        new_url = page.url
        if new_url != add_url and "add" not in new_url.split("?")[0].rstrip("/").split("/")[-1]:
            log_entry["status"] = "success"
        else:
            log_entry["status"] = "incomplete"  # form had errors
        log_entry["new_url"] = new_url
        print(f"    → {new_url}  [{log_entry['status']}]")

    except Exception as e:
        log_entry["status"] = "error"
        log_entry["error"] = str(e)
        print(f"    [ERROR] {e}")

    return log_entry


async def cmd_repost():
    print("=== REPOST MODE ===")
    listings = load_json(LISTINGS_FILE, [])
    if not listings:
        print(f"[ERROR] No listings in {LISTINGS_FILE}. Run 'python main.py scrape' first.")
        sys.exit(1)

    repost_log = load_json(REPOST_LOG_FILE, [])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        await login(page)

        this_run = []
        for listing in listings:
            entry = await repost_listing(page, listing)
            this_run.append(entry)
            repost_log.append(entry)
            save_json(REPOST_LOG_FILE, repost_log)
            await asyncio.sleep(2)

        await browser.close()

    print(f"\nRepost log saved to {REPOST_LOG_FILE}")
    success = sum(1 for e in this_run if e["status"] == "success")
    print(f"Done: {success}/{len(listings)} listings reposted successfully.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("scrape", "repost"):
        print(__doc__)
        sys.exit(0)
    if sys.argv[1] == "scrape":
        asyncio.run(cmd_scrape())
    elif sys.argv[1] == "repost":
        asyncio.run(cmd_repost())


if __name__ == "__main__":
    main()
