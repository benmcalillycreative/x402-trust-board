"""Real payments on Solana: the same count as chain.py, for sellers paid in USDC on Solana.

On Solana a payment to a wallet lands in that wallet's USDC token account, not the wallet
itself. So for each Solana address an endpoint asks to be paid to, this module:

  1. finds its USDC token accounts (getTokenAccountsByOwner),
  2. lists their transaction signatures back to 30 days ago (getSignaturesForAddress,
     1,000 per call; failed transactions are skipped without fetching),
  3. fetches each new transaction once (getTransaction) and reads the token balance changes:
     a rise in the seller's account is a payment, and the account that fell is the payer.

Every transaction seen is remembered, so later runs only fetch new ones, and every run is
capped in time. An address is counted (shown) only once its whole 30 days have been read and
it was caught up within the last two days. Accounts with more than SOL_MAX_TX transactions
in the window are skipped: a public RPC can't read them in reasonable time, and partial
numbers would mislead.
"""

import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db

CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sol_accounts (
    owner        TEXT NOT NULL,      -- the address the endpoint asks to be paid to (case kept: base58)
    account      TEXT NOT NULL,      -- its USDC token account
    newest_sig   TEXT,               -- newest signature fully processed
    synced_at    TEXT,               -- when the last full pass finished
    too_busy     INTEGER DEFAULT 0,  -- more transactions than a public RPC can read; not counted
    PRIMARY KEY (owner, account)
);
CREATE TABLE IF NOT EXISTS sol_owners (
    owner        TEXT PRIMARY KEY,
    accounts_at  TEXT                -- when its token accounts were last looked up
);
CREATE TABLE IF NOT EXISTS sol_txs (
    sig          TEXT NOT NULL,
    account      TEXT NOT NULL,
    ts           TEXT NOT NULL,
    from_owner   TEXT,               -- the payer's wallet, when this was an incoming payment
    value        INTEGER NOT NULL,   -- USDC atomic units received (0 = not a payment)
    PRIMARY KEY (sig, account)
);
CREATE INDEX IF NOT EXISTS sol_txs_account ON sol_txs (account, ts);
"""


class RateLimited(Exception):
    pass


_state = {"last": 0.0, "next": 0, "cooldown": {}}


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    urls = list(dict.fromkeys(config.SOL_RPC_URLS))
    waited = 0.0
    for _ in range(len(urls) * 4 + 10):
        now = time.monotonic()
        ready = [u for u in urls if _state["cooldown"].get(u, 0) <= now]
        if not ready:
            wait = max(1.0, min(_state["cooldown"].get(u, 0) for u in urls) - now)
            if waited + wait > config.CHAIN_MAX_WAIT:
                raise RateLimited("all Solana RPCs are rate-limiting us")
            time.sleep(wait)
            waited += wait
            continue
        url = ready[_state["next"] % len(ready)]
        gap = config.SOL_MIN_INTERVAL - (time.monotonic() - _state["last"])
        if gap > 0:
            time.sleep(gap)
        _state["last"] = time.monotonic()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            hold = 30 if exc.code == 429 else 60
            try:
                hold = max(hold, float(exc.headers.get("Retry-After") or 0))
            except ValueError:
                pass
            _state["cooldown"][url] = time.monotonic() + hold
            _state["next"] += 1
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
            _state["cooldown"][url] = time.monotonic() + 60
            _state["next"] += 1
            continue
        err = out.get("error")
        if err:
            msg = str(err.get("message", err)).lower()
            if err.get("code") in (429, -32005) or any(w in msg for w in ("rate", "too many", "limit")):
                _state["cooldown"][url] = time.monotonic() + 30
                _state["next"] += 1
                continue
            raise RuntimeError(err)
        return out.get("result")
    raise RateLimited("all Solana RPCs are rate-limiting or failing")


def tracked_owners(conn):
    """Solana addresses endpoints asked to be paid to, in USDC, in the scoring window (case kept)."""
    since = (datetime.now(timezone.utc) - timedelta(days=config.SCORE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_host = defaultdict(set)
    for host, a in conn.execute(
            "SELECT e.host, p.quoted_pay_to FROM probes p JOIN endpoints e USING (resource) "
            "WHERE p.ts >= ? AND p.quoted_network = ? AND lower(p.quoted_asset) = lower(?) "
            "AND p.quoted_pay_to IS NOT NULL", (since, CHAIN, USDC_MINT)):
        by_host[host].add(a.strip())
    out = set()
    for addrs in by_host.values():
        if len(addrs) <= 5:  # a fresh address per quote can't be counted this way (see chain.py)
            out |= addrs
    return sorted(out)


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _amount(b):
    try:
        return int(((b or {}).get("uiTokenAmount") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def read_payment(tx, account):
    """(value received by `account`, payer wallet) from one getTransaction result."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return 0, None
    keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    keys += list(((meta.get("loadedAddresses") or {}).get("writable") or []))
    keys += list(((meta.get("loadedAddresses") or {}).get("readonly") or []))
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or [] if b.get("mint") == USDC_MINT}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or [] if b.get("mint") == USDC_MINT}
    delta = {i: _amount(post.get(i)) - _amount(pre.get(i)) for i in set(pre) | set(post)}
    mine = [i for i, k in enumerate(keys) if k == account]
    got = sum(delta.get(i, 0) for i in mine)
    if got <= 0:
        return 0, None
    payer_idx = min((i for i in delta if i not in mine), key=lambda i: delta[i], default=None)
    if payer_idx is None or delta[payer_idx] >= 0:
        return got, None
    payer = (pre.get(payer_idx) or post.get(payer_idx) or {}).get("owner")
    return got, payer


