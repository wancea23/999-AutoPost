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
from urllib.parse import quote, urlparse

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
def _on_999(url: str) -> bool:
    """
    True only when the browser is actually ON 999.md. A naive `'999.md' in
    url` check is wrong: the Simpals login/auth-confirm URLs carry
    `redirectUrl=https%3A%2F%2F999.md%2F…` in their query string, so the
    substring matches while we're still stuck on v2.simpalsid.com (this
    false positive was the root cause of silent repost failures).
    """
    host = urlparse(url).hostname or ""
    return host == "999.md" or host.endswith(".999.md")


async def _click_auth_confirm(page) -> bool:
    """Click the 'Accesați site-ul 999.md' button on the auth-confirm page."""
    try:
        btn = page.locator(
            "button:has-text('Accesați'), a:has-text('Accesați'), "
            "button.Button_solid__gEcaH").first
        await btn.wait_for(state="visible", timeout=10_000)
        await btn.click()
        return True
    except Exception as e:
        print(f"  [WARN] Auth-confirm click failed: {e}")
        return False


async def login(page, max_attempts: int = 3):
    """
    Log in via Simpals ID. Verified live 2026-07-12 — the flow has three
    states we must handle explicitly:
      • password form  → fill + submit (wait for hydration first: filling the
        React form too early makes the submit click a silent no-op);
      • auth-confirm interstitial → click 'Accesați site-ul 999.md'. When a
        SID session already exists, /login redirects straight here with NO
        password form, so this must be handled on entry too;
      • already on 999.md → done.
    Success is verified against the real hostname (see _on_999) and retried;
    on definitive failure we raise instead of letting callers scrape/fill a
    login page.
    """
    last_note = ""
    for attempt in range(1, max_attempts + 1):
        print(f"Navigating to login page … (attempt {attempt}/{max_attempts})")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Wait for whichever state materialises first.
        state = ""
        for _ in range(30):                      # up to ~15 s
            if _on_999(page.url):
                state = "done"
                break
            if "auth-confirm" in page.url:
                state = "confirm"
                break
            try:
                if await page.locator('input[type="password"]').first.is_visible():
                    state = "form"
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)

        if state == "form":
            await page.wait_for_timeout(700)     # let React attach handlers
            await page.fill('input[type="text"]', EMAIL)
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')

            # Wait to leave the login page; read any inline error so wrong
            # credentials fail fast instead of retrying forever.
            left = False
            for _ in range(30):                  # up to ~15 s
                if _on_999(page.url) or "auth-confirm" in page.url:
                    left = True
                    break
                await page.wait_for_timeout(500)
            if not left:
                try:
                    last_note = (await page.locator(
                        '[class*="error" i], [role="alert"]'
                    ).first.inner_text(timeout=1_000)).strip()
                except Exception:
                    last_note = ""
                # Only credential-specific messages abort the retry loop —
                # generic transient errors ("Something went wrong") must NOT
                # match, so no bare "wrong"/"error" keywords here.
                if last_note and re.search(
                        r"(greșit|gresit|incorect|wrong password|wrong login"
                        r"|invalid credential|неверн)",
                        last_note, re.I):
                    raise RuntimeError(
                        f"Login failed — wrong credentials: {last_note}")
                print(f"  [WARN] Still on login page"
                      f"{' — ' + last_note if last_note else ''}; retrying …")
                await page.wait_for_timeout(5_000)  # back off (rate limiting)
                continue

        if "auth-confirm" in page.url:
            print("Auth-confirm page detected, clicking redirect button …")
            await _click_auth_confirm(page)

        # Final verification: we must land on the real 999.md host.
        forced_nav = False
        for i in range(40):                      # up to ~20 s
            if _on_999(page.url):
                break
            # The auth-confirm click sometimes doesn't navigate even though
            # the SID session is already established (seen as "Login failed
            # after 3 attempts, stuck at …auth-confirm"). After ~10 s force
            # a direct navigation and let the session cookie do the work.
            if not forced_nav and i >= 20 and "auth-confirm" in page.url:
                print("  Auth-confirm did not redirect — going to 999.md directly …")
                await page.goto(BASE_URL + "/ro", wait_until="domcontentloaded")
                forced_nav = True
            await page.wait_for_timeout(500)
        if forced_nav and _on_999(page.url):
            # Direct navigation lands on 999.md even without a session —
            # only accept it if we're actually logged in.
            await page.wait_for_timeout(1500)
            logged = await page.locator(
                "#avatar-circle-header, a[href*='/cabinet/']").count()
            if not logged:
                print("  [WARN] Landed on 999.md logged-out; retrying login …")
                continue
        if _on_999(page.url):
            print(f"Logged in successfully. URL: {page.url}")
            await ensure_new_design(page)
            return
        last_note = f"ended at {page.url}"
        print(f"  [WARN] Login attempt {attempt} {last_note}; retrying …")

    raise RuntimeError(
        f"Login failed after {max_attempts} attempts"
        f"{' — ' + last_note if last_note else ''}")


# ── Expand collapsed sections ────────────────────────────────────────────────
async def expand_collapsibles(page):
    """
    Click "Citește tot" / "Afișează totul" expanders so the full description
    and feature lists are mounted before we scrape. On 999.md the "Citește
    tot" trigger is rendered as a <label> (not a button) tied to a hidden
    checkbox via styles_description__button, so we include `label` selectors.
    """
    texts = [
        "Citește tot", "Citeste tot",
        "Citește mai mult", "Citeste mai mult",
        "Afișează totul", "Afiseaza totul",
        "Afișează tot", "Afiseaza tot",
        "Vezi totul", "Vezi tot",
        "Показать всё", "Показать все", "Читать далее", "Подробнее",
        "Show all", "Show more", "Read more",
    ]
    for _ in range(2):
        clicked_any = False
        for txt in texts:
            try:
                loc = page.locator(
                    f"button:has-text('{txt}'), a:has-text('{txt}'), "
                    f"label:has-text('{txt}'), span:has-text('{txt}'), "
                    f"div[role='button']:has-text('{txt}')"
                )
                count = await loc.count()
                for i in range(count):
                    try:
                        el = loc.nth(i)
                        if await el.is_visible(timeout=200):
                            await el.scroll_into_view_if_needed(timeout=500)
                            await el.click(timeout=1000)
                            clicked_any = True
                            await page.wait_for_timeout(250)
                    except Exception:
                        pass
            except Exception:
                pass
        if not clicked_any:
            break
        await page.wait_for_timeout(400)


