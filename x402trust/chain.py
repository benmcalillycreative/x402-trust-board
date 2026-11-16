"""Real payments, counted from the blockchain instead of taken from the catalog.

Coinbase's Bazaar publishes call and payer counts for each listing, and those
counts include a lot of self-dealing. Every x402 payment on Base is a USDC
transfer to the seller's pay-to address, so this module counts them directly:
for each address an endpoint asks to be paid to, every USDC transfer it received
in the last 30 days, who sent it, and how much.

It also asks one question about where the money came from: did it come from the
seller's own wallets? A payment from the receiving address itself, or from another
address the same service asks to be paid to, isn't a customer. (Sellers paying
other sellers is normal, since agents buy from each other, so that isn't counted
against anyone.)

How: eth_getLogs over the USDC contract, filtered to Transfer events whose recipient
is one of our addresses. The scan is incremental. Each address remembers the last
block it was synced to, a new address is backfilled 30 days, and every run is capped
in time so a slow public RPC just means the backfill finishes over a few cycles.
Block times are estimated from the chain head (Base makes a block every 2 seconds),
which saves one RPC call per block.
"""

import json
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db
from .parse import norm_addr

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CHAIN = "eip155:8453"
BURN = {"0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_addresses (
    address    TEXT PRIMARY KEY,   -- lowercase pay-to address on Base
    synced_to  INTEGER,            -- last block fully scanned for this address (NULL = not yet)
    added      TEXT
);
CREATE TABLE IF NOT EXISTS chain_transfers (
    tx         TEXT NOT NULL,
    log_index  INTEGER NOT NULL,
    block      INTEGER NOT NULL,
    ts         TEXT NOT NULL,      -- estimated from block number
    to_addr    TEXT NOT NULL,
    from_addr  TEXT NOT NULL,
    value      INTEGER NOT NULL,   -- USDC atomic units (6 decimals)
    PRIMARY KEY (tx, log_index)
);
CREATE INDEX IF NOT EXISTS chain_transfers_to ON chain_transfers (to_addr, ts);
CREATE TABLE IF NOT EXISTS chain_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class RangeTooBig(Exception):
    pass


class RateLimited(Exception):
    """Every RPC we know is refusing us for now. Progress is saved; the next cycle carries on."""


RANGE_WORDS = ("range", "too many", "limit", "exceed", "too large", "max", "timeout", "10000", "results")
_state = {"next": 0, "last": 0.0, "cooldown": {}}


def _urls():
    urls = list(dict.fromkeys(config.CHAIN_RPC_URLS or [config.BASE_RPC_URL]))
    return urls


def _error_from(raw):
    try:
        out = json.loads(raw)
    except Exception:
        return None
    return out.get("error") if isinstance(out, dict) else None


def rpc(method, params):
    """One JSON-RPC call to Base, spread across the configured RPCs.

    Paces requests, skips an RPC for a while after it rate-limits us (honouring Retry-After),
    and moves on to the next one. If they're all refusing, waits a little, and after a few
    rounds raises RateLimited so the caller can stop cleanly and resume next cycle."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    urls = _urls()
    waited = 0.0
    for attempt in range(len(urls) * 4 + 20):
        now = time.monotonic()
        ready = [u for u in urls if _state["cooldown"].get(u, 0) <= now]
        if not ready:
            wait = max(1.0, min(_state["cooldown"].get(u, 0) for u in urls) - now)
            if waited + wait > config.CHAIN_MAX_WAIT:
                raise RateLimited("all Base RPCs are rate-limiting us")
            time.sleep(wait)
            waited += wait
            continue
        url = ready[_state["next"] % len(ready)]
        gap = config.CHAIN_MIN_INTERVAL - (time.monotonic() - _state["last"])
        if gap > 0:
            time.sleep(gap)
        _state["last"] = time.monotonic()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 413:
                raise RangeTooBig("response too large")
            if exc.code == 429:
                try:
                    after = float(exc.headers.get("Retry-After") or 0)
                except ValueError:
                    after = 0
                _state["cooldown"][url] = time.monotonic() + max(after, 30)
                _state["next"] += 1
                continue
            if exc.code in (400, 403, 500, 502, 503, 504):
                err = _error_from(exc.read() or b"")
                if err and any(w in str(err.get("message", err)).lower() for w in RANGE_WORDS):
                    raise RangeTooBig(str(err))
                _state["cooldown"][url] = time.monotonic() + 60
                _state["next"] += 1
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            _state["cooldown"][url] = time.monotonic() + 60
            _state["next"] += 1
            continue
        try:
            out = json.loads(raw)
        except ValueError:
            _state["cooldown"][url] = time.monotonic() + 60
            _state["next"] += 1
            continue
        err = out.get("error")
        if err:
            msg = str(err.get("message", err)).lower()
            if any(w in msg for w in ("rate", "capacity", "credits", "throttl")) or err.get("code") == 429:
                _state["cooldown"][url] = time.monotonic() + 30
                _state["next"] += 1
                continue
            if any(w in msg for w in RANGE_WORDS):
                raise RangeTooBig(msg)
            raise RuntimeError(err)
        return out.get("result")
    raise RateLimited("all Base RPCs are rate-limiting or failing")


def _pad(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def head():
    blk = rpc("eth_getBlockByNumber", ["latest", False])
    return int(blk["number"], 16), int(blk["timestamp"], 16)


def get_logs(lo, hi, group):
    """Transfers to any address in `group` in blocks lo..hi, splitting the request if it's too big."""
    try:
        return rpc("eth_getLogs", [{
            "fromBlock": hex(lo), "toBlock": hex(hi), "address": USDC_BASE,
            "topics": [TRANSFER, None, [_pad(a) for a in group]],
        }]) or []
    except RangeTooBig:
        if hi > lo:
            mid = (lo + hi) // 2
            return get_logs(lo, mid, group) + get_logs(mid + 1, hi, group)
        if len(group) > 1:
            half = len(group) // 2
            return get_logs(lo, hi, group[:half]) + get_logs(lo, hi, group[half:])
        raise


