"""
Test posting a tiny real order with every signing type combination.
Uses a real market token, $1 stake, cancelled immediately if it fills.
RUN: python test_order_signing.py
"""
import os, sys, requests, json
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from py_clob_client_v2 import (ClobClient, ApiCreds, SignatureTypeV2,
                                OrderArgs, OrderType, PartialCreateOrderOptions, Side)
from eth_account import Account

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
API_KEY     = os.environ.get("POLYMARKET_API_KEY", "")
API_SECRET  = os.environ.get("POLYMARKET_API_SECRET", "")
API_PASS    = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
HOST        = "https://clob.polymarket.com"
EOA         = Account.from_key(PRIVATE_KEY).address

creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)

# ── Fetch a real market and token ID from Gamma API ──────────────────────────
print("\n── Fetching a real market to use as test token ─────────────")
try:
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": True, "closed": False, "limit": 20, "order": "volume", "ascending": False},
        timeout=10,
    )
    markets = r.json()
    # Find a binary market with YES token and price between 0.10 and 0.90
    test_token = None
    test_market = None
    test_price = None
    for m in markets:
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str):
            try: tokens = json.loads(tokens)
            except: continue
        if len(tokens) < 1:
            continue
        # Use a cheap YES token (price around 0.10) to minimise fill risk on test
        try:
            out_yes = m.get("outcomePrices", "[]")
            if isinstance(out_yes, str): out_yes = json.loads(out_yes)
            yes_price = float(out_yes[0]) if out_yes else None
            if yes_price and 0.05 < yes_price < 0.30:
                test_token  = tokens[0]
                test_market = m.get("question", "?")[:60]
                test_price  = yes_price
                break
        except Exception:
            continue
    if test_token:
        print(f"  Market : {test_market}")
        print(f"  Token  : {test_token}")
        print(f"  YES px : {test_price}")
    else:
        print("  ⚠️  No suitable market found — using first available")
        m = markets[0]
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str): tokens = json.loads(tokens)
        test_token  = tokens[0]
        test_market = m.get("question", "?")[:60]
        test_price  = 0.10
        print(f"  Market : {test_market}")
        print(f"  Token  : {test_token}")
except Exception as e:
    print(f"  ❌ Could not fetch markets: {e}")
    sys.exit(1)

# ── Test each signing type ────────────────────────────────────────────────────
print(f"\n── Testing POST order with each sig type (price={test_price}, size=1) ──")
print(f"   EOA:   {EOA}")
print(f"   Proxy: {FUNDER}\n")

results = []

for sig_type in SignatureTypeV2:
    for use_funder in [False, True]:
        funder_val = FUNDER if use_funder else None
        label = f"sig_type={sig_type.name}({sig_type.value}), funder={'proxy' if use_funder else 'None ':5s}"
        try:
            client = ClobClient(
                host=HOST, key=PRIVATE_KEY, chain_id=137,
                creds=creds,
                funder=funder_val,
                signature_type=sig_type,
            )
            # Price well below market so it almost certainly won't fill
            price = round(min(test_price * 0.5, 0.05), 2)
            price = max(price, 0.01)
            size  = round(1.0 / price, 1)  # ~$1 stake

            resp = client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=test_token,
                    price=price,
                    size=size,
                    side=Side.BUY,
                ),
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.GTC,
            )
            order_id = resp.get("orderID") or resp.get("id") or str(resp)[:40]
            print(f"  ✅ {label} → ORDER PLACED! id={order_id}")
            results.append(("OK", sig_type, use_funder, order_id))

            # Cancel immediately
            try:
                client.cancel_order(order_id)
                print(f"     ↩️  Cancelled {order_id}")
            except Exception as ce:
                print(f"     ⚠️  Cancel failed: {ce}")

        except Exception as e:
            err = str(e)[:100]
            status = "invalid_sig" if "invalid signature" in err.lower() else \
                     "balance"     if "balance" in err.lower() else \
                     "other"
            print(f"  ❌ {label} → [{status}] {err}")
            results.append((status, sig_type, use_funder, err))

print(f"\n── Summary ─────────────────────────────────────────────────")
for res, sig_type, use_funder, detail in results:
    fstr = "proxy" if use_funder else "None "
    print(f"  {res:12s} | {sig_type.name:20s} | funder={fstr} | {str(detail)[:60]}")