# ── Scrape listing detail ─────────────────────────────────────────────────────
async def scrape_listing_detail(page, url: str, client: httpx.AsyncClient) -> dict:
    """Scrape a single listing page (new 999.md design) and return a dict."""
    clean_url = url.split("?")[0]
    print(f"  Scraping: {clean_url}")
    await page.goto(clean_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    # Scroll to the bottom once to trigger lazy-loaded sections, then expand
    # every "Citește tot" / "Afișează totul" so the full text and checkbox
    # lists are mounted before we read them.
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(600)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except Exception:
        pass
    await expand_collapsibles(page)

    listing = {"url": clean_url}

    # Extract everything via JavaScript — avoids CSS visibility issues
    extracted = await page.evaluate("""() => {
        // Title — on 999.md detail pages the real title is in <h2>; the
        // <h1> is an empty `introjs-tooltip-title` placeholder. Try h2 first.
        let title = '';
        for (const sel of ['h2', 'h1', '[class*="title"]']) {
            const el = document.querySelector(sel);
            if (el && el.innerText.trim()) { title = el.innerText.trim(); break; }
        }

        // Price: the listing detail page has exactly one element with class
        // matching `price__main` — that's the headline price. Every other
        // price on the page (`price__text`, `oldprice`, `price__component`)
        // belongs to the similar-listings strip below. Verified live on
        // 999.md/ro new design 2026-05-14.
        let price = '';
        const priceMain = document.querySelector('[class*="price__main"]');
        if (priceMain) price = (priceMain.innerText || '').trim();
        // Fallback for non-car listings if `price__main` is missing.
        if (!price) {
            const priceRe = /\d[\d\s\xa0\.,]*\s*(?:MDL|€|EUR|\$|USD|lei)/i;
            for (const el of document.querySelectorAll(
                '[class*="price__value"], [class*="Price__value"], ' +
                '[class*="price-block"], [class*="PriceBlock"]'
            )) {
                const t = (el.innerText || '').trim();
                if (t && t.length < 60 && priceRe.test(t)) { price = t; break; }
            }
        }

        // Currency detection — repost form needs to flip the currency toggle.
        let currency = '';
        if (/€|EUR/i.test(price))      currency = 'EUR';
        else if (/\$|USD/i.test(price)) currency = 'USD';
        else if (/MDL|lei/i.test(price)) currency = 'MDL';

        // Description — the wrapper `[itemprop="description"]` contains both
        // the body and the "Citește tot" <label>, so its innerText ends with
        // "Citește tot". Use the inner `description__body` element instead
        // (and the JSON-LD product description as a last-resort fallback).
        // Strip any stray "Citește tot" / "Citește mai mult" lines just in
        // case 999.md rearranges the DOM.
        let desc = '';
        const descBody = document.querySelector('[class*="description__body"]');
        if (descBody && descBody.innerText.trim()) {
            desc = descBody.innerText.trim();
        } else {
            for (const sel of ['[data-testid="advert-description"]',
                               '[class*="description__text"]',
                               '[itemprop="description"]']) {
                const el = document.querySelector(sel);
                if (el && el.innerText.trim()) { desc = el.innerText.trim(); break; }
            }
            // Final fallback: JSON-LD Product.description
            if (!desc) {
                for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                    try {
                        const j = JSON.parse(s.textContent);
                        const d = j.description ||
                                  (j['@graph'] && j['@graph'][0] && j['@graph'][0].description);
                        if (d) { desc = String(d).trim(); break; }
                    } catch (e) {}
                }
            }
        }
        desc = desc.replace(
            /\\n?\\s*(Cite[șs]te (tot|mai mult)|Citește tot|Show more|Read more|Показать всё|Подробнее)\\s*$/i,
            ''
        ).trim();

        // Breadcrumb
        const crumbEls = document.querySelectorAll(
            'nav[aria-label*="breadcrumb"] a, [class*="breadcrumb"] a, [class*="Breadcrumb"] a'
        );
        const crumbs = Array.from(crumbEls).map(e => e.innerText.trim()).filter(Boolean);
        const crumbLinks = Array.from(crumbEls).map(e => e.getAttribute('href') || '');
        const categoryHref = [...crumbLinks].reverse().find(h => h.includes('/category/') || h.includes('/list/')) || '';

        // Attributes — `[class*="features"] [data-testid="<GroupName>"]`
        // wraps each feature group. Inside each group every <li> is one
        // feature row:
        //   • 3 children → key/value pair (icon + label + value)
        //   • 2 children → checkbox feature (checkmark + label only)
        // We discriminate per-row instead of per-group because cars mix both
        // (Securitate has only checkboxes, Particularități only k/v, but
        // hybrid groups exist in other categories). Verified live 2026-05-14.
        const attrs = {};
        const feature_lists = {};
        const featuresEl = document.querySelector('[class*="features"]');
        if (featuresEl) {
            featuresEl.querySelectorAll('[data-testid]').forEach(group => {
                const groupName = group.getAttribute('data-testid') || '';
                const liItems = group.querySelectorAll('li');
                const checkboxItems = [];
                liItems.forEach(li => {
                    const parts = (li.innerText || '').split('\\n')
                        .map(s => s.trim()).filter(Boolean);
                    if (parts.length === 2) {
                        attrs[parts[0]] = parts[1];
                    } else if (parts.length === 1) {
                        checkboxItems.push(parts[0]);
                    }
                });
                if (checkboxItems.length) {
                    feature_lists[groupName] = Array.from(new Set(checkboxItems));
                }
            });
        }

        return { title, price, currency, desc, crumbs, categoryHref, attrs, feature_lists };
    }""")

    listing["title"] = extracted.get("title", "")
    listing["price"] = extracted.get("price", "")
    listing["currency"] = extracted.get("currency", "")
    listing["description"] = extracted.get("desc", "")
    listing["category_breadcrumb"] = extracted.get("crumbs", [])
    listing["category_url"] = extracted.get("categoryHref", "")
    listing["attributes"] = extracted.get("attrs", {})
    listing["feature_lists"] = extracted.get("feature_lists", {})
    feat_count = sum(len(v) for v in listing["feature_lists"].values())
    print(f"    title={listing['title']!r}  price={listing['price']!r}  "
          f"cur={listing['currency']!r}  attrs={len(listing['attributes'])}  "
          f"features={feat_count}")

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

        # Collect listing URLs across all pages.
        # Skip "Cumpăra" cards entirely — those are wanted-ads (buyer looking
        # for an item) and must not be re-posted. We read each card's type
        # badge from the DOM and drop matches before they ever hit the detail
        # scraper.
        ad_urls = []
        skipped_cumpara = 0
        page_num = 1
        while True:
            print(f"Scanning page {page_num} …")
            cards = await page.evaluate(f"""(BASE_URL) => {{
                // Each cabinet ad card wraps a single listing link whose last
                // URL segment is a pure digit (e.g. /ro/12345678). Walk those
                // anchors and inspect the surrounding card text for a
                // "Cumpăr"/"Cumpara" badge.
                const out = [];
                const seen = new Set();
                for (const a of document.querySelectorAll('a[href]')) {{
                    let href = a.getAttribute('href') || '';
                    if (!href.startsWith('http')) href = BASE_URL + href;
                    const clean = href.split('?')[0].replace(/\\/$/, '');
                    const last = clean.split('/').pop();
                    if (!/^\\d+$/.test(last)) continue;
                    if (seen.has(clean)) continue;
                    seen.add(clean);
                    // Walk up to the nearest card-ish container to read its text.
                    let card = a;
                    for (let i = 0; i < 6 && card.parentElement; i++) {{
                        card = card.parentElement;
                        const cls = (card.className || '').toString().toLowerCase();
                        if (cls.includes('card') || cls.includes('item') || cls.includes('advert')) break;
                    }}
                    const text = (card.innerText || '').toLowerCase();
                    const isCumpara = /\\bcump[ăa]r/.test(text);
                    out.push({{ url: clean, isCumpara }});
                }}
                return out;
            }}""", BASE_URL)
            hrefs = []
            for c in cards:
                if c["url"] in ad_urls:
                    continue
                if c["isCumpara"]:
                    skipped_cumpara += 1
                    print(f"  [skip] Cumpăra listing: {c['url']}")
                    continue
                hrefs.append(c["url"])
                ad_urls.append(c["url"])
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

        print(f"Total ads found: {len(ad_urls)} (skipped {skipped_cumpara} Cumpăra)")
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
    Fill a 999.md React combobox by its label text. Verified live 2026-05-14.

    Every dropdown on the new add form has the same structure:
        <div class="styles_field__row__86QTX …">
          <div class="styles_field__row__title__9wh0D">
            <span class="styles_title__name__vu2nT">{LABEL}</span>
          </div>
          <input data-testid="{N}-input"        aria-haspopup="listbox"
                 placeholder="Selectează"       value="">
          <input data-testid="{N}-hidden-input" type="hidden">
        </div>

    Strategy:
      1. Find the title span with EXACT label text + `title__name` class
         (so "Tip" doesn't ambiguously match "Tip combustibil", and the
         label-only `field__row__title` container is distinguishable from
         the input container).
      2. Walk up to the surrounding `field__row__86QTX` container.
      3. Click `input[data-testid$="-input"]` inside that container — this
         opens a `<div role="listbox" id="{N}">` of `[role="option"]`s.
      4. Poll up to 4 s (dependent dropdowns like Tip take a moment to
         populate after their parent field is selected) then click the
         option whose text equals (or contains) the value.
    """
    if not value:
        return False
    value = str(value).strip()
    safe_label = label_text.replace("'", "\\'")

    # ── 1+2: Locate the field & decide whether it's a listbox or a value
    # input. Two field shapes exist on the new add form:
    #   • listbox combobox  → `<input data-testid="620-input" aria-haspopup="listbox" …>`
    #   • numeric/free text → `<input id="N.value" placeholder="Introdu valoarea" …>`
    #                         often with a sibling `<button data-testid="N.unit">km|€|…</button>`
    info = await page.evaluate(f"""() => {{
        const labelText = '{safe_label}';
        // Source listings render labels like "Putere" while the add form
        // bakes the unit into the label ("Putere, CP"). Accept the first
        // exact match if any; otherwise fall back to a prefix match such
        // that "Putere" matches "Putere, CP" or "Putere CP".
        let exact = null, prefix = null;
        for (const sp of document.querySelectorAll('span')) {{
            if (!(sp.className || '').toString().includes('title__name')) continue;
            const r = sp.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const t = (sp.innerText || '').trim().replace(/\\s*\\*$/, '').trim();
            if (t === labelText) {{ exact = sp; break; }}
            if (!prefix && (t.startsWith(labelText + ',') ||
                            t.startsWith(labelText + ' '))) {{
                prefix = sp;
            }}
        }}
        const title = exact || prefix;
        if (!title) return {{ error: 'label_not_found' }};
        // Walk up to the field container (NOT the title-only container)
        let row = title;
        for (let i = 0; i < 10; i++) {{
            row = row.parentElement;
            if (!row) break;
            const cls = (row.className || '').toString();
            if (cls.includes('field__row__86QTX') ||
                (cls.includes('field__row') && !cls.includes('field__row__title'))) break;
        }}
        if (!row) return {{ error: 'no_field_row' }};

        // 1. Listbox combobox?
        const combo = row.querySelector(
            'input[aria-haspopup="listbox"][data-testid$="-input"]:not([type="hidden"])'
        );
        if (combo) {{
            return {{
                kind: 'listbox',
                testid: combo.getAttribute('data-testid'),
                currentValue: combo.value || '',
            }};
        }}

        // 2. Plain value input?  Match `id$=".value"` first (price/rulaj/putere
        //    use this canonical form), then fall back to placeholder match.
        let val = row.querySelector('input[id$=".value"]:not([type="hidden"])');
        if (!val) {{
            val = row.querySelector(
                'input[placeholder*="valoarea" i]:not([type="hidden"]), ' +
                'input[placeholder*="Introdu" i]:not([type="hidden"])'
            );
        }}
        if (val) {{
            const unitBtn = row.querySelector('button[data-testid$=".unit"]');
            return {{
                kind: 'value',
                id: val.id || '',
                placeholder: val.getAttribute('placeholder') || '',
                currentValue: val.value || '',
                unit_testid: unitBtn ? unitBtn.getAttribute('data-testid') : '',
                unit_text: unitBtn ? (unitBtn.innerText || '').trim() : '',
            }};
        }}
        return {{ error: 'no_input' }};
    }}""")

    if not info or info.get("error"):
        print(f"      [DBG] '{label_text}': {(info or {}).get('error', 'null')}")
        return False

    # ── Value-input branch: fill the field, optionally adjust the unit ─────
    if info.get("kind") == "value":
        # Decide whether the field is numeric-with-unit (Rulaj "142000 km",
        # Putere "131 CP", Motor "1.5 l") or free-form text (VIN code, license
        # plates, custom strings). We only strip non-digits in the first case
        # — otherwise valid VINs like "JT2EL55D9N40020463" get mangled to
        # "20020463" and the form rejects them as "Valoare neacceptată".
        unit_testid = info.get("unit_testid", "")
        looks_numeric = bool(re.match(
            r"^\s*-?\d[\d\s.,]*(\s*[A-Za-zА-Яа-я%/³²]+)?\s*$", value
        ))
        if unit_testid or looks_numeric:
            fill_value = re.sub(r"[^\d.,]", "", value).strip().rstrip(".,")
            if not fill_value:
                print(f"      [DBG] '{label_text}': no digits in {value!r}")
                return False
        else:
            fill_value = value

        # Prefer id-based selector — IDs are stable; placeholders aren't.
        if info.get("id"):
            val_sel = f'input[id="{info["id"]}"]'
        else:
            ph = info.get("placeholder", "Introdu valoarea").replace('"', '\\"')
            val_sel = f'input[placeholder="{ph}"]'
        try:
            await page.locator(val_sel).first.fill(fill_value, timeout=2000)
        except Exception as e:
            print(f"      [DBG] '{label_text}': fill failed: {e}")
            return False

        # If there's a unit toggle and the scraped value names a different
        # unit than what's currently set, flip it. Light-touch: only act
        # when we can confidently map the value's unit text.
        unit_testid = info.get("unit_testid", "")
        if unit_testid:
            lv = value.lower()
            target = None
            if   "mi" in lv and "km" not in lv: target = "mi"
            elif "km" in lv:                    target = "km"
            elif "€" in value or "eur" in lv:   target = "€"
            elif "$" in value or "usd" in lv:   target = "$"
            elif "mdl" in lv or "lei" in lv:    target = "MDL"
            current = (info.get("unit_text") or "").strip()
            if target and target.lower() != current.lower():
                try:
                    await page.locator(f'button[data-testid="{unit_testid}"]').click(timeout=1500)
                    await page.wait_for_timeout(300)
                    await page.locator(
                        f'[role="option"]:text-is("{target}")'
                    ).first.click(timeout=1500)
                    await page.wait_for_timeout(200)
                except Exception:
                    pass
        return True

    # ── Listbox branch ──────────────────────────────────────────────────────
    testid = info["testid"]                       # e.g. "620-input"
    listbox_id = testid.rsplit("-", 1)[0]         # e.g. "620"

    # If the combobox already shows the desired value, skip clicking.
    if info["currentValue"].strip().lower() == value.lower():
        return True

    input_sel = f'input[data-testid="{testid}"]'

    # ── 3: Open the dropdown ──────────────────────────────────────────────────
    try:
        trigger = page.locator(input_sel).first
        await trigger.scroll_into_view_if_needed(timeout=1500)
        await trigger.click(timeout=2000)
    except Exception as e:
        print(f"      [DBG] '{label_text}': click trigger failed: {e}")
        return False

    # ── 4: Wait for the listbox + click the matching option ──────────────────
    safe_value = value.replace("'", "\\'").replace('"', '\\"')
    listbox_sel = f'#{listbox_id}[role="listbox"]'
    opt_sels = [
        f'{listbox_sel} [role="option"]:text-is("{safe_value}")',
        f'{listbox_sel} [role="option"]:has-text("{safe_value}")',
        # Fallback: any visible option (handles dropdowns whose listbox id
        # changes between open/close, e.g. when React remounts).
        f'[role="option"]:text-is("{safe_value}"):visible',
        f'[role="option"]:has-text("{safe_value}"):visible',
    ]

    for _ in range(8):  # up to ~4 s — covers slow dependent dropdowns
        for sel in opt_sels:
            try:
                opt = page.locator(sel).first
                if await opt.is_visible(timeout=300):
                    await opt.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                pass
        await page.wait_for_timeout(500)

    # Last-resort: try typing into the combobox (Regiune autocompletes this way)
    try:
        await page.locator(input_sel).first.fill(value[:8])
        await page.wait_for_timeout(700)
        for sel in opt_sels:
            try:
                opt = page.locator(sel).first
                if await opt.is_visible(timeout=400):
                    await opt.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                pass
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


# ── Currency toggle (€ | MDL | $) ────────────────────────────────────────────
async def select_currency(page, currency: str) -> bool:
    """
    Set the currency unit next to the price input. Anchor on the price
    INPUT itself (id "2.value" / placeholder "Introdu prețul") and climb to
    the wrapper that holds its `button[data-testid$=".unit"]` — the old
    label-anchored walk from the "Preț" title span landed on the wrong
    container on transport/cars, where the price block is a bare
    `styles_wrapper` outside any `field__row` (verified live 2026-07-16).
    The unit widget also carries `input[data-testid$=".unit-hidden-input"]`
    with values like UNIT_EUR — if it already matches, no click is needed
    (cars defaults to €, so EUR listings need no toggling at all).
    """
    cur = currency.upper()
    symbol = {"EUR": "€", "USD": "$", "MDL": "MDL"}.get(cur)
    if not symbol:
        return False

    info = await page.evaluate("""() => {
        const price = document.getElementById('2.value') ||
            document.querySelector('input[placeholder*="prețul" i], ' +
                                   'input[placeholder*="pretul" i]');
        if (!price) return {error: 'no_price_input'};
        let node = price;
        for (let i = 0; i < 8 && node; i++) {
            node = node.parentElement;
            if (!node) break;
            const btn = node.querySelector('button[data-testid$=".unit"]');
            if (btn) {
                const hidden = node.querySelector(
                    'input[data-testid$=".unit-hidden-input"]');
                return {testid: btn.getAttribute('data-testid'),
                        text: (btn.innerText || '').trim(),
                        hidden: hidden ? (hidden.value || '') : ''};
            }
        }
        return {error: 'no_unit_button'};
    }""")

    if not info or info.get("error"):
        print(f"      [DBG] currency: {(info or {}).get('error', 'null')}")
        return False

    # Already on the right unit? (hidden value UNIT_EUR/UNIT_USD/UNIT_MDL,
    # or the trigger visibly shows the symbol)
    if info.get("hidden", "").upper() == f"UNIT_{cur}" or \
            info.get("text", "").strip() == symbol:
        return True

    testid_sel = f'button[data-testid="{info["testid"]}"]'

    try:
        trigger = page.locator(testid_sel).first
        await trigger.scroll_into_view_if_needed(timeout=1500)
        await trigger.click(timeout=2000)
    except Exception:
        return False

    await page.wait_for_timeout(350)
    safe = symbol.replace("'", "\\'")
    for sel in (
        f'[role="option"]:text-is("{safe}")',
        f'[role="option"]:has-text("{safe}")',
    ):
        try:
            opt = page.locator(sel).first
            if await opt.is_visible(timeout=600):
                await opt.click(timeout=2000)
                await page.wait_for_timeout(200)
                return True
        except Exception:
            pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


# ── Tick a single feature checkbox by its label text ──────────────────────────
async def tick_checkbox(page, label: str) -> bool:
    """
    Tick a boolean feature checkbox by its visible label. On the new add
    form each checkbox is rendered as:
        <label class="…feature__boolean__label__…"><input type=checkbox> Text</label>
    Verified live on the cars add form 2026-05-14 (53 checkboxes, all in DOM).
    Skips items that are already ticked.
    """
    if not label:
        return False
    safe = label.replace("'", "\\'").replace('"', '\\"')
    sels = [
        f'label[class*="boolean__label"]:has-text("{safe}")',
        f'label[class*="checkbox__container"]:has-text("{safe}")',
        f'label:text-is("{safe}")',
    ]
    for sel in sels:
        try:
            el = page.locator(sel).first
            if not await el.is_visible(timeout=300):
                continue
            # If the inner checkbox is already checked, skip.
            try:
                cb = el.locator('input[type="checkbox"]').first
                if await cb.is_checked():
                    return True
            except Exception:
                pass
            try:
                await el.scroll_into_view_if_needed(timeout=500)
            except Exception:
                pass
            await el.click(timeout=1500)
            await page.wait_for_timeout(80)
            return True
        except Exception:
            pass
    return False


# ── License-plate hiding modal (cars) ─────────────────────────────────────────
async def dismiss_plate_modal(page) -> None:
    """
    After photo upload on a car listing, 999.md pops a modal asking whether
    to blur visible licence plates. The user wants plates kept visible, so
    we click any "Nu" button inside the dialog. Silent no-op otherwise.
    """
    end = asyncio.get_event_loop().time() + 7.0
    while asyncio.get_event_loop().time() < end:
        for sel in [
            "[role='dialog'] button:has-text('Nu')",
            "[role='dialog'] button:text-is('Nu')",
            "[class*='modal' i] button:has-text('Nu')",
            "button:has-text('Nu, păstrează')",
            "button:has-text('Nu, pastreaza')",
            "button:text-is('Nu')",
            "[role='dialog'] button:text-is('Нет')",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=300):
                    await btn.click(timeout=1000)
                    print("    Declined licence-plate blur prompt.")
                    await page.wait_for_timeout(400)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(400)


async def dismiss_onboarding(page) -> bool:
    """
    Kill the IntroJS coach-mark 999.md shows on a freshly opened add form
    ("Completează primii pași … Am înțeles"). Verified live 2026-08-15.

    Why this matters: `.introjs-tooltipReferenceLayer` sits at z-index 1e8
    over the form's FIRST field, so Playwright refuses the click with
    "<div class='introjs-tooltipReferenceLayer'> … intercepts pointer
    events". On phone-and-communication/mobile-phones that first field is
    "Tip dispozitiv" — and because Marcă/Model/Preț/Regiune only mount once
    it is selected, the whole form stayed at 50%, no publish button ever
    rendered, and `_find_submit` fell through to the header search button
    → "No publish button found".

    Prefer the tour's own "Am înțeles" anchor (that's what persists the
    seen-flag, so it stops coming back); then rip out any layer left over —
    `.introjs-tour` is a body-level sibling holding only tour chrome, it
    never contains the form, so removing it is safe. Returns True if
    anything was dismissed.
    """
    try:
        acted = await page.evaluate("""() => {
            let acted = 0;
            try {
                const btn = document.querySelector(
                    '.introjs-tooltipbuttons .introjs-nextbutton, '
                    + '.introjs-tooltipbuttons a, '
                    + '.introjs-tooltipbuttons button');
                if (btn) { btn.click(); acted++; }
            } catch (e) { /* tour mid-teardown */ }
            for (const sel of ['.introjs-overlay', '.introjs-helperLayer',
                               '.introjs-tooltipReferenceLayer',
                               '.introjs-disableInteraction', '.introjs-tour']) {
                for (const el of document.querySelectorAll(sel)) {
                    el.remove(); acted++;
                }
            }
            document.body.classList.remove('add-form-hint-active',
                                           'onboarding-active');
            for (const el of document.querySelectorAll('.introjs-showElement')) {
                el.classList.remove('introjs-showElement');
            }
            return acted;
        }""")
    except Exception:
        return False
    return bool(acted)

# ── Repost command ────────────────────────────────────────────────────────────
MAX_WIZARD_STEPS = 5   # transport/cars uses a multi-step form; most are 1 step

# Free vs paid is decided per ad, never per category. 999.md caps FREE ads
# per subcategory, per month, per account (Autoturisme in 2026-09: 1 a month,
# shown on the add form as "Au rămas N anunț gratuit") and deleting an ad
# does not give the slot back. The same car category was free on one account
# and paid on another the same day (2026-07-16), so the old hardcoded
# "transport/cars is paid" list skipped cars that could have gone out free.
# The user's hard rule stays: NEVER pay. We publish, read 999.md's answer
# (_classify_publish) and delete the ad again if it landed on the fee page.


async def delete_ad(page, ad_id: str, require_unpaid: bool = False) -> bool:
    """
    Delete one of our own ads from its owner-view page. Used to roll back
    an ad that 999.md only offered to publish for a fee (no free slot left
    this month) — it would otherwise sit unpaid in the cabinet.
    Owner toolbar buttons carry data-testids: publicate / edit / delete
    ("Ştergere") — verified live 2026-07-17 on the new design.

    require_unpaid: delete only when the owner page itself says "Anunțul nu
    este plătit" (verified live 2026-09-13). The fee decision comes from the
    redirect URL; this second signal makes sure a misread URL can never
    delete an ad that actually went live for free.
    """
    try:
        await page.goto(f"{BASE_URL}/ro/{ad_id}", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        if require_unpaid:
            unpaid = False
            for _ in range(6):                   # the ad body renders late
                body = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
                if re.search(r"nu\s+este\s+pl[ăa]tit", body, re.I):
                    unpaid = True
                    break
                await page.wait_for_timeout(1000)
            if not unpaid:
                print(f"    [WARN] Ad {ad_id} is not marked unpaid — NOT "
                      f"deleting it, check it by hand.")
                return False
        btn = page.locator('[data-testid="delete"]').first
        await btn.click(timeout=5000)
        await page.wait_for_timeout(1000)
        # Confirmation dialog — click the affirmative button. Its label uses
        # the CEDILLA spelling "Ştergere" (U+015E, not comma-below "Șterge",
        # verified live 2026-07-17) and the container has no role="dialog",
        # so match by regex over any visible button EXCEPT the owner
        # toolbar's own delete trigger.
        confirmed = False
        candidates = page.locator(
            'button:not([data-testid="delete"])',
            has_text=re.compile(r"^\s*[SŞȘsșş]terge(re)?\s*$", re.I))
        for _ in range(6):                       # dialog may mount slowly
            try:
                for i in range(await candidates.count()):
                    b = candidates.nth(i)
                    if await b.is_visible():
                        await b.click(timeout=2000)
                        confirmed = True
                        break
            except Exception:
                pass
            if confirmed:
                break
            await page.wait_for_timeout(500)
        if not confirmed:
            # Dump whatever dialog appeared so the log explains a miss.
            texts = await page.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll(
                    '[role="dialog"] button, [class*="modal" i] button')) {
                    const r = el.getBoundingClientRect();
                    if (r.width && r.height) out.push((el.innerText || '').trim());
                }
                return out.slice(0, 10);
            }""")
            print(f"    [WARN] Delete confirm dialog not matched; buttons={texts}")
        await page.wait_for_timeout(2000)
        # Verify: after deletion the owner toolbar (and its delete button)
        # disappears from the ad page.
        await page.goto(f"{BASE_URL}/ro/{ad_id}", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        gone = await page.locator('[data-testid="delete"]').count() == 0
        print(f"    Delete ad {ad_id}: {'✓ deleted' if gone else '✗ still exists'}")
        return gone
    except Exception as e:
        print(f"    [WARN] delete_ad({ad_id}) failed: {e}")
        return False


async def _visible_form_errors(page) -> list:
    """Collect visible validation/error texts currently shown on the form."""
    return await page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll(
            '[class*="error" i], [role="alert"]')) {
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            const t = (el.innerText || '').trim();
            if (t && t.length < 200) out.push(t);
        }
        return [...new Set(out)].slice(0, 15);
    }""")


async def _form_signature(page) -> str:
    """
    Fingerprint of the currently mounted form step: sorted visible field
    labels + input count. Changes when a wizard advances to its next step,
    stays identical when a "Continuați" click bounced off validation.
    """
    return await page.evaluate("""() => {
        const labels = [];
        for (const sp of document.querySelectorAll('span')) {
            if (!(sp.className || '').toString().includes('title__name')) continue;
            const r = sp.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            labels.push((sp.innerText || '').trim());
        }
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]), textarea').length;
        return labels.sort().join('|') + '#' + inputs;
    }""")


def _is_published(url: str) -> bool:
    """
    True once the browser left the add form for a page that carries the new
    ad's id: the classic success redirect (`…/<digits>`) or the promo
    "Ultimul pas" page (`/ro/services/<digits>?pageType=packages`).
    """
    last = url.split("?")[0].rstrip("/").split("/")[-1]
    return "success" in url or last.isdigit()


def _classify_publish(url: str) -> tuple:
    """
    (status, note) for a published-ad URL — this is where "free or paid" is
    decided. A free publication redirects with `freePublish=true` and the ad
    goes live immediately. With the subcategory's monthly free quota used up,
    999.md lands on the same /services page WITHOUT freePublish and offers
    only paid packages (Basic 4-5 MDL) — the ad exists but stays unpaid
    ("Trebuie să achitați" in cabinet → Toate). Verified live 2026-07-16
    and 2026-09-13.
    """
    if "/services/" in url and "freePublish=true" not in url:
        return ("needs_payment",
                "Anunț creat peste limita lunară gratuită — a rămas neachitat "
                "(cabinet → Toate → „Trebuie să achitați”).")
    return "success", ""


async def _find_submit(page):
    """Return (locator, text, data-testid) of the form's final CTA button."""
    submit = page.locator(
        'button:has-text("Publică anunțul"), '
        'button:has-text("Publică"), '
        'button:has-text("Continuați"), '
        'button:has-text("Continuati"), '
        'button[type="submit"]'
    ).last
    try:
        btn_text = (await submit.inner_text()).strip()
    except Exception:
        btn_text = ""
    try:
        testid = await submit.get_attribute("data-testid") or ""
    except Exception:
        testid = ""
    return submit, btn_text, testid


