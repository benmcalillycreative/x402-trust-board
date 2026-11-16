"""Reading payment terms from a 402 response.

x402: v1 puts terms in the JSON body, v2 in a base64 PAYMENT-REQUIRED header.
MPP (Machine Payments Protocol): one or more `WWW-Authenticate: Payment ...`
challenges, each with a base64url JSON `request` (amount, currency, recipient).
MPP terms are translated into the same shape as x402's `accepts` list, so the
rest of the board treats both protocols alike.
"""

import base64
import json
import re

# Legacy v1 network names -> CAIP-2 ids used by v2.
NETWORK_ALIASES = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "ethereum": "eip155:1",
    "polygon": "eip155:137",
    "avalanche": "eip155:43114",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VLGtfcB",
}

# Known USDC contracts (6 decimals). Lower-cased for EVM addresses.
USDC_ASSETS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # Base
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # Base Sepolia
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # Ethereum
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # Polygon
    "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",  # Avalanche
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # Arbitrum
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # Optimism
    "epjfwdd5aufqssqem2qn1xzybapc8g4wegggkzwytdt1v",  # Solana mainnet
    "4zmmc9srt5ri5x14gagxhahii3gnpaeerypjgzjdncdu",  # Solana devnet
}

# Tempo, the chain most MPP services settle on. Its USD stablecoins are TIP-20 tokens.
TEMPO_CHAIN_ID = 4217
USD_STABLES_6DP = {
    "0x20c000000000000000000000b9537d11c60e8b50",  # USDC on Tempo (the MPP default)
    "0x20c0000000000000000000000000000000000000",  # pathUSD (Tempo)
}

TESTNETS = {"eip155:84532", "eip155:11155111", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
            "solana:EtWTRABZaYq6iMfeYKouRu166VLGtfcB"}


def norm_network(network):
    if not network:
        return None
    return NETWORK_ALIASES.get(network, network)


def norm_addr(addr):
    return addr.lower() if isinstance(addr, str) else None


def to_usd(amount, asset):
    """Convert atomic units to dollars when the asset is a known USDC contract."""
    if amount is None or not asset:
        return None
    a = norm_addr(asset)
    try:
        if a in USDC_ASSETS or a in USD_STABLES_6DP:
            return int(str(amount)) / 1_000_000
        if a == "usd":  # MPP card payments through Stripe: amounts in cents
            return int(str(amount)) / 100
    except ValueError:
        return None
    return None


def pick_requirement(accepts):
    """Choose the payment option we compare against: prefer mainnet USDC 'exact'."""
    if not accepts:
        return None
    def rank(req):
        net = norm_network(req.get("network"))
        return (
            net in TESTNETS,
            not req.get("payTo"),  # MPP card options have no address to check; prefer one that does
            norm_addr(req.get("asset")) not in USDC_ASSETS | USD_STABLES_6DP,
            req.get("scheme") != "exact",
        )
    return sorted(accepts, key=rank)[0]


def requirement_fields(req):
    """Flatten one accepts[] entry from either protocol version."""
    if not req:
        return {}
    amount = req.get("amount", req.get("maxAmountRequired"))
    asset = req.get("asset")
    return {
        "amount": str(amount) if amount is not None else None,
        "usd": to_usd(amount, asset),
        "network": norm_network(req.get("network")),
        "asset": asset,
        "pay_to": req.get("payTo"),
        "scheme": req.get("scheme"),
    }


def decode_header(value):
    """PAYMENT-REQUIRED is base64 JSON. Tolerate missing padding and url-safe alphabet."""
    raw = value.strip()
    raw += "=" * (-len(raw) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return json.loads(decoder(raw))
        except Exception:
            continue
    return None


_PARAM = re.compile(r'([A-Za-z][\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


def _b64url_json(value):
    raw = (value or "").strip()
    raw += "=" * (-len(raw) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(raw))
    except Exception:
        return None


def mpp_network(method, details):
    """CAIP-2-style id for an MPP payment method."""
    chain = (details or {}).get("chainId")
    if method in ("tempo", "evm") or chain:
        return f"eip155:{chain or TEMPO_CHAIN_ID}"
    return method or None


def parse_mpp(values):
    """`WWW-Authenticate: Payment ...` header values -> x402-style accepts list."""
    accepts = []
    for value in values:
        # Several challenges can share one header line: split before each "Payment ".
        for chunk in re.split(r",\s*(?=Payment\s)", value):
            chunk = chunk.strip()
            if not chunk.lower().startswith("payment "):
                continue
            params = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
                      for m in _PARAM.finditer(chunk[8:])}
            req = _b64url_json(params.get("request"))
            if not isinstance(req, dict):
                continue
            method = params.get("method")
            accepts.append({
                "scheme": "exact" if params.get("intent", "charge") == "charge" else "upto",
                "network": mpp_network(method, req.get("methodDetails")),
                "amount": req.get("amount"),
                "asset": req.get("currency"),
                "payTo": req.get("recipient"),
                "mppMethod": method,
                "mppIntent": params.get("intent"),
            })
    return accepts


def parse_402(headers, body_bytes):
    """Return (protocol, terms) where terms has an x402-style "accepts" list, or ('none', None).

    `headers` may be a dict or a list of (name, value) pairs; use pairs so that
    repeated WWW-Authenticate headers (one per MPP payment method) all survive.
    """
    pairs = list(headers.items()) if hasattr(headers, "items") else list(headers or [])
    header = None
    challenges = []
    for key, value in pairs:
        k = key.lower()
        if k == "payment-required" and header is None:
            header = value
        elif k == "www-authenticate" and "payment" in (value or "").lower():
            challenges.append(value)
    if header:
        decoded = decode_header(header)
        if isinstance(decoded, dict) and decoded.get("accepts"):
            return "v2", decoded

    try:
        body = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("accepts"):
        version = body.get("x402Version", 1)
        return ("v2" if version == 2 else "v1"), body
    if challenges:
        accepts = parse_mpp(challenges)
        if accepts:
            return "mpp", {"accepts": accepts}
    return "none", None
