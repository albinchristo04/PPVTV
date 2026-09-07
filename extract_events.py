#!/usr/bin/env python3

"""
EVaultHub Events Extractor
Fetches the PPV events API with a primary and fallback host.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

# Try the current host first, then the documented fallback.
API_URLS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.st/api/streams",
]
OUTPUT_FILE = "events.json"

TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5


def create_session():
    """Create a normal HTTP session with browser-like headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://ppv.to/",
        "Origin": "https://ppv.to",
    })
    return session


def fetch_events():
    """Fetch events from the primary API, then the fallback host."""
    session = create_session()

    for api_url in API_URLS:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"\nAttempt {attempt}/{MAX_RETRIES}")
                print(f"Fetching: {api_url}")

                response = session.get(api_url, timeout=TIMEOUT)
                print(f"Status Code: {response.status_code}")

                response.raise_for_status()
                data = response.json()

                print(f"✓ Successfully fetched events from {api_url}")
                return data, api_url

            except requests.exceptions.SSLError as exc:
                print(f"✗ TLS/SSL error: {exc}")
                # Retrying the same TLS endpoint is unlikely to help; move
                # immediately to the fallback host.
                break

            except requests.exceptions.RequestException as exc:
                print(f"✗ Attempt {attempt} failed: {exc}")
                if attempt < MAX_RETRIES:
                    print(f"Retrying in {RETRY_DELAY} seconds...")
                    time.sleep(RETRY_DELAY)

            except ValueError as exc:
                print(f"✗ Invalid JSON response: {exc}")
                break

    print("✗ All API endpoints failed")
    return None, None


def save_to_json(data, filename=OUTPUT_FILE):
    """Save JSON safely using an atomic replace."""
    try:
        temp_file = filename + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, filename)
        print(f"✓ Saved to {filename}")
        return True
    except Exception as exc:
        print(f"✗ Save failed: {exc}")
        return False


def prepare_output(events_data, source_url):
    """Prepare structured output."""
    return {
        "metadata": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": source_url,
            "total_events": len(events_data) if isinstance(events_data, list) else None,
        },
        "events": events_data,
    }


def print_summary(events_data):
    """Print summary info."""
    print("\n========== SUMMARY ==========")

    if isinstance(events_data, list):
        print(f"Total events: {len(events_data)}")
        if events_data and isinstance(events_data[0], dict):
            name = events_data[0].get("title") or events_data[0].get("name")
            if name:
                print(f"First event: {name}")
    elif isinstance(events_data, dict):
        print(f"Keys: {', '.join(events_data.keys())}")

    print("=============================\n")


def main():
    print("=" * 50)
    print("EVaultHub Events Extractor")
    print("=" * 50)

    events_data, source_url = fetch_events()

    if events_data is None:
        print("✗ Failed to fetch events")
        raise SystemExit(1)

    output_data = prepare_output(events_data, source_url)

    if not save_to_json(output_data):
        raise SystemExit(1)

    print_summary(events_data)

    print("✓ Completed successfully")
    print(f"Source used: {source_url}")
    print("=" * 50)


if __name__ == "__main__":
    main()
