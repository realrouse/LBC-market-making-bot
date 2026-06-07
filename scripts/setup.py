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

# ─── LANGUAGE SELECTION ───────────────────────────────────────────────────────
# Asked before anything else so all subsequent output uses the chosen language.
print("Language / Langue :  [E] English   [F] Français")
_lang_raw = input(">>> ").strip().upper()
LANG = "FR" if _lang_raw.startswith("F") else "EN"

# ─── TRANSLATIONS ─────────────────────────────────────────────────────────────
# All user-visible strings indexed by LANG. Add new keys here to keep every
# message in one place and avoid scattered bilingual conditionals throughout.
T = {
    # Private key prompt
    "key_prompt": {
        "EN": "Polygon private key (0x...) — press Enter without key for simulation mode: ",
        "FR": "Clé privée Polygon (0x...) — Entrée sans clé pour mode simulation : ",
    },
    # Simulation mode output
    "sim_title": {
        "EN": "  SIMULATION MODE — no real orders",
        "FR": "  MODE SIMULATION — aucun ordre réel",
    },
    "sim_config": {
        "EN": "  Config written : ",
        "FR": "  Config écrit   : ",
    },
    "sim_key": {
        "EN": "  private_key    : (empty — simulated orders)",
        "FR": "  private_key    : (vide — ordres simulés)",
    },
    "sim_launch_title": {
        "EN": "Launch the bot :",
        "FR": "Lancer le bot :",
    },
    # Key format error
    "bad_key": {
        "EN": "Invalid format — expected: 0x followed by 64 hex characters",
        "FR": "Format invalide — attendu : 0x suivi de 64 caractères hexadécimaux",
    },
    # Setup header
    "setup_title": {
        "EN": "  POLYMARKET LIVE BOT — SETUP",
        "FR": "  POLYMARKET LIVE BOT — CONFIGURATION",
    },
    "label_wallet": {
        "EN": "Wallet    : ",
        "FR": "Portefeuille : ",
    },
    "label_connected": {
        "EN": "Connected : ",
        "FR": "Connecté  : ",
    },
    "label_block": {
        "EN": "Block     : ",
        "FR": "Bloc      : ",
    },
    # Balances section
    "balances_title": {
        "EN": "── BALANCES ──",
        "FR": "── SOLDES ──",
    },
    "bal_ok":           {"EN": "✅",              "FR": "✅"},
    "bal_matic_low":    {"EN": "❌ INSUFFICIENT", "FR": "❌ INSUFFISANT"},
    "bal_usdc_e_low":   {"EN": "⚠️  Swap needed", "FR": "⚠️  Swap nécessaire"},
    "bal_allow_low":    {"EN": "❌ Needs approval","FR": "❌ Approbation requise"},
    # Swap section
    "swap_detected": {
        "EN": "⚠️  No USDC.e but {amt:.2f} native USDC detected.",
        "FR": "⚠️  Pas de USDC.e mais {amt:.2f} USDC natif détecté.",
    },
    "swap_starting": {
        "EN": "   Auto-swapping native USDC → USDC.e...",
        "FR": "   Échange automatique USDC natif → USDC.e...",
    },
    "swap_approve_ok": {
        "EN": "   Approve OK",
        "FR": "   Approbation OK",
    },
    "swap_ok":   {"EN": "OK",     "FR": "OK"},
    "swap_fail": {"EN": "FAILED", "FR": "ÉCHOUÉ"},
    "swap_done": {
        "EN": "   Swap {status} — TX: {tx}",
        "FR": "   Échange {status} — TX : {tx}",
    },
    "swap_balance": {
        "EN": "   USDC.e balance: {bal:.2f}",
        "FR": "   Solde USDC.e : {bal:.2f}",
    },
    # Allowance section
    "allow_needed": {
        "EN": "⚠️  No allowance set. Approving CTF Exchange...",
        "FR": "⚠️  Aucune autorisation. Approbation du CTF Exchange...",
    },
    "allow_done": {
        "EN": "   Allowance {status} — TX: {tx}",
        "FR": "   Autorisation {status} — TX : {tx}",
    },
    # API keys section
    "api_title": {
        "EN": "── POLYMARKET API KEYS ──",
        "FR": "── CLÉS API POLYMARKET ──",
    },
    "api_ok": {
        "EN": "✅ Keys derived from private key",
        "FR": "✅ Clés dérivées depuis la clé privée",
    },
    # Final config summary
    "cfg_title": {
        "EN": "  CONFIG WRITTEN → ",
        "FR": "  CONFIG ÉCRIT → ",
    },
    "cfg_keys":   {"EN": "  Keys derived  : OK", "FR": "  Clés dérivées  : OK"},
    "cfg_perms":  {"EN": "  Permissions   : chmod 600", "FR": "  Permissions    : chmod 600"},
    "launch_title": {
        "EN": "  LAUNCH",
        "FR": "  LANCEMENT",
    },
}