async def _fill_visible_fields(page, listing: dict, done: dict) -> int:
    """
    Fill every listing field that exists on the CURRENT form state and
    return how many NEW fields were completed. Successes are remembered in
    `done`: fields not yet in the DOM simply miss here ('·') and are picked
    up on a later pass; filled ones are never touched again. Callers run
    this to a fixpoint because most 999.md forms mount progressively —
    e.g. on mobile-phones, Marcă/Model/Preț/Regiune only appear after
    "Tip dispozitiv" is selected.

    The onboarding coach-mark is cleared first on every pass: it mounts a
    second or two after the form does, so a single up-front dismissal can
    race it, and it blocks the very first field it points at.
    """
    await dismiss_onboarding(page)
    start_count = len(done)

    async def once(key, coro_fn, desc):
        if done.get(key):
            return
        try:
            ok = await coro_fn()
        except Exception as e:
            print(f"    {desc} → ✗ ({e})")
            return
        if ok:
            done[key] = True
        print(f"    {desc} → {'✓' if ok else '·'}")

    # Region (always Chișinău)
    await once("region",
               lambda: fill_dropdown(page, "Regiune", "Chișinău"),
               "Regiune='Chișinău'")

    # Price + currency toggle
    price_num = re.sub(r"[^\d.]", "", listing.get("price", "") or "")
    if price_num:
        await once("price",
                   lambda: fill_input(page, "Introdu prețul", price_num),
                   f"Preț={price_num!r}")
    currency = (listing.get("currency") or "").upper()
    if currency and done.get("price"):
        await once("currency",
                   lambda: select_currency(page, currency),
                   f"Currency={currency}")

    # Characteristics
    for attr_key, attr_val in (listing.get("attributes", {}) or {}).items():
        if attr_key == "An de fabricație":
            await once(f"attr:{attr_key}",
                       lambda v=attr_val: fill_input(
                           page, "Introdu an de fabricație", v),
                       f"{attr_key}={attr_val!r}")
        else:
            await once(f"attr:{attr_key}",
                       lambda k=attr_key, v=attr_val: fill_dropdown(page, k, v),
                       f"{attr_key}={attr_val!r}")

    # Checkbox feature lists (Dotări, Securitate, …)
    for group_name, items in (listing.get("feature_lists", {}) or {}).items():
        for item in items:
            await once(f"feat:{group_name}:{item}",
                       lambda i=item: tick_checkbox(page, i),
                       f"[{group_name}] {item!r}")

    # Titles (RO + RU both required)
    if not done.get("titles"):
        title = listing.get("title", "Untitled")
        title_inputs = await page.query_selector_all(
            "input[placeholder*='titlul'], input[placeholder*='Titlul'], "
            "input[placeholder*='anunțului'], input[placeholder*='anuntului']"
        )
        if title_inputs:
            for inp in title_inputs:
                await inp.fill(title)
            done["titles"] = True
            print(f"    Titles filled: {len(title_inputs)} input(s)")

    # Description (RO + RU textareas)
    desc = listing.get("description", "")
    if desc and not done.get("description"):
        # Defensive strip — older listings.json caches (scraped before we
        # anchored to `description__body`) end with the trailing "Citește
        # tot" expander label baked into the description text.
        desc = re.sub(
            r"\n?\s*(Cite[șs]te\s+(tot|mai\s+mult)|Show\s+more|Read\s+more|"
            r"Показать\s+(всё|все)|Подробнее|Читать\s+далее)\s*$",
            "",
            desc,
            flags=re.IGNORECASE,
        ).rstrip()
        desc_areas = await page.query_selector_all(
            "textarea[placeholder*='detalii'], textarea[placeholder*='Detalii']"
        )
        if desc_areas:
            for ta in desc_areas:
                await ta.fill(desc)
            done["description"] = True
            print(f"    Description filled: {len(desc_areas)} textarea(s)")

    # Photos — upload exactly once, whichever step exposes the file input
    if not done.get("photos"):
        local_images = [p for p in listing.get("local_images", [])
                        if Path(p).exists()]
        if local_images:
            try:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count():
                    await file_input.set_input_files(local_images[:20])
                    await page.wait_for_timeout(3000)
                    done["photos"] = True
                    print(f"    Photos: {len(local_images)} uploaded")
                    # Car listings show a modal asking whether to blur the
                    # licence plates — always decline (keep plates visible).
                    await dismiss_plate_modal(page)
            except Exception as e:
                print(f"    [WARN] Photo upload failed: {e}")

    return len(done) - start_count