def tracked_addresses(conn):
    """Every address a Base-USDC endpoint asked to be paid to recently, or lists."""
    since = (datetime.now(timezone.utc) - timedelta(days=config.SCORE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = set()
    for (a,) in conn.execute(
            "SELECT DISTINCT quoted_pay_to FROM probes WHERE ts >= ? AND quoted_network = ? "
            "AND lower(quoted_asset) = ? AND quoted_pay_to IS NOT NULL", (since, CHAIN, USDC_BASE)):
        out.add(norm_addr(a))
    for (a,) in conn.execute(
            "SELECT DISTINCT pay_to FROM endpoints WHERE delisted = 0 AND network = ? AND lower(asset) = ? "
            "AND pay_to IS NOT NULL", (CHAIN, USDC_BASE)):
        out.add(norm_addr(a))
    return sorted(a for a in out if a and a.startswith("0x") and len(a) == 42 and a not in BURN)


def run(conn=None, log=print, budget_seconds=None):
    conn = conn or db.connect()
    conn.executescript(SCHEMA)
    started = time.monotonic()
    budget = budget_seconds if budget_seconds is not None else config.CHAIN_MAX_SECONDS
    try:
        now_block, now_ts = head()
    except RateLimited as exc:
        log(f"On-chain payments: skipped this cycle ({exc}); progress is kept")
        return {"addresses": 0, "transfers": 0, "finished": False, "rate_limited": True}
    conn.execute("INSERT OR REPLACE INTO chain_meta VALUES ('head', ?)", (str(now_block),))
    conn.commit()
    window = int(config.CHAIN_WINDOW_DAYS * 86400 / config.BASE_BLOCK_SECONDS)
    floor = max(0, now_block - window)

    addrs = tracked_addresses(conn)
    conn.executemany("INSERT OR IGNORE INTO chain_addresses (address, added) VALUES (?, ?)",
                     [(a, db.now()) for a in addrs])
    conn.commit()
    wanted = set(addrs)

    # Group addresses by where their scan should resume, so each block range is fetched once.
    resume = defaultdict(list)
    for a, synced in conn.execute("SELECT address, synced_to FROM chain_addresses"):
        if a in wanted:
            resume[max(synced or 0, floor - 1)].append(a)

    def ts_of(block):
        return datetime.fromtimestamp(now_ts - (now_block - block) * config.BASE_BLOCK_SECONDS,
                                      timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    step, group_size = config.CHAIN_BLOCK_STEP, config.CHAIN_GROUP
    found = 0
    finished = True
    limited = None
    for start in sorted(resume):
        group_all = resume[start]
        lo = start + 1
        while lo <= now_block:
            if time.monotonic() - started > budget:
                finished = False
                break
            hi = min(lo + step - 1, now_block)
            try:
                logs = [lg for i in range(0, len(group_all), group_size)
                        for lg in get_logs(lo, hi, group_all[i:i + group_size])]
            except (RateLimited, RangeTooBig) as exc:
                limited = str(exc)[:120]
                finished = False
                break
            for lg in logs:
                topics = lg.get("topics") or []
                if len(topics) < 3:
                    continue
                conn.execute("INSERT OR IGNORE INTO chain_transfers VALUES (?,?,?,?,?,?,?)", (
                    lg["transactionHash"], int(lg["logIndex"], 16), int(lg["blockNumber"], 16),
                    ts_of(int(lg["blockNumber"], 16)), "0x" + topics[2][-40:].lower(),
                    "0x" + topics[1][-40:].lower(), int(lg.get("data") or "0x0", 16)))
                found += 1
            conn.executemany("UPDATE chain_addresses SET synced_to=? WHERE address=?",
                             [(hi, a) for a in group_all])
            conn.commit()
            lo = hi + 1
        if not finished:
            break

    cutoff = ts_of(max(0, now_block - int((config.CHAIN_WINDOW_DAYS + 5) * 86400 / config.BASE_BLOCK_SECONDS)))
    conn.execute("DELETE FROM chain_transfers WHERE ts < ?", (cutoff,))
    conn.commit()
    behind = conn.execute("SELECT COUNT(*) FROM chain_addresses WHERE synced_to IS NULL OR synced_to < ?",
                          (now_block,)).fetchone()[0]
    log(f"On-chain payments: {len(addrs):,} Base addresses, {found:,} new transfers"
        + ("" if finished else f"; {behind:,} addresses still catching up (continues next cycle)")
        + (f"; stopped early: {limited}" if limited else ""))
    return {"addresses": len(addrs), "transfers": found, "finished": finished, "rate_limited": bool(limited)}


# ---------- summaries ----------

def own_wallets(conn):
    """{address: set(addresses the same service asks to be paid to)}, from recent quotes and listings."""
    by_host = defaultdict(set)
    since = (datetime.now(timezone.utc) - timedelta(days=config.SCORE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for host, a in conn.execute(
            "SELECT e.host, p.quoted_pay_to FROM probes p JOIN endpoints e USING (resource) "
            "WHERE p.ts >= ? AND p.quoted_pay_to IS NOT NULL", (since,)):
        by_host[host].add(norm_addr(a))
    for host, a in conn.execute("SELECT host, pay_to FROM endpoints WHERE pay_to IS NOT NULL"):
        by_host[host].add(norm_addr(a))
    out = defaultdict(set)
    for addrs in by_host.values():
        for a in addrs:
            out[a] |= addrs
    return out


def summaries(conn):
    """{address: {payments, payers, usd, own_share, top_share, synced}} for the last 30 days."""
    conn.executescript(SCHEMA)
    since = (datetime.now(timezone.utc) - timedelta(days=config.CHAIN_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    own = own_wallets(conn)
    rows = defaultdict(list)
    for to, frm, value in conn.execute(
            "SELECT to_addr, from_addr, value FROM chain_transfers WHERE ts >= ?", (since,)):
        rows[to].append((frm, value))
    synced = {a: s for a, s in conn.execute("SELECT address, synced_to FROM chain_addresses")}
    row = conn.execute("SELECT value FROM chain_meta WHERE key='head'").fetchone()
    # Only addresses scanned up to (nearly) the chain head: a 30-day count that stops
    # partway through the backfill would look like a seller with no customers.
    latest = int(row[0]) if row else max((s for s in synced.values() if s), default=0)
    lag = int(2 * 86400 / config.BASE_BLOCK_SECONDS)
    out = {}
    for addr, s in synced.items():
        if s is None or s < latest - lag:
            continue  # still backfilling: partial counts would mislead
        txs = rows.get(addr, [])
        mine = own.get(addr, set()) | {addr}
        own_n = sum(1 for f, _ in txs if f in mine)
        senders = Counter(f for f, _ in txs if f not in mine)
        outside = len(txs) - own_n
        out[addr] = {
            "payments": len(txs),
            "payers": len(senders),
            "usd": round(sum(v for _, v in txs) / 1e6, 2),
            "own_share": round(own_n / len(txs), 3) if txs else 0.0,
            "top_share": round(senders.most_common(1)[0][1] / outside, 3) if outside else 0.0,
        }
    return out
