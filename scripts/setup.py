#!/usr/bin/env python3
"""
Polymarket wallet setup — run ONCE before starting the bot.

Press Enter without a key to create a simulation config (no real orders).

Steps performed (real key only):
  1. Read the Polygon private key securely via masked stdin (getpass),
     so it never appears in process listings (ps aux) or shell history.
  2. Check MATIC, USDC.e, and native USDC balances on Polygon mainnet.
  3. If the wallet holds native USDC but no USDC.e, auto-swap via Uniswap V3
     (Polymarket only accepts the bridged USDC.e variant).
  4. If the CTF Exchange allowance is zero, approve it for the current balance
     (exact amount — not an unlimited approval, to limit smart-contract risk).
  5. Derive Polymarket API keys deterministically from the private key via ECDSA.
  6. Write all credentials to TRADINEBOTTE_DIR/config.json with chmod 600.

Usage:
  python3 scripts/setup.py                           # real wallet
  python3 scripts/setup.py  (Enter without key)      # simulation mode
  TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py
"""

import sys, os, json, getpass, sysconfig

INSTALL_DIR = os.path.expanduser(os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte"))
RPC         = "https://polygon.drpc.org"
CONFIG_PATH = os.path.join(INSTALL_DIR, "config.json")

# getpass reads from /dev/tty so the key is never echoed to the terminal,
# never stored in readline history, and never visible in `ps aux` arguments.
PRIVATE_KEY = getpass.getpass(
    "Polygon private key (0x...) — Enter without key for simulation mode: "
).strip()

if not PRIVATE_KEY:
    # Simulation mode: write a minimal config with empty credentials.
    # The bot detects private_key="" and places SIMULATED orders only.
    os.makedirs(INSTALL_DIR, exist_ok=True)
    sim_config = {"private_key": "", "api_key": "", "api_secret": "", "api_passphrase": ""}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(sim_config, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"\n{'='*60}")
    print("  SIMULATION MODE — aucun ordre réel")
    print(f"{'='*60}")
    print(f"  Config écrit  : {CONFIG_PATH}")
    print("  private_key   : (vide — ordres simulés)")
    print(f"\nLancer le bot :")
    print(f"  bash scripts/start_bot.sh")
    print(f"  tail -f {os.path.join(INSTALL_DIR, 'live.log')}")
    sys.exit(0)

if not PRIVATE_KEY.startswith("0x") or len(PRIVATE_KEY) != 66:
    print("Invalid format — expected: 0x followed by 64 hex characters")
    sys.exit(1)

# ── Polygon contract addresses ─────────────────────────────────────────────────
USDC_E       = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # bridged USDC.e — Polymarket only accepts this variant
USDC_NATIVE  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native USDC (Circle's newer Polygon issuance)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polymarket CTF Exchange — needs allowance to take funds
UNISWAP_V3   = "0xE592427A0AEce92De3Edee1F18E0157C05861564"  # Uniswap V3 SwapRouter — used for the native→bridged swap

# sysconfig resolves the correct site-packages path for whatever Python version
# is currently running. This avoids a hardcoded "python3.12" substring that
# would break silently on Python 3.11 or any future 3.13+ install.
_venv = os.path.join(INSTALL_DIR, "venv")
_site = sysconfig.get_path("purelib", vars={"platbase": _venv, "base": _venv})
sys.path.insert(0, _site)

from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

w3   = Web3(Web3.HTTPProvider(RPC))
acct = Account.from_key(PRIVATE_KEY)  # pylint: disable=no-value-for-parameter
WALLET = acct.address

print(f"\n{'='*60}")
print("  SETUP POLYMARKET LIVE BOT")
print(f"{'='*60}")
print(f"Wallet    : {WALLET}")
print(f"Connected : {w3.is_connected()}")
print(f"Block     : {w3.eth.block_number}")

# ── ERC-20 minimal ABI ────────────────────────────────────────────────────────
# Only includes the three functions we need: balanceOf, allowance, approve.
# pylint: disable=line-too-long
abi = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]
# pylint: enable=line-too-long

usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=abi)
usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=abi)

bal_e = usdc_e.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
bal_n = usdc_n.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
allow = usdc_e.functions.allowance(Web3.to_checksum_address(WALLET), Web3.to_checksum_address(CTF_EXCHANGE)).call()
matic = w3.eth.get_balance(Web3.to_checksum_address(WALLET))

print("\n── BALANCES ──")
print(f"MATIC    : {matic/1e18:.4f} MATIC {'✅' if matic > 0.01*1e18 else '❌ INSUFFICIENT'}")
print(f"USDC.e   : {bal_e/1e6:.2f} USDC {'✅' if bal_e > 0 else '⚠️  Swap needed'}")
print(f"USDC nat : {bal_n/1e6:.2f} USDC")
print(f"Allowance: {min(allow/1e6, 999999):.0f}+ USDC {'✅' if allow > 0 else '❌ Needs approval'}")

