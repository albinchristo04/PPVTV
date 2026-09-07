#!/usr/bin/env python3
"""Advanced M3U8 discovery for public event iframe pages.

Reads events.json and writes events_with_m3u8.json.  It uses several
independent discovery techniques:
  1. HTML/source and decoded/escaped strings
  2. Recursive iframe discovery
  3. Browser network requests/responses
  4. Performance/resource entries exposed by the page
  5. Common player/config objects and data-* attributes
  6. JS URL candidates discovered from scripts

It does not attempt to bypass DRM, authentication, paywalls, or other
access controls.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

INPUT_FILE = "events.json"
OUTPUT_FILE = "events_with_m3u8.json"
REQUEST_TIMEOUT = 20
PLAYWRIGHT_TIMEOUT = 30_000
MAX_CONCURRENT = 3
RETRY_COUNT = 2
RETRY_DELAY = 2
MAX_IFRAMES = 8

ABS_M3U8_RE = re.compile(r"https?://[^\"'<>\\\s]+?\.m3u8(?:\?[^\"'<>\\\s]*)?", re.I)
REL_M3U8_RE = re.compile(r"(?:^|[\"'`=:\s(])([^\"'`<>\s]+?\.m3u8(?:\?[^\"'`<>\s]*)?)", re.I)
URLISH_RE = re.compile(r"https?://[^\"'<>\\\s]+", re.I)


def normalize_text(text):
    if not text:
        return ""
    replacements = {
        r"\\/": "/",
        r"\u0026": "&",
        r"\u003d": "=",
        r"\u003f": "?",
        r"\u002F": "/",
        r"\x2f": "/",
        r"\x26": "&",
        "&amp;": "&",
        "\\u0026": "&",
        "\\u003d": "=",
        "\\u003f": "?",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def clean_url(url):
    if not url:
        return None
    url = normalize_text(url).strip().strip("\\\"'` )],;>")
    if url.startswith("//"):
        url = "https:" + url
    return url if ".m3u8" in url.lower() else None


def find_m3u8(text, base_url=None):
    text = normalize_text(text)
    if not text:
        return None

    match = ABS_M3U8_RE.search(text)
    if match:
        return clean_url(match.group(0))

    match = REL_M3U8_RE.search(text)
    if match:
        candidate = match.group(1)
        if base_url:
            return clean_url(urljoin(base_url, candidate))

    # Last pass: look at URL-like strings and decode obvious JSON escaping.
    for candidate in URLISH_RE.findall(text):
        candidate = clean_url(candidate)
        if candidate:
            return candidate
    return None


def http_extract(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://ppv.to/",
    }
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    result = find_m3u8(response.text, url)
    if result:
        return result, "html"

    # Some pages put the actual player URL in an iframe src.
    iframe_srcs = re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", response.text, re.I)
    for src in iframe_srcs[:MAX_IFRAMES]:
        nested = urljoin(url, normalize_text(src))
        try:
            r = requests.get(nested, headers={**headers, "Referer": url}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            result = find_m3u8(r.text, nested)
            if result:
                return result, "nested_html"
        except Exception:
            continue
    return None, None


async def extract_from_page(page, url):
    found = []

    def remember(candidate):
        candidate = clean_url(candidate)
        if candidate and candidate not in found:
            found.append(candidate)

    def on_request(request):
        remember(request.url)

    def on_response(response):
        remember(response.url)

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)
    except PlaywrightTimeoutError:
        pass

    # Let delayed player initialization/network calls happen.
    await page.wait_for_timeout(7000)

    if found:
        return found[0], "network"

    # Collect browser resource URLs. This catches resources that were loaded
    # before event listeners were attached or via fetch/XHR.
    try:
        resources = await page.evaluate("""
            () => performance.getEntriesByType('resource').map(x => x.name)
        """)
        for resource in resources or []:
            remember(resource)
    except Exception:
        pass
    if found:
        return found[0], "performance"

    # Search DOM, inline scripts, data attributes and player configuration.
    try:
        candidates = await page.evaluate("""
            () => {
              const out = [];
              const push = x => { if (typeof x === 'string') out.push(x); };
              push(document.documentElement?.outerHTML || '');
              for (const s of document.scripts) push(s.textContent || '');
              for (const e of document.querySelectorAll('*')) {
                for (const a of e.attributes || []) push(a.value || '');
              }
              for (const k of ['playerConfig','player','config','video','source','stream','sources']) {
                try { push(JSON.stringify(window[k])); } catch (_) {}
              }
              return out;
            }
        """)
        for text in candidates or []:
            result = find_m3u8(text, url)
            if result:
                return result, "page_data"
    except Exception:
        pass

    # Inspect nested iframes. A common layout is iframe -> player iframe -> stream.
    try:
        frames = page.frames
        for frame in frames[1:MAX_IFRAMES + 1]:
            try:
                html = await frame.content()
                result = find_m3u8(html, frame.url or url)
                if result:
                    return result, "nested_frame"
                resources = await frame.evaluate("""
                    () => performance.getEntriesByType('resource').map(x => x.name)
                """)
                for resource in resources or []:
                    result = clean_url(resource)
                    if result:
                        return result, "nested_performance"
            except Exception:
                continue
    except Exception:
        pass

    return None, None


async def extract_one(browser, semaphore, item, index, total):
    iframe_url = item.get("iframe")
    if not iframe_url:
        item["m3u8"] = None
        item["m3u8_status"] = "no_iframe"
        return

    async with semaphore:
        name = item.get("name", item.get("title", "Unnamed"))
        print(f"[{index}/{total}] {name}")
        print(f"  iframe: {iframe_url[:140]}")

        for attempt in range(1, RETRY_COUNT + 1):
            try:
                result, method = await asyncio.to_thread(http_extract, iframe_url)
                if result:
                    item["m3u8"] = result
                    item["m3u8_status"] = "found_" + method
                    print(f"  ✓ M3U8 found via {method}")
                    return
                break
            except Exception as exc:
                print(f"  HTTP attempt {attempt} failed: {exc}")
                if attempt < RETRY_COUNT:
                    await asyncio.sleep(RETRY_DELAY)

        page = await browser.new_page(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        )
        try:
            result, method = await extract_from_page(page, iframe_url)
            if result:
                item["m3u8"] = result
                item["m3u8_status"] = "found_" + method
                print(f"  ✓ M3U8 found via {method}")
            else:
                item["m3u8"] = None
                item["m3u8_status"] = "not_found"
                print("  ✗ M3U8 not found after advanced discovery")
        except Exception as exc:
            item["m3u8"] = None
            item["m3u8_status"] = "error"
            item["m3u8_error"] = str(exc)
            print(f"  ✗ Browser failed: {exc}")
        finally:
            await page.close()


def get_stream_items(data):
    events = data.get("events") if isinstance(data, dict) else data
    categories = events.get("streams", []) if isinstance(events, dict) else events
    items = []
    for category in categories or []:
        if isinstance(category, dict):
            items.extend(x for x in category.get("streams", []) if isinstance(x, dict))
    return items


async def main_async():
    if not os.path.exists(INPUT_FILE):
        raise SystemExit(f"Missing {INPUT_FILE}")

    data = json.load(open(INPUT_FILE, "r", encoding="utf-8"))
    items = get_stream_items(data)

    print("=" * 60)
    print("ADVANCED M3U8 EXTRACTOR")
    print("=" * 60)
    print(f"Streams: {len(items)}")

    if not items:
        raise SystemExit("No stream items found in events.json")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            await asyncio.gather(*[
                extract_one(browser, semaphore, item, i, len(items))
                for i, item in enumerate(items, 1)
            ])
        finally:
            await browser.close()

    found = sum(1 for item in items if item.get("m3u8"))
    data["m3u8_metadata"] = {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "source_file": INPUT_FILE,
        "total_streams": len(items),
        "found": found,
        "not_found": len(items) - found,
        "methods": "HTTP HTML + nested iframe + browser network + performance + page data + nested frames",
    }

    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUTPUT_FILE)

    print("=" * 60)
    print(f"Total streams: {len(items)}")
    print(f"M3U8 found:    {found}")
    print(f"Not found:     {len(items) - found}")
    print(f"Saved:         {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main_async())
