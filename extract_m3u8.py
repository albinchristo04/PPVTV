#!/usr/bin/env python3

"""
EVaultHub M3U8 Extractor

Reads events.json, visits each event iframe, discovers an M3U8 playlist URL,
and writes the result to events_with_m3u8.json without modifying events.json.

The extractor first checks the iframe HTML for obvious playlist URLs and then
falls back to Playwright for JavaScript-generated URLs.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

INPUT_FILE = "events.json"
OUTPUT_FILE = "events_with_m3u8.json"

REQUEST_TIMEOUT = 20
PLAYWRIGHT_TIMEOUT = 25_000
MAX_CONCURRENT = 4
RETRY_COUNT = 2
RETRY_DELAY = 2

M3U8_RE = re.compile(
    r"https?://[^\"'<>\\\s]+?\.m3u8(?:\?[^\"'<>\\\s]*)?",
    re.IGNORECASE,
)


def load_events():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def find_m3u8(text, base_url=None):
    """Find the first usable M3U8 URL in text."""
    if not text:
        return None

    # Normalize common JS/HTML escaping.
    text = (
        text.replace(r"\\/", "/")
        .replace(r"\u0026", "&")
        .replace(r"\u003d", "=")
        .replace("&amp;", "&")
    )

    match = M3U8_RE.search(text)
    if match:
        return match.group(0).rstrip("\\\"' )],;")

    # Also support relative playlist paths.
    relative = re.search(
        r"[\"']([^\"']+\.m3u8(?:\?[^\"']*)?)[\"']",
        text,
        re.IGNORECASE,
    )
    if relative and base_url:
        return urljoin(base_url, relative.group(1))

    return None


def http_extract(iframe_url):
    """Try extracting the playlist directly from iframe HTML."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://ppv.to/",
    }

    response = requests.get(
        iframe_url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return find_m3u8(response.text, iframe_url)


async def playwright_extract(page, iframe_url):
    """Load the iframe in a real browser and capture playlist requests/responses."""
    found = []

    def remember(url):
        if url and ".m3u8" in url.lower() and url not in found:
            found.append(url)

    page.on("request", lambda request: remember(request.url))
    page.on("response", lambda response: remember(response.url))

    try:
        await page.goto(
            iframe_url,
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_TIMEOUT,
        )
    except PlaywrightTimeoutError:
        # A video page may continue loading indefinitely; captured requests
        # are still useful, so continue instead of treating this as fatal.
        pass

    # Give player JavaScript a little time to initialize.
    await page.wait_for_timeout(5000)

    if found:
        return found[0]

    # Inspect the rendered HTML and scripts as a second fallback.
    try:
        html = await page.content()
        result = find_m3u8(html, iframe_url)
        if result:
            return result
    except Exception:
        pass

    return None


async def extract_one(browser, semaphore, item, index, total):
    iframe_url = item.get("iframe")
    if not iframe_url:
        item["m3u8"] = None
        item["m3u8_status"] = "no_iframe"
        return

    async with semaphore:
        print(f"[{index}/{total}] {item.get('name', item.get('title', 'Unnamed'))}")
        print(f"  iframe: {iframe_url[:120]}...")

        # Fast path: ordinary HTTP request.
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                result = await asyncio.to_thread(http_extract, iframe_url)
                if result:
                    item["m3u8"] = result
                    item["m3u8_status"] = "found_http"
                    print(f"  ✓ M3U8 found via HTTP")
                    return
                break
            except Exception as exc:
                print(f"  HTTP attempt {attempt} failed: {exc}")
                if attempt < RETRY_COUNT:
                    await asyncio.sleep(RETRY_DELAY)

        # Browser fallback for JavaScript-generated players.
        page = await browser.new_page()
        try:
            result = await playwright_extract(page, iframe_url)
            if result:
                item["m3u8"] = result
                item["m3u8_status"] = "found_playwright"
                print("  ✓ M3U8 found via Playwright")
            else:
                item["m3u8"] = None
                item["m3u8_status"] = "not_found"
                print("  ✗ M3U8 not found")
        except Exception as exc:
            item["m3u8"] = None
            item["m3u8_status"] = "error"
            item["m3u8_error"] = str(exc)
            print(f"  ✗ Playwright failed: {exc}")
        finally:
            await page.close()


def get_stream_items(data):
    """Return all stream dictionaries while preserving their original structure."""
    events = data.get("events") if isinstance(data, dict) else data
    items = []

    if isinstance(events, dict):
        categories = events.get("streams", [])
    elif isinstance(events, list):
        categories = events
    else:
        categories = []

    for category in categories:
        if not isinstance(category, dict):
            continue
        for item in category.get("streams", []):
            if isinstance(item, dict):
                items.append(item)

    return items


async def main_async():
    if not os.path.exists(INPUT_FILE):
        raise SystemExit(f"Missing {INPUT_FILE}")

    data = load_events()
    items = get_stream_items(data)

    print("=" * 60)
    print("EVaultHub M3U8 Extractor")
    print("=" * 60)
    print(f"Input: {INPUT_FILE}")
    print(f"Streams: {len(items)}")
    print(f"Output: {OUTPUT_FILE}")

    if not items:
        print("✗ No stream items found in events.json")
        raise SystemExit(1)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )

        try:
            tasks = [
                extract_one(browser, semaphore, item, i, len(items))
                for i, item in enumerate(items, 1)
            ]
            await asyncio.gather(*tasks)
        finally:
            await browser.close()

    data["m3u8_metadata"] = {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "source_file": INPUT_FILE,
        "total_streams": len(items),
        "found": sum(1 for item in items if item.get("m3u8")),
    }

    temp_file = OUTPUT_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_file, OUTPUT_FILE)

    found = sum(1 for item in items if item.get("m3u8"))
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total streams: {len(items)}")
    print(f"M3U8 found:    {found}")
    print(f"Not found:     {len(items) - found}")
    print(f"✓ Saved:       {OUTPUT_FILE}")
    print("=" * 60)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
