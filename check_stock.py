"""
Target Pokemon TCG stock checker.

Checks store-level inventory for a list of Target products at a list of stores,
and posts to a Discord channel when something comes into stock (and when it sells out).

Runs on a schedule via GitHub Actions, one job per territory (see territories.json):
    python check_stock.py --territory south
Each territory posts to its own Discord channel via the webhook passed in DISCORD_WEBHOOK_URL.
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
# Products to watch live in products.json (Target product ID -> name). Max 30: Target's
# lookup takes at most 30 products per request, and we use one request per store.
PRODUCTS = json.loads((Path(__file__).parent / "products.json").read_text())
assert len(PRODUCTS) <= 30, "Target's lookup takes at most 30 products per request"

# Stores live in territories.json, grouped by territory. Each territory has its own
# Discord channel (webhook secret) and its own state file under state/.
BASE_DIR = Path(__file__).parent
TERRITORIES = json.loads((BASE_DIR / "territories.json").read_text())

# Set in main() from the --territory argument
TERRITORY = None
STORES = {}

ZIP = "55337"
STATE = "MN"
SECONDS_BETWEEN_STORES = 8  # Target throttles rapid-fire requests; keep this >= 8
STATE_FILE = None   # state/<territory>.json   - what was in stock last run
REPORT_FILE = None  # reports/<territory>.json - what happened on the latest run

MAX_CONSECUTIVE_FAILURES = 3  # this many stores in a row fail -> Target is throttling; stop the run

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
    # Target sometimes throttles by returning an HTML block page OR a JSON reply with no
    # products in it. Both count as a failed check: wait and retry, then give up for this run.
    waits = [15]
    problem = ""
    summaries = None
    for attempt in range(len(waits) + 1):
        resp = session.get(FULFILLMENT_URL, params=params, headers=HEADERS, timeout=30)
        try:
            data = resp.json()
            summaries = (data.get("data") or {}).get("product_summaries") or []
            if summaries:
                break
            errs = data.get("errors") or data.get("message") or ""
            problem = f"HTTP {resp.status_code}, no products in reply: {str(errs)[:200]}"
        except ValueError:
            problem = f"HTTP {resp.status_code}, blocked (non-JSON reply)"
        summaries = None
        if attempt < len(waits):
            log(f"  store {store_id}: {problem}; waiting {waits[attempt]}s and retrying")
            time.sleep(waits[attempt])
    if not summaries:
        raise RuntimeError(problem)

    results = {}
    for p in summaries:
        opts = (p.get("fulfillment") or {}).get("store_options") or [{}]
        s = opts[0]
        qty = s.get("location_available_to_promise_quantity") or 0
        status = (s.get("in_store_only") or {}).get("availability_status") or "UNKNOWN"
        price = ((p.get("price") or {}).get("formatted_current_price")) or ""
        title = ((p.get("item") or {}).get("product_description") or {}).get("title") or ""
        results[p["tcin"]] = {
            "title": title.replace("&#233;", "é").replace("&#8212;", "—"),
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


def setup_territory(name):
    global TERRITORY, STORES, STATE_FILE, REPORT_FILE
    if name not in TERRITORIES:
        sys.exit(f"Unknown territory {name!r}. Choose from: {', '.join(TERRITORIES)}")
    TERRITORY = TERRITORIES[name]
    STORES = TERRITORY["stores"]
    (BASE_DIR / "state").mkdir(exist_ok=True)
    (BASE_DIR / "reports").mkdir(exist_ok=True)
    STATE_FILE = BASE_DIR / "state" / f"{name}.json"
    REPORT_FILE = BASE_DIR / "reports" / f"{name}.json"


def main():
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "--territory":
        sys.exit("usage: python check_stock.py --territory <name>")
    setup_territory(args[1])
    label = TERRITORY["label"]
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if os.environ.get("TEST_ALERT") == "true":
        send_discord(webhook, [{
            "title": f"Test alert: {label} territory is connected",
            "description": f"Watching {len(PRODUCTS)} products at {len(STORES)} Target stores:\n"
                           + "\n".join(f"• {n}" for n in STORES.values()),
            "color": 0x3BA55D,
        }])
        log(f"Sent test alert for {label}.")
        return

    previous = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    current = {}
    alerts = []
    errors = []
    report = {"territory": label, "started_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "stores": {}}

    session = requests.Session()
    try:
        key = get_api_key(session)
    except Exception as e:
        log(f"ERROR getting site key: {e}")
        sys.exit(0)  # don't fail the run; try again next schedule

    consecutive_failures = 0
    stopped_early = False
    for i, (store_id, store_name) in enumerate(STORES.items()):
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            # Target is throttling this whole run; stop and try again next schedule.
            stopped_early = True
            errors.append(store_name)
            report["stores"][store_name] = "skipped: run stopped early (Target throttling)"
            for tcin in PRODUCTS:
                k = f"{store_id}:{tcin}"
                if k in previous:
                    current[k] = previous[k]
            continue
        if i:
            time.sleep(SECONDS_BETWEEN_STORES)
        try:
            results = check_store(session, key, store_id)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            errors.append(store_name)
            report["stores"][store_name] = f"ERROR: {e}"
            log(f"{store_name}: ERROR {e}")
            # keep previous state for this store so we don't send false alerts
            for tcin in PRODUCTS:
                k = f"{store_id}:{tcin}"
                if k in previous:
                    current[k] = previous[k]
            continue

        if "products" not in report:
            report["products"] = {
                tcin: (f"{r['price']} | {r['status']} | {r['title'][:70]}" if (r := results.get(tcin))
                       else "NOT RETURNED by Target (bad or retired product ID?)")
                for tcin in PRODUCTS
            }
        summary = []
        for tcin, name in PRODUCTS.items():
            r = results.get(tcin)
            if not r:
                continue
            k = f"{store_id}:{tcin}"
            was_in_stock = previous.get(k, {}).get("in_stock", False)
            current[k] = {"in_stock": r["in_stock"], "qty": r["qty"]}
            if r["qty"] or r["in_stock"]:
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
                    "footer": {"text": f"{label} territory"},
                })
            elif was_in_stock and not r["in_stock"]:
                alerts.append({
                    "title": f"Sold out: {name}",
                    "url": product_link(tcin),
                    "color": 0x99AAB5,
                    "description": f"{store_name} is back to 0.",
                })
        report["stores"][store_name] = "ok: " + (", ".join(summary) or f"all {len(results)} products at 0")
        log(f"{store_name}: " + (", ".join(summary) or f"all {len(results)} products at 0"))

    STATE_FILE.write_text(json.dumps(current, indent=1, sort_keys=True))
    report["ok_count"] = len(STORES) - len(errors)
    report["error_count"] = len(errors)
    report["stopped_early"] = stopped_early
    REPORT_FILE.write_text(json.dumps(report, indent=1))

    if alerts:
        has_restock = any(a["title"].startswith("IN STOCK") for a in alerts)
        send_discord(webhook, alerts, content=f"@here Pokémon restock in {label}!" if has_restock else None)
        log(f"Sent {len(alerts)} alert(s).")
    else:
        log("No changes.")

    if stopped_early:
        log("Stopped early: Target is throttling this run. Will try again next schedule.")
    if errors:
        log(f"Stores that errored/skipped this run: {', '.join(errors)}")


if __name__ == "__main__":
    main()
