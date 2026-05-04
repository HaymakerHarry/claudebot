"""
Diagnose balance/allowance and order signing.
RUN: python diagnose_clob2.py
"""
import os, sys, inspect
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PRIVATE_KEY  = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
API_KEY      = os.environ.get("POLYMARKET_API_KEY", "")
API_SECRET   = os.environ.get("POLYMARKET_API_SECRET", "")
API_PASS     = os.environ.get("POLYMARKET_API_PASSPHRASE", "")
FUNDER       = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
HOST         = "https://clob.polymarket.com"
CHAIN_ID     = 137

from py_clob_client_v2 import ClobClient, ApiCreds
creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASS)

# ── What does py_clob_client_v2 export? ─────────────────────────────────────
import py_clob_client_v2 as pkg
print("\n── py_clob_client_v2 exports ───────────────────────────────")
exports = [x for x in dir(pkg) if not x.startswith("_")]
print("  " + ", ".join(exports))

# Look for AssetType or enums
for name in exports:
    obj = getattr(pkg, name)
    if "asset" in name.lower() or "type" in name.lower() or "enum" in name.lower():
        print(f"\n  {name}: {obj}")
        try:
            for member in obj:
                print(f"    {member.name} = {member.value!r}")
        except Exception:
            pass

# ── get_balance_allowance signature ─────────────────────────────────────────
print("\n── get_balance_allowance signature ─────────────────────────")
try:
    sig = inspect.signature(ClobClient.get_balance_allowance)
    print(f"  {sig}")
except Exception as e:
    print(f"  {e}")

print("\n── update_balance_allowance signature ──────────────────────")
try:
    sig2 = inspect.signature(ClobClient.update_balance_allowance)
    print(f"  {sig2}")
except Exception as e:
    print(f"  {e}")

# ── Try get_balance_allowance with different asset_type values ───────────────
print("\n── Trying get_balance_allowance with various asset_types ───")

# No funder client (original)
client_no_funder = ClobClient(
    host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, creds=creds,
    signature_type=0,
)

asset_types_to_try = ["USDC", "COLLATERAL", "usdc", "collateral", "0", "1",
                       "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                       "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"]

# Also try with AssetType enum if it exists
try:
    from py_clob_client_v2 import AssetType
    for at in AssetType:
        asset_types_to_try.append(at)
        asset_types_to_try.append(at.value)
except ImportError:
    pass

for at in asset_types_to_try:
    try:
        result = client_no_funder.get_balance_allowance(asset_type=at)
        print(f"  asset_type={at!r:50s} → {result}")
    except Exception as e:
        err = str(e)[:80]
        print(f"  asset_type={at!r:50s} → ERROR: {err}")

# ── Check get_address ────────────────────────────────────────────────────────
print("\n── get_address() ───────────────────────────────────────────")
try:
    addr = client_no_funder.get_address()
    print(f"  {addr}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── Try with funder set to EOA address (not proxy) ──────────────────────────
from eth_account import Account
eoa = Account.from_key(PRIVATE_KEY).address
print(f"\n── EOA address: {eoa}")
print(f"   Proxy wallet (from .env): {FUNDER}")

print("\n── Trying get_balance_allowance with funder=EOA (not proxy) ─")
client_funder_eoa = ClobClient(
    host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, creds=creds,
    funder=eoa, signature_type=0,
)
for at in ["COLLATERAL", "USDC"]:
    try:
        result = client_funder_eoa.get_balance_allowance(asset_type=at)
        print(f"  asset_type={at!r} → {result}")
    except Exception as e:
        print(f"  asset_type={at!r} → ERROR: {e}")

# ── check what signer looks like ─────────────────────────────────────────────
print("\n── Signer details ───────────────────────────────────────────")
for client_name, client in [("no_funder", client_no_funder), ("funder_eoa", client_funder_eoa)]:
    s = getattr(client, "signer", None)
    if s:
        print(f"  [{client_name}] signer type: {type(s).__name__}")
        for attr in dir(s):
            if not attr.startswith("_"):
                try:
                    val = getattr(s, attr)
                    if not callable(val):
                        print(f"    .{attr} = {val}")
                except Exception:
                    pass