def t(key, **kwargs):
    """Return the translated string for `key` in the current LANG, with optional format args."""
    text = T[key][LANG]
    return text.format(**kwargs) if kwargs else text


# ─── PRIVATE KEY ──────────────────────────────────────────────────────────────
# getpass reads from /dev/tty so the key is never echoed to the terminal,
# never stored in readline history, and never visible in `ps aux` arguments.
PRIVATE_KEY = getpass.getpass(t("key_prompt")).strip()

if not PRIVATE_KEY:
    # Simulation mode: write a minimal config with empty credentials.
    # The bot detects private_key="" and places SIMULATED orders only.
    os.makedirs(INSTALL_DIR, exist_ok=True)
    sim_config = {"private_key": "", "api_key": "", "api_secret": "", "api_passphrase": "", "lang": LANG}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(sim_config, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"\n{'='*60}")
    print(t("sim_title"))
    print(f"{'='*60}")
    print(t("sim_config") + CONFIG_PATH)
    print(t("sim_key"))
    print(f"\n{t('sim_launch_title')}")
    print(f"  {INSTALL_DIR}/run.sh")
    print(f"  tail -f {os.path.join(INSTALL_DIR, 'live.log')}")
    sys.exit(0)

if not PRIVATE_KEY.startswith("0x") or len(PRIVATE_KEY) != 66:
    print(t("bad_key"))
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
print(t("setup_title"))
print(f"{'='*60}")
print(t("label_wallet")    + WALLET)
print(t("label_connected") + str(w3.is_connected()))
print(t("label_block")     + str(w3.eth.block_number))

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

print(f"\n{t('balances_title')}")
matic_status = t("bal_ok") if matic > 0.01 * 1e18 else t("bal_matic_low")
usdc_e_status = t("bal_ok") if bal_e > 0 else t("bal_usdc_e_low")
allow_status  = t("bal_ok") if allow > 0 else t("bal_allow_low")
print(f"MATIC    : {matic/1e18:.4f} MATIC {matic_status}")
print(f"USDC.e   : {bal_e/1e6:.2f} USDC {usdc_e_status}")
print(f"USDC nat : {bal_n/1e6:.2f} USDC")
print(f"Allowance: {min(allow/1e6, 999999):.0f}+ USDC {allow_status}")

# ── Swap USDC native → USDC.e if needed ───────────────────────────────────────
if bal_e == 0 and bal_n > 0:
    print(f"\n{t('swap_detected', amt=bal_n/1e6)}")
    print(t("swap_starting"))
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
    print(t("swap_approve_ok"))

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
    status = t("swap_ok") if receipt.status == 1 else t("swap_fail")
    print(t("swap_done", status=status, tx=txh.hex()))
    bal_e = usdc_e.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
    print(t("swap_balance", bal=bal_e / 1e6))

# ── Approve CTF Exchange to spend USDC.e ─────────────────────────────────────
if allow == 0 and bal_e > 0:
    print(f"\n{t('allow_needed')}")
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
    status = t("swap_ok") if receipt.status == 1 else t("swap_fail")
    print(t("allow_done", status=status, tx=txh.hex()))

# ── Derive Polymarket API keys ─────────────────────────────────────────────────
# create_or_derive_api_creds signs a deterministic message with the wallet's
# ECDSA key. The result is reproducible — re-running this script produces the
# same API keys, so no separate secret needs to be stored beyond the private key.
print(f"\n{t('api_title')}")
client = ClobClient("https://clob.polymarket.com",
    key=PRIVATE_KEY, chain_id=POLYGON, signature_type=0)
creds = client.create_or_derive_api_creds()
print(t("api_ok"))

# ── Write config.json ─────────────────────────────────────────────────────────
config = {
    "private_key":    PRIVATE_KEY,
    "api_key":        creds.api_key,
    "api_secret":     creds.api_secret,
    "api_passphrase": creds.api_passphrase,
    "lang":           LANG,   # persisted so shell scripts (start_bot, monitor) inherit the choice
}
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
with open(CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
# 0o600: owner read+write only — prevents other OS users from reading the file.
os.chmod(CONFIG_PATH, 0o600)

print(f"\n{'='*60}")
print(t("cfg_title") + CONFIG_PATH)
print(f"{'='*60}")
print(t("label_wallet") + WALLET)
print(t("cfg_keys"))
print(t("cfg_perms"))
print(f"\n{'='*60}")
print(t("launch_title"))
print(f"{'='*60}")
print("bash scripts/start_bot.sh")
print(f"tail -f {os.path.join(INSTALL_DIR, 'live.log')}")
