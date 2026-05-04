"""
Inspect py_clob_client_v2 signing internals and test all signature types.
RUN: python inspect_signing.py
"""
import os, sys, inspect
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import py_clob_client_v2 as pkg
pkg_dir = os.path.dirname(pkg.__file__)
print(f"\n── Package location: {pkg_dir}")

# ── Print signing.py source ──────────────────────────────────────────────────
for fname in ["signer.py", "signing.py", "order_builder/__init__.py"]:
    fpath = os.path.join(pkg_dir, fname)
    if os.path.exists(fpath):
        print(f"\n{'═'*60}")
        print(f"  SOURCE: {fname}")
        print('═'*60)
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            print(f.read())

# ── Also check order_builder directory ──────────────────────────────────────
ob_dir = os.path.join(pkg_dir, "order_builder")
if os.path.isdir(ob_dir):
    for fname in os.listdir(ob_dir):
        if fname.endswith(".py"):
            fpath = os.path.join(ob_dir, fname)
            print(f"\n{'═'*60}")
            print(f"  SOURCE: order_builder/{fname}")
            print('═'*60)
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                print(f.read())

# ── Try create_order (no post) with each signature type ─────────────────────
from py_clob_client_v2 import ClobClient, ApiCreds, SignatureTypeV2, OrderArgs, OrderType, PartialCreateOrderOptions, Side
from eth_account import Account

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
API_KEY     = os.environ.get("POLYMARKET_API_KEY", "")
API_SECRET  = os.environ.get("POLYMARKET_API_SECRET", "")
API_PASS    = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
HOST        = "https://clob.polymarket.com"
EOA         = Account.from_key(PRIVATE_KEY).address

creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)

# Dummy token ID to test signing (won't post)
DUMMY_TOKEN = "1343197538147866997676250008839231694243646439078075543648191028162058737203"

print(f"\n\n── Testing create_order (no post) with each sig type ───────")
print(f"   EOA:   {EOA}")
print(f"   Proxy: {FUNDER}")

for sig_type in SignatureTypeV2:
    for use_funder in [False, True]:
        funder_val = FUNDER if use_funder else None
        label = f"sig_type={sig_type.name}({sig_type.value}), funder={'proxy' if use_funder else 'None'}"
        try:
            client = ClobClient(
                host=HOST, key=PRIVATE_KEY, chain_id=137,
                creds=creds,
                funder=funder_val,
                signature_type=sig_type,
            )
            order_args = OrderArgs(
                token_id=DUMMY_TOKEN,
                price=0.50,
                size=1.0,
                side=Side.BUY,
            )
            order = client.create_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size="0.01"),
            )
            # Show the signed order structure
            sig_preview = str(order)[:200] if order else "None"
            print(f"\n  ✅ {label}")
            print(f"     order preview: {sig_preview}...")
        except Exception as e:
            print(f"\n  ❌ {label}")
            print(f"     {str(e)[:120]}")
