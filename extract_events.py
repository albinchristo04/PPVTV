#!/usr/bin/env python3

"""
EVaultHub Events Extractor
Cloudflare-bypass version using cloudscraper
"""

import json
import time
import os
from datetime import datetime

import cloudscraper

# Configuration
API_URL = "https://api.ppv.to/api/streams"
OUTPUT_FILE = "events.json"

TIMEOUT = 30
MAX_RETRIES = 5
RETRY_DELAY = 5


def create_scraper():
    """Create Cloudflare-bypass scraper"""

    scraper = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False,
        }
    )

    scraper.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://ppv.to/",
        "Origin": "https://ppv.to",
        "Connection": "keep-alive",
    })

    return scraper


def fetch_events(api_url):
    """Fetch events with retry + Cloudflare bypass"""

    scraper = create_scraper()

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            print(f"\nAttempt {attempt}/{MAX_RETRIES}")
            print(f"Fetching: {api_url}")

            response = scraper.get(
                api_url,
                timeout=TIMEOUT
            )

            print(f"Status Code: {response.status_code}")

            if response.status_code != 200:
                raise Exception(f"Bad status code: {response.status_code}")

            data = response.json()

            print("✓ Successfully fetched events")
            return data

        except Exception as e:

            print(f"✗ Attempt {attempt} failed: {e}")

            if attempt < MAX_RETRIES:
                print(f"Retrying in {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
            else:
                print("✗ All retries failed")
                return None


def save_to_json(data, filename=OUTPUT_FILE):
    """Save JSON safely"""

    try:

        temp_file = filename + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        os.replace(temp_file, filename)

        print(f"✓ Saved to {filename}")
        return True

    except Exception as e:

        print(f"✗ Save failed: {e}")
        return False


def prepare_output(events_data):
    """Prepare structured output"""

    return {
        "metadata": {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source": API_URL,
            "total_events": len(events_data) if isinstance(events_data, list) else None,
        },
        "events": events_data
    }


def print_summary(events_data):
    """Print summary info"""

    print("\n========== SUMMARY ==========")

    if isinstance(events_data, list):

        print(f"Total events: {len(events_data)}")

        if len(events_data) > 0:

            first = events_data[0]

            if isinstance(first, dict):

                name = first.get("title") or first.get("name")

                if name:
                    print(f"First event: {name}")

    elif isinstance(events_data, dict):

        print(f"Keys: {', '.join(events_data.keys())}")

    print("=============================\n")


def main():

    print("=" * 50)
    print("EVaultHub Events Extractor")
    print("=" * 50)

    events_data = fetch_events(API_URL)

    if not events_data:
        print("✗ Failed to fetch events")
        exit(1)

    output_data = prepare_output(events_data)

    success = save_to_json(output_data)

    if not success:
        exit(1)

    print_summary(events_data)

    print("✓ Completed successfully")
    print("=" * 50)


if __name__ == "__main__":
    main()