# ── Swap USDC native → USDC.e if needed ───────────────────────────────────────
if bal_e == 0 and bal_n > 0:
    print(f"\n⚠️  No USDC.e but {bal_n/1e6:.2f} native USDC detected.")
    print("   Auto-swapping native USDC → USDC.e...")
    import time

    # Keep 1 USDC native as dust reserve; swap everything else.
    amount_in = bal_n - int(1e6)

    # Step 1: approve the Uniswap V3 router to spend exactly amount_in.
    # Using the exact amount instead of 2**256-1 limits the blast radius if
    # the Uniswap router were ever exploited after this transaction.
    usdc_native_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=abi)
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(WALLET))
    tx = usdc_native_contract.functions.approve(
        Web3.to_checksum_address(UNISWAP_V3), amount_in
    ).build_transaction({'from': Web3.to_checksum_address(WALLET), 'nonce': nonce,
                         'gas': 100000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print("   Approve OK")

    # Step 2: call exactInputSingle on the Uniswap V3 SwapRouter.
    # fee=100 selects the 0.01% fee tier — the cheapest pool for stablecoin-to-stablecoin swaps.
    # amountOutMinimum = amount_in × 0.995 enforces a 0.5% max slippage guard.
    # sqrtPriceLimitX96=0 means no on-chain price cap (the slippage guard is enough here).
    router_abi = [{"inputs":[{"components":[
        {"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},
        {"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},
        {"name":"deadline","type":"uint256"},{"name":"amountIn","type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}
    ],"name":"params","type":"tuple"}],"name":"exactInputSingle",
    "outputs":[{"name":"amountOut","type":"uint256"}],"type":"function"}]
    router = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V3), abi=router_abi)
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(WALLET))
    tx = router.functions.exactInputSingle((
        Web3.to_checksum_address(USDC_NATIVE),
        Web3.to_checksum_address(USDC_E),
        100,                              # 0.01% fee tier (cheapest stable pool)
        Web3.to_checksum_address(WALLET),
        int(time.time()) + 300,           # 5-minute deadline
        amount_in,
        int(amount_in * 0.995),           # minimum output: 0.5% max slippage
        0                                 # no price limit
    )).build_transaction({'from': Web3.to_checksum_address(WALLET), 'nonce': nonce,
                          'gas': 300000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print(f"   Swap {'OK' if receipt.status==1 else 'FAILED'} — TX: {txh.hex()}")
    bal_e = usdc_e.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
    print(f"   USDC.e balance: {bal_e/1e6:.2f}")

# ── Approve CTF Exchange to spend USDC.e ─────────────────────────────────────
if allow == 0 and bal_e > 0:
    print("\n⚠️  No allowance set. Approving CTF Exchange...")
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(WALLET))
    # Approve the exact current balance rather than 2**256-1 (unlimited).
    # If CTF Exchange were ever exploited, the attacker could only drain the
    # balance at approval time, not all future deposits into the wallet.
    tx = usdc_e.functions.approve(
        Web3.to_checksum_address(CTF_EXCHANGE), bal_e
    ).build_transaction({'from': Web3.to_checksum_address(WALLET), 'nonce': nonce,
                         'gas': 100000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print(f"   Allowance {'OK' if receipt.status==1 else 'FAILED'} — TX: {txh.hex()}")

# ── Derive Polymarket API keys ─────────────────────────────────────────────────
# create_or_derive_api_creds signs a deterministic message with the wallet's
# ECDSA key. The result is reproducible — re-running this script produces the
# same API keys, so no separate secret needs to be stored beyond the private key.
print("\n── API KEYS POLYMARKET ──")
client = ClobClient("https://clob.polymarket.com",
    key=PRIVATE_KEY, chain_id=POLYGON, signature_type=0)
creds = client.create_or_derive_api_creds()
print("✅ Keys derived from private key")

# ── Write config.json ─────────────────────────────────────────────────────────
config = {
    "private_key":    PRIVATE_KEY,
    "api_key":        creds.api_key,
    "api_secret":     creds.api_secret,
    "api_passphrase": creds.api_passphrase,
}
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
with open(CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
# 0o600: owner read+write only — prevents other OS users from reading the file.
os.chmod(CONFIG_PATH, 0o600)

print(f"\n{'='*60}")
print(f"  CONFIG WRITTEN → {CONFIG_PATH}")
print(f"{'='*60}")
print(f"  Wallet        : {WALLET}")
print("  Keys derived  : OK")
print("  Permissions   : chmod 600")
print(f"\n{'='*60}")
print("  LAUNCH")
print(f"{'='*60}")
print("bash scripts/start_bot.sh")
print(f"tail -f {os.path.join(INSTALL_DIR, 'live.log')}")
