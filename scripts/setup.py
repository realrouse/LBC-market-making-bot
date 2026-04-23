#!/usr/bin/env python3
"""
SETUP WALLET — À exécuter UNE SEULE FOIS avant de lancer le bot.
Ce script :
1. Dérive les API keys Polymarket depuis ta clé privée
2. Vérifie les balances USDC
3. Approuve l'allowance USDC.e pour le contrat Polymarket
4. Affiche les variables d'environnement à exporter

Usage:
    python3 setup.py 0xTA_PRIVATE_KEY_ICI
"""

import sys, os

if len(sys.argv) < 2:
    print("Usage: python3 setup.py 0xTA_PRIVATE_KEY")
    sys.exit(1)

PRIVATE_KEY = sys.argv[1]
RPC = "https://polygon.drpc.org"

# Contrats Polygon
USDC_E       = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (requis par Polymarket)
USDC_NATIVE  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC natif
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polymarket CTF Exchange
UNISWAP_V3   = "0xE592427A0AEce92De3Edee1F18E0157C05861564"  # Pour swap si nécessaire

sys.path.insert(0, '/opt/polymarket-live/venv/lib/python3.12/site-packages')

from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

w3 = Web3(Web3.HTTPProvider(RPC))
acct = Account.from_key(PRIVATE_KEY)
WALLET = acct.address

print(f"\n{'='*60}")
print(f"  SETUP POLYMARKET LIVE BOT")
print(f"{'='*60}")
print(f"Wallet   : {WALLET}")
print(f"Connecté : {w3.is_connected()}")
print(f"Block    : {w3.eth.block_number}")

# ── Vérification balances ─────────────────────────────────────────────────
abi = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=abi)
usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=abi)

bal_e = usdc_e.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
bal_n = usdc_n.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
allow = usdc_e.functions.allowance(Web3.to_checksum_address(WALLET), Web3.to_checksum_address(CTF_EXCHANGE)).call()
matic = w3.eth.get_balance(Web3.to_checksum_address(WALLET))

print(f"\n── BALANCES ──")
print(f"MATIC    : {matic/1e18:.4f} MATIC {'✅' if matic > 0.01*1e18 else '❌ INSUFFISANT'}")
print(f"USDC.e   : {bal_e/1e6:.2f} USDC {'✅' if bal_e > 0 else '⚠️  Besoin de swap'}")
print(f"USDC nat : {bal_n/1e6:.2f} USDC")
print(f"Allowance: {min(allow/1e6, 999999):.0f}+ USDC {'✅' if allow > 0 else '❌ À approuver'}")

# ── Swap USDC natif → USDC.e si nécessaire ───────────────────────────────
if bal_e == 0 and bal_n > 0:
    print(f"\n⚠️  Pas de USDC.e mais {bal_n/1e6:.2f} USDC natif détecté.")
    print("   Swap automatique USDC natif → USDC.e en cours...")
    import time

    usdc_native_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=abi)
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(WALLET))
    tx = usdc_native_contract.functions.approve(
        Web3.to_checksum_address(UNISWAP_V3), 2**256-1
    ).build_transaction({'from': Web3.to_checksum_address(WALLET), 'nonce': nonce,
                         'gas': 100000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print(f"   Approve OK")

    router_abi = [{"inputs":[{"components":[
        {"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},
        {"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},
        {"name":"deadline","type":"uint256"},{"name":"amountIn","type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}
    ],"name":"params","type":"tuple"}],"name":"exactInputSingle",
    "outputs":[{"name":"amountOut","type":"uint256"}],"type":"function"}]
    router = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V3), abi=router_abi)
    amount_in = bal_n - int(1e6)  # garde 1 USDC natif
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(WALLET))
    tx = router.functions.exactInputSingle((
        Web3.to_checksum_address(USDC_NATIVE), Web3.to_checksum_address(USDC_E),
        100, Web3.to_checksum_address(WALLET), int(time.time())+300,
        amount_in, int(amount_in*0.995), 0
    )).build_transaction({'from': Web3.to_checksum_address(WALLET), 'nonce': nonce,
                          'gas': 300000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print(f"   Swap {'OK' if receipt.status==1 else 'FAILED'} — TX: {txh.hex()}")
    bal_e = usdc_e.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()
    print(f"   USDC.e balance: {bal_e/1e6:.2f}")

# ── Approve CTF Exchange si nécessaire ───────────────────────────────────
if allow == 0 and bal_e > 0:
    print(f"\n⚠️  Allowance non accordée. Approbation en cours...")
    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(WALLET))
    tx = usdc_e.functions.approve(
        Web3.to_checksum_address(CTF_EXCHANGE), 2**256-1
    ).build_transaction({'from': Web3.to_checksum_address(WALLET), 'nonce': nonce,
                         'gas': 100000, 'gasPrice': w3.eth.gas_price, 'chainId': 137})
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=60)
    print(f"   Allowance {'OK' if receipt.status==1 else 'FAILED'} — TX: {txh.hex()}")

# ── Dérivation API keys ───────────────────────────────────────────────────
print(f"\n── API KEYS POLYMARKET ──")
client = ClobClient("https://clob.polymarket.com",
    key=PRIVATE_KEY, chain_id=POLYGON, signature_type=0)
creds = client.create_or_derive_api_creds()
print(f"✅ Clés dérivées depuis ta clé privée")

print(f"\n{'='*60}")
print(f"  VARIABLES D'ENVIRONNEMENT À EXPORTER")
print(f"{'='*60}")
print(f"export POLY_PRIVATE_KEY={PRIVATE_KEY}")
print(f"export POLY_API_KEY={creds.api_key}")
print(f"export POLY_API_SECRET={creds.api_secret}")
print(f"export POLY_PASSPHRASE={creds.api_passphrase}")
print(f"\n{'='*60}")
print(f"  LANCEMENT")
print(f"{'='*60}")
print(f"nohup python3 /opt/polymarket-live/live_bot.py > /dev/null 2>&1 &")
print(f"tail -f /opt/polymarket-live/live.log")
