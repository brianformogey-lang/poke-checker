"""
One-off diagnostic.
1) Records each watched product's DPCI (to match against store receipts).
2) Control test: does Target's store-stock data show real numbers for other trading cards
   and regular toys, while Pokemon reads 0?
Writes reports/diagnostic.json. Run from GitHub: Actions -> Diagnostic -> Run workflow.
"""
import json, time
from pathlib import Path
import check_stock as cs

RECEIPT_DPCIS = {"361-00-8095": "$69.99", "361-00-0040": "$31.99", "361-00-2198": "$29.99",
                 "361-00-6528": "$19.99", "361-00-0053": "$12.99", "361-00-4809": "$11.99"}
CONTROLS = {
    "94725169": "TOY: Rayquaza figure (IN_STOCK on day 1)",
    "53438201": "TOY: Pikachu walkie talkies (IN_STOCK on day 1)",
    "88504355": "CARDS: 2023 Topps Series 1 blaster",
    "89547654": "CARDS: 2023 Topps Chrome blaster",
    "90478751": "CARDS: MTG Karlov Manor play booster 3-pack",
    "1009866458": "CARDS: MTG TMNT play booster",
    "91366486": "CARDS: Lorcana Into the Inklands booster box",
    "1010148355": "CARDS: Lorcana Winterspell booster pack",
    "1010263962": "CARDS: Lorcana Wilds Unknown booster display",
    "91351718": "CARDS: Lorcana Ursula's Return booster display",
    "1010892070": "POKEMON: 30th Knock Out Collection",
    "1011407490": "POKEMON: 30th Booster Bundle",
    "94411686": "POKEMON: Charizard ex Special Collection",
    "95230446": "POKEMON: Perfect Order 3-booster blister",
}
STORES = {"1456": "Rogers", "2101": "Roseville", "1484": "Lakeville"}


def fetch(session, key, tcins, store):
    params = {"key": key, "tcins": ",".join(tcins), "store_id": store, "required_store_id": store,
              "scheduled_delivery_store_id": store, "zip": cs.ZIP, "state": cs.STATE,
              "paid_membership": "false", "base_membership": "false", "card_membership": "false", "channel": "WEB"}
    for attempt in range(3):
        r = session.get(cs.FULFILLMENT_URL, params=params, headers=cs.HEADERS, timeout=30)
        try:
            s = (r.json().get("data") or {}).get("product_summaries") or []
            if s:
                return s
        except ValueError:
            pass
        time.sleep(15)
    raise RuntimeError(f"blocked (HTTP {r.status_code})")


def dpci_of(p):
    it = p.get("item") or {}
    for k in ("dpci", "DPCI"):
        if it.get(k):
            return it[k]
    # search nested fields for anything that looks like a DPCI
    blob = json.dumps(it)
    import re
    m = re.search(r'"dpci"\s*:\s*"([0-9-]{9,11})"', blob, re.I)
    return m.group(1) if m else None


def main():
    session = cs.requests.Session()
    key = cs.get_api_key(session)
    out = {"started_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}

    # 1) DPCIs of watched products
    try:
        summ = fetch(session, key, list(cs.PRODUCTS), "1456")
        dp = {}
        for p in summ:
            d = dpci_of(p)
            dp[p["tcin"]] = {"name": cs.PRODUCTS.get(p["tcin"]), "dpci": d,
                             "on_receipt": bool(d and d in RECEIPT_DPCIS),
                             "price": (p.get("price") or {}).get("formatted_current_price")}
        out["watched_dpcis"] = dp
        out["item_keys_sample"] = sorted((summ[0].get("item") or {}).keys())
        out["receipt_dpcis_not_in_watch_list"] = [d for d in RECEIPT_DPCIS
                                                  if d not in {v["dpci"] for v in dp.values()}]
    except Exception as e:
        out["watched_dpcis"] = f"ERROR: {e}"

    # 2) control test
    out["control_test"] = {}
    for name_id, name in STORES.items():
        time.sleep(8)
        try:
            summ = fetch(session, key, list(CONTROLS), name_id)
            res = {}
            for p in summ:
                s = ((p.get("fulfillment") or {}).get("store_options") or [{}])[0]
                res[CONTROLS.get(p["tcin"], p["tcin"])] = (
                    f"qty={s.get('location_available_to_promise_quantity')} "
                    f"instore={(s.get('in_store_only') or {}).get('availability_status')} "
                    f"pickup={(s.get('order_pickup') or {}).get('availability_status')}")
            for t, label in CONTROLS.items():
                res.setdefault(label, "not returned")
            out["control_test"][name] = res
        except Exception as e:
            out["control_test"][name] = f"ERROR: {e}"
        print(name, "done", flush=True)

    Path("reports").mkdir(exist_ok=True)
    Path("reports/diagnostic.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