async def repost_listing(page, listing: dict, dry_run: bool = False) -> dict:
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

    log_entry = {
        "original_url": listing.get("url", ""),
        "title": title,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "new_url": "",
        "error": "",
    }

    await page.goto(add_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    # An expired session bounces /ro/add to the Simpals login page. Without
    # this guard the code "fills" the login page (every field fails) and even
    # clicks its "Intră" button as the submit — re-login and come back instead.
    if "simpalsid.com" in page.url:
        print("    Session lost — re-logging in …")
        try:
            await login(page)
        except RuntimeError as e:
            log_entry["status"] = "error"
            log_entry["error"] = str(e)
            print(f"    [ERROR] {e}")
            return log_entry
        await page.goto(add_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    # Wait for the add form to actually mount before filling anything.
    # Two deliberate choices here:
    #   • accept ANY field label (`title__name`), not just "Preț" — a
    #     multi-step wizard's first step may not contain the price field;
    #   • retry with a reload — the SPA sometimes hangs on first load
    #     (this was the intermittent "Add form did not load" failure on
    #     phone-and-communication/mobile-phones).
    form_ok = False
    for attempt in range(1, 4):
        for _ in range(10):
            if await page.locator('span[class*="title__name"]').count():
                form_ok = True
                break
            await page.wait_for_timeout(1000)
        if form_ok:
            break
        print(f"    Form not mounted (attempt {attempt}/3) — reloading …")
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
    if not form_ok:
        log_entry["status"] = "error"
        log_entry["error"] = f"Add form did not load (stuck at {page.url})"
        print(f"    [ERROR] {log_entry['error']}")
        return log_entry

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

        # ── Fill the form, step by step ──────────────────────────────────────
        # Single-step categories (parts, phones, …) publish on the first
        # pass. transport/cars labels its submit "Continuați" and the click
        # itself creates the ad (payment step follows) — so after every
        # advance we first check whether we already landed on a published-ad
        # URL before touching anything else.
        filled: dict = {}
        submit = None
        btn_text = ""
        btn_testid = ""
        for step in range(1, MAX_WIZARD_STEPS + 1):
            if _is_published(page.url):
                break
            # The add form's IntroJS coach-mark overlays (and blocks
            # clicks on) the first field of the step — clear it before
            # touching anything.
            await dismiss_onboarding(page)
            # Some categories hide most checkbox features behind an
            # "Afișează totul" expander — open before filling each step.
            await expand_collapsibles(page)
            # Fill to a fixpoint: selecting one dropdown often mounts more
            # fields (mobile-phones shows only "Tip dispozitiv" at first).
            for _pass in range(6):
                if await _fill_visible_fields(page, listing, filled) == 0:
                    break
                await page.wait_for_timeout(500)
            submit, btn_text, btn_testid = await _find_submit(page)
            if not re.search(r"continua[țt]i", btn_text, re.I):
                break
            print(f"    Step {step}: CTA is {btn_text!r} — advancing …")
            sig_before = await _form_signature(page)
            await submit.click()
            # The step advanced when we left the form (ad created) or the
            # visible field set changed; an unchanged form means validation
            # bounced the click.
            advanced = False
            for _ in range(10):
                await page.wait_for_timeout(1000)
                if _is_published(page.url) or \
                        await _form_signature(page) != sig_before:
                    advanced = True
                    break
            if not advanced:
                errs = await _visible_form_errors(page)
                log_entry["status"] = "incomplete"
                log_entry["error"] = (
                    f"Wizard stuck at step {step}: "
                    + ("; ".join(errs) if errs
                       else "form unchanged after Continuați click"))
                print(f"    [ERROR] {log_entry['error']}")
                return log_entry
        else:
            log_entry["status"] = "incomplete"
            log_entry["error"] = (f"CTA still reads {btn_text!r} after "
                                  f"{MAX_WIZARD_STEPS} wizard steps")
            print(f"    [ERROR] {log_entry['error']}")
            return log_entry

        unfilled = [k for k in
                    ([f"attr:{a}" for a in (listing.get("attributes") or {})]
                     + ["region", "titles"])
                    if not filled.get(k)]
        if unfilled and not _is_published(page.url):
            print(f"    [WARN] Unfilled after all steps: {unfilled}")

        # ── Submit & outcome ─────────────────────────────────────────────────
        if not _is_published(page.url):
            if dry_run:
                log_entry["status"] = "dry_run"
                log_entry["error"] = (f"DRY RUN — stopped before {btn_text!r}; "
                                      f"unfilled: {unfilled or 'none'}")
                print(f"    [DRY RUN] Final CTA {btn_text!r}; not clicked.")
                return log_entry
            # Never "submit" via the site header's search button — that's
            # the button[type=submit] fallback matching when a form has no
            # real CTA (e.g. an unexpected page).
            if btn_testid == "header-search-button" or not btn_text:
                log_entry["status"] = "incomplete"
                log_entry["error"] = (f"No publish button found "
                                      f"(page: {page.url})")
                print(f"    [ERROR] {log_entry['error']}")
                return log_entry
            await submit.click()

        # Watch the outcome for up to ~24 s: either 999.md redirects to the
        # success page / new listing URL, or validation errors surface on the
        # form. Capture those errors verbatim so the log says WHY it failed.
        form_errors: list[str] = []
        new_url = page.url
        for _ in range(8):
            if _is_published(new_url):
                break
            await page.wait_for_timeout(3000)
            new_url = page.url
            if _is_published(new_url):
                break
            form_errors = await _visible_form_errors(page)
            if form_errors:
                break

        if _is_published(new_url):
            log_entry["status"], note = _classify_publish(new_url)
            log_entry["error"] = note
            if log_entry["status"] == "needs_payment":
                # No free slot left: the ad already exists but can only go
                # live for money — delete it so no unpaid ad piles up. The id
                # is the LAST path segment; the old "first digits in the URL"
                # search matched "999" in the domain and deleted nothing (the
                # 2026-09-03 unpaid ads were left behind that way).
                ad_id = new_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                if ad_id.isdigit() and await delete_ad(page, ad_id,
                                                       require_unpaid=True):
                    log_entry["status"] = "skipped_paid"
                    log_entry["error"] = (
                        "Fără anunț gratuit disponibil luna asta în această "
                        "subcategorie — anunțul creat a fost șters automat "
                        "(nu plătim).")
        elif form_errors:
            log_entry["status"] = "incomplete"
            log_entry["error"] = "Form validation: " + "; ".join(form_errors)
        else:
            log_entry["status"] = "incomplete"
            log_entry["error"] = f"No success redirect after submit (stuck at {new_url})"
        log_entry["new_url"] = new_url
        print(f"    → {new_url}  [{log_entry['status']}]"
              + (f"  ({log_entry['error']})" if log_entry["error"] else ""))

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
