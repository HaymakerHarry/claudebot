"""
Quick check: does the private key in .env match the Polymarket wallet address?
RUN: python check_wallet.py
"""
import os, sys
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
EXPECTED    = "0x14A6ed8BB49cF095417e00B2DF27D4ae8745e0a9"   # from browser POLY_ADDRESS

if not PRIVATE_KEY:
    print("❌  POLYMARKET_PRIVATE_KEY not set in .env")
    sys.exit(1)

try:
    from eth_account import Account
    acct = Account.from_key(PRIVATE_KEY)
    derived = acct.address
except Exception as e:
    print(f"❌  Could not derive address from key: {e}")
    sys.exit(1)

print(f"\n  Private key → address : {derived}")
print(f"  Browser POLY_ADDRESS  : {EXPECTED}")
print(f"  Proxy wallet          : 0x311df0E628b31b695507D954B84718B6cCc9BB7e")

if derived.lower() == EXPECTED.lower():
    print("\n  ✅  MATCH — private key controls the Polymarket wallet")
    print("     The signature issue is in signing type, not the key itself.")
else:
    print("\n  ❌  MISMATCH — the private key does NOT control the Polymarket wallet!")
    print(f"     Your .env key controls: {derived}")
    print(f"     Polymarket is using   : {EXPECTED}")
    print()
    print("  This means the bot can never sign valid orders.")
    print("  The private key in .env needs to be the key for 0x14A6...ed8BB49c...")
    print("  (i.e. the Trust Wallet / MetaMask key for your Polymarket account)")