def run(conn=None, log=print, budget_seconds=None):
    conn = conn or db.connect()
    conn.executescript(SCHEMA)
    started = time.monotonic()
    budget = budget_seconds if budget_seconds is not None else config.SOL_MAX_SECONDS
    since_t = time.time() - config.CHAIN_WINDOW_DAYS * 86400
    owners = tracked_owners(conn)
    fetched = finished_n = 0
    limited = None
    out_of_time = False

    def over():
        return time.monotonic() - started > budget

    try:
        for owner in owners:
            if over():
                out_of_time = True
                break
            row = conn.execute("SELECT accounts_at FROM sol_owners WHERE owner=?", (owner,)).fetchone()
            stale = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not row or not row[0] or row[0] < stale:
                res = rpc("getTokenAccountsByOwner", [owner, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
                for acc in (res or {}).get("value") or []:
                    conn.execute("INSERT OR IGNORE INTO sol_accounts (owner, account) VALUES (?, ?)",
                                 (owner, acc["pubkey"]))
                conn.execute("INSERT OR REPLACE INTO sol_owners VALUES (?, ?)", (owner, db.now()))
                conn.commit()
            for account, newest, busy in conn.execute(
                    "SELECT account, newest_sig, too_busy FROM sol_accounts WHERE owner=?", (owner,)).fetchall():
                if busy or over():
                    out_of_time = out_of_time or over()
                    continue
                # List signatures from now back to the last fully processed one (or 30 days).
                sigs, before, top = [], None, None
                while True:
                    opts = {"limit": PAGE}
                    if before:
                        opts["before"] = before
                    if newest:
                        opts["until"] = newest
                    page = rpc("getSignaturesForAddress", [account, opts]) or []
                    if top is None and page:
                        top = page[0]["signature"]
                    reached = False
                    for s in page:
                        if s.get("blockTime") is not None and s["blockTime"] < since_t:
                            reached = True
                            break
                        sigs.append(s)
                    if reached or len(page) < PAGE:
                        break
                    if len(sigs) > config.SOL_MAX_TX:
                        break
                    before = page[-1]["signature"]
                if len(sigs) > config.SOL_MAX_TX:
                    conn.execute("UPDATE sol_accounts SET too_busy=1 WHERE owner=? AND account=?", (owner, account))
                    conn.commit()
                    continue
                known = {r[0] for r in conn.execute("SELECT sig FROM sol_txs WHERE account=?", (account,))}
                todo = [s for s in sigs if s["signature"] not in known]
                complete = True
                for s in todo:
                    if over():
                        complete = False
                        out_of_time = True
                        break
                    ts = _iso(s.get("blockTime") or time.time())
                    value, payer = 0, None
                    if s.get("err") is None:
                        tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                                     "maxSupportedTransactionVersion": 0}])
                        value, payer = read_payment(tx, account)
                        fetched += 1
                    conn.execute("INSERT OR IGNORE INTO sol_txs VALUES (?,?,?,?,?)",
                                 (s["signature"], account, ts, payer, value))
                    if fetched % 50 == 0:
                        conn.commit()
                if complete:
                    conn.execute("UPDATE sol_accounts SET newest_sig=COALESCE(?, newest_sig), synced_at=? "
                                 "WHERE owner=? AND account=?", (top, db.now(), owner, account))
                    finished_n += 1
                conn.commit()
    except RateLimited as exc:
        limited = str(exc)

    cutoff = _iso(time.time() - (config.CHAIN_WINDOW_DAYS + 5) * 86400)
    conn.execute("DELETE FROM sol_txs WHERE ts < ?", (cutoff,))
    conn.commit()
    log(f"Solana payments: {len(owners):,} addresses, {fetched:,} transactions read"
        + ("; more next cycle" if out_of_time else "") + (f"; stopped early: {limited}" if limited else ""))
    return {"owners": len(owners), "fetched": fetched, "finished": not out_of_time and not limited}


def summaries(conn):
    """Same shape as chain.summaries, keyed by Solana address, for owners fully counted."""
    conn.executescript(SCHEMA)
    since = _iso(time.time() - config.CHAIN_WINDOW_DAYS * 86400)
    fresh = _iso(time.time() - 2 * 86400)
    accounts = defaultdict(list)
    complete = {}
    for owner, account, synced, busy in conn.execute(
            "SELECT owner, account, synced_at, too_busy FROM sol_accounts"):
        accounts[owner].append(account)
        complete[owner] = complete.get(owner, True) and not busy and bool(synced) and synced >= fresh
    owners_of = defaultdict(set)  # a service's own other wallets, so self-payments can be told apart
    for owner in accounts:
        owners_of[owner].add(owner)
    out = {}
    for owner, accs in accounts.items():
        if not complete.get(owner):
            continue
        txs = [(f, v) for acc in accs for f, v in conn.execute(
            "SELECT from_owner, value FROM sol_txs WHERE account=? AND ts >= ? AND value > 0", (acc, since))]
        mine = owners_of[owner]
        own_n = sum(1 for f, _ in txs if f in mine)
        senders = Counter(f for f, _ in txs if f and f not in mine)
        outside = len(txs) - own_n
        out[owner] = {
            "payments": len(txs),
            "payers": len(senders),
            "usd": round(sum(v for _, v in txs) / 1e6, 2),
            "own_share": round(own_n / len(txs), 3) if txs else 0.0,
            "top_share": round(senders.most_common(1)[0][1] / outside, 3) if outside else 0.0,
        }
    return out
