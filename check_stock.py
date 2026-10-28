"""
Target Pokemon TCG stock checker.

Checks store-level inventory for a list of Target products at a list of stores,
and posts to a Discord channel when something comes into stock (and when it sells out).

Runs on a schedule via GitHub Actions. Needs one secret: DISCORD_WEBHOOK_URL.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# What to watch. Edit these lists to add/remove products or stores.
# Product IDs (TCINs) are the number at the end of a Target link: .../A-1010892076
# ---------------------------------------------------------------------------
PRODUCTS = {
    "1010892076": "30th Celebration Elite Trainer Box",
    "1012422107": "30th Celebration Mini Tin",
    "1011407490": "30th Celebration Booster Bundle",
    "1010892070": "30th Celebration Knock Out Collection",
    "94411686": "Charizard ex Special Collection",
}

STORES = {
    "2340": "Burnsville",
    "360": "Eagan",
    "1484": "Lakeville",
    "643": "Apple Valley (Cedar Ave)",
    "2390": "Apple Valley South (Pilot Knob)",
    "1833": "Savage",
    "5": "Bloomington (79th & Penn)",
    "220": "Eden Prairie",
    "1456": "Rogers",
}

ZIP = "55337"
STATE = "MN"
SECONDS_BETWEEN_STORES = 5  # Target briefly blocks rapid-fire requests; keep this >= 5
STATE_FILE = Path(__file__).with_name("state.json")

IN_STOCK_STATUSES = {"IN_STOCK", "LIMITED_STOCK"}

FULFILLMENT_URL = (
    "https://redsky.target.com/redsky_aggregations/v1/web/"
    "product_summary_with_fulfillment_v1"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.target.com",
    "Referer": "https://www.target.com/",
}


def log(msg):
    print(msg, flush=True)


def get_api_key(session):
    """Target's website embeds a public site key in its homepage. Grab it fresh each run."""
    override = os.environ.get("TARGET_API_KEY")
    if override:
        return override
    html = session.get("https://www.target.com/", headers={**HEADERS, "Accept": "text/html"}, timeout=30).text
    match = re.search(r'apiKey\\?"\s*:\s*\\?"([A-Za-z0-9]{32,48})', html)
    if not match:
        raise RuntimeError("Couldn't find Target's site key on the homepage (page layout may have changed).")
    return match.group(1)


def check_store(session, key, store_id):
    params = {
        "key": key,
        "tcins": ",".join(PRODUCTS),
        "store_id": store_id,
        "required_store_id": store_id,
        "scheduled_delivery_store_id": store_id,
        "zip": ZIP,
        "state": STATE,
        "paid_membership": "false",
        "base_membership": "false",
        "card_membership": "false",
        "channel": "WEB",
    }
    for attempt in range(2):
        resp = session.get(FULFILLMENT_URL, params=params, headers=HEADERS, timeout=30)
        try:
            data = resp.json()
            break
        except ValueError:
            # Non-JSON response = Target's temporary block page. Wait and retry once.
            if attempt == 0:
                log(f"  store {store_id}: got blocked (HTTP {resp.status_code}), waiting 45s and retrying")
                time.sleep(45)
            else:
                raise RuntimeError(f"blocked by Target (HTTP {resp.status_code})")

    results = {}
    for p in (data.get("data") or {}).get("product_summaries") or []:
        opts = (p.get("fulfillment") or {}).get("store_options") or [{}]
        s = opts[0]
        qty = s.get("location_available_to_promise_quantity") or 0
        status = (s.get("in_store_only") or {}).get("availability_status") or "UNKNOWN"
        price = ((p.get("price") or {}).get("formatted_current_price")) or ""
        results[p["tcin"]] = {
            "qty": int(qty),
            "status": status,
            "price": price,
            "in_stock": qty > 0 or status in IN_STOCK_STATUSES,
        }
    return results


def send_discord(webhook, embeds, content=None):
    if not webhook:
        log("  (no DISCORD_WEBHOOK_URL set; skipping Discord post)")
        return
    # Discord allows max 10 embeds per message
    for i in range(0, max(len(embeds), 1), 10):
        payload = {"embeds": embeds[i : i + 10]}
        if content and i == 0:
            payload["content"] = content
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code >= 300:
            log(f"  Discord error {r.status_code}: {r.text[:200]}")
        time.sleep(1)


def product_link(tcin):
    return f"https://www.target.com/p/-/A-{tcin}"


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if os.environ.get("TEST_ALERT") == "true":
        send_discord(webhook, [{
            "title": "Test alert: stock checker is connected",
            "description": f"Watching {len(PRODUCTS)} products at {len(STORES)} Target stores.",
            "color": 0x3BA55D,
        }])
        log("Sent test alert.")
        return

    previous = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    current = {}
    alerts = []
    errors = []

    session = requests.Session()
    try:
        key = get_api_key(session)
    except Exception as e:
        log(f"ERROR getting site key: {e}")
        sys.exit(0)  # don't fail the run; try again next schedule

    for i, (store_id, store_name) in enumerate(STORES.items()):
        if i:
            time.sleep(SECONDS_BETWEEN_STORES)
        try:
            results = check_store(session, key, store_id)
        except Exception as e:
            errors.append(store_name)
            log(f"{store_name}: ERROR {e}")
            # keep previous state for this store so we don't send false alerts
            for tcin in PRODUCTS:
                k = f"{store_id}:{tcin}"
                if k in previous:
                    current[k] = previous[k]
            continue

        summary = []
        for tcin, name in PRODUCTS.items():
            r = results.get(tcin)
            if not r:
                continue
            k = f"{store_id}:{tcin}"
            was_in_stock = previous.get(k, {}).get("in_stock", False)
            current[k] = {"in_stock": r["in_stock"], "qty": r["qty"]}
            summary.append(f"{name}={r['qty']}")

            if r["in_stock"] and not was_in_stock:
                alerts.append({
                    "title": f"IN STOCK: {name}",
                    "url": product_link(tcin),
                    "color": 0x3BA55D,
                    "fields": [
                        {"name": "Store", "value": store_name, "inline": True},
                        {"name": "Units", "value": str(r["qty"]), "inline": True},
                        {"name": "Price", "value": r["price"] or "?", "inline": True},
                        {"name": "Status", "value": r["status"], "inline": True},
                    ],
                })
            elif was_in_stock and not r["in_stock"]:
                alerts.append({
                    "title": f"Sold out: {name}",
                    "url": product_link(tcin),
                    "color": 0x99AAB5,
                    "description": f"{store_name} is back to 0.",
                })
        log(f"{store_name}: " + ", ".join(summary))

    STATE_FILE.write_text(json.dumps(current, indent=1, sort_keys=True))

    if alerts:
        has_restock = any(a["title"].startswith("IN STOCK") for a in alerts)
        send_discord(webhook, alerts, content="@here Pokémon restock!" if has_restock else None)
        log(f"Sent {len(alerts)} alert(s).")
    else:
        log("No changes.")

    if errors:
        log(f"Stores that errored this run: {', '.join(errors)}")


if __name__ == "__main__":
    main()
