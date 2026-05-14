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
    Click the currency unit trigger that sits next to the price input and
    pick the matching option. The price's unit trigger is the first
    `button[data-testid$=".unit"][aria-haspopup="listbox"]` in the form
    DOM order (verified live 2026-05-14 — other `.unit` triggers like
    "Prima rată" / "Rulaj" come later in the page).
    """
    symbol = {"EUR": "€", "USD": "$", "MDL": "MDL"}.get(currency.upper())
    if not symbol:
        return False

    # Locate the price field's unit trigger via the JS helper used by
    # fill_dropdown — anchor to the "Preț" title span, walk up to the
    # field row, look for the `.unit` button inside.
    testid = await page.evaluate("""() => {
        let title = null;
        for (const sp of document.querySelectorAll('span')) {
            const t = (sp.innerText || '').trim().replace(/\\s*\\*$/, '').trim();
            if (t !== 'Preț') continue;
            if (!(sp.className || '').toString().includes('title__name')) continue;
            title = sp;
            break;
        }
        if (!title) return null;
        let row = title;
        for (let i = 0; i < 10; i++) {
            row = row.parentElement;
            if (!row) break;
            const cls = (row.className || '').toString();
            if (cls.includes('field__row__86QTX') ||
                (cls.includes('field__row') && !cls.includes('field__row__title'))) break;
        }
        if (!row) return null;
        const btn = row.querySelector('button[data-testid$=".unit"][aria-haspopup="listbox"]');
        return btn ? btn.getAttribute('data-testid') : null;
    }""")

    if not testid:
        # Fallback: first unit trigger on the page (price is first in DOM).
        testid_sel = 'button[data-testid$=".unit"][aria-haspopup="listbox"]'
    else:
        testid_sel = f'button[data-testid="{testid}"]'

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

    # Some categories (esp. transport/cars) hide most of their checkbox
    # features behind an "Afișează totul" / "Mai multe" expander on the add
    # form. Click it before we start filling so the targets exist in the DOM.
    await expand_collapsibles(page)

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

        # ── Currency (€ / MDL / $ toggle next to the price) ─────────────────
        currency = (listing.get("currency") or "").upper()
        if currency:
            cur_clicked = await select_currency(page, currency)
            print(f"    Currency={currency} → {'✓' if cur_clicked else '✗'}")

        # ── Characteristics ──────────────────────────────────────────────────
        attrs = listing.get("attributes", {})
        for attr_key, attr_val in attrs.items():
            if attr_key == "An de fabricație":
                ok = await fill_input(page, "Introdu an de fabricație", attr_val)
            else:
                ok = await fill_dropdown(page, attr_key, attr_val)
            print(f"    {attr_key}={attr_val!r} → {'✓' if ok else '✗'}")

        # ── Checkbox feature lists (Dotări, etc.) ────────────────────────────
        feat_lists = listing.get("feature_lists", {}) or {}
        if feat_lists:
            # Ensure any "Afișează totul" inside characteristic sections is open
            await expand_collapsibles(page)
        for group_name, items in feat_lists.items():
            for item in items:
                ticked = await tick_checkbox(page, item)
                print(f"    [{group_name}] {item!r} → {'✓' if ticked else '✗'}")

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
            # Defensive strip — older listings.json caches (scraped before
            # we anchored to `description__body`) end with the trailing
            # "Citește tot" expander label baked into the description text.
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

            # Car listings show a modal asking whether to blur the licence
            # plates. We always decline (user wants the plates kept visible).
            await dismiss_plate_modal(page)

        # ── Submit ───────────────────────────────────────────────────────────
        submit = page.locator(
            'button:has-text("Publică anunțul"), '
            'button:has-text("Publică"), '
            'button:has-text("Continuați"), '
            'button:has-text("Continuati"), '
            'button[type="submit"]'
        ).last

        # If the final CTA is "Continuați", the form needs more wizard steps
        # than we support — bail out instead of leaving a half-filled draft.
        try:
            btn_text = (await submit.inner_text()).strip()
        except Exception:
            btn_text = ""
        if re.search(r"continua[țt]i", btn_text, re.I):
            log_entry["status"] = "skipped_continuati"
            log_entry["error"] = f"Submit button reads {btn_text!r} — multi-step form, skipped"
            print(f"    [SKIP] Submit reads {btn_text!r}; skipping listing.")
            return log_entry

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
