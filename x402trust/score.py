"""Turn probe history into a trust score (0-100), a grade, and plain-English flags.

Weights (tune as the data teaches you):
  50%  uptime        share of checks where the server answered (402 with terms, served,
                     or asked for input first); TLS problems on our side don't count
  15%  speed         median response time (<=0.5s full marks, >=5s zero)
  20%  consistency   does it ask to be paid where, and roughly what, its listing says?
  15%  adoption      real unique payers in the last 30 days (from the catalog)

Lessons from the first real week of data, built in here:
  - Many honest services use a fresh payment address per request, or pay into another
    wallet the same service lists. Neither is a problem, so neither is penalised.
  - Prices often depend on the request (AI calls priced by size). Cheaper than listed,
    or up to 2x, is normal. 2-10x is a warning; over 10x is a red flag.
  - Asking to be paid to the zero address burns the payer's money: automatic F.
  - The real hijack signature is a wallet that USED to match the listing and then
    switched to an address no listing anywhere mentions. That's an automatic F.
"""

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import config, db
from .parse import TESTNETS, norm_addr, norm_network
from .probe import classify_status, is_template
from . import notes as owner_notes
from . import chain, payout

FLAG_TEXT = {
    # red
    "wallet_switched": "Payment wallet switched to an address no listing mentions",
    "burn_address": "Asks to be paid to the zero address (funds would be lost)",
    "example_offer": "Bare URL returns an example offer (zero address) that can't be paid; real offers need order details",
    "overcharge_10x": "Asks over 10× its listed price",
    "private_address": "Points at a private network address",
    "payout_mismatch": "Asks for payment to an address its own site doesn't list",
    # warnings
    "overcharge_2x": "Asks 2–10× its listed price",
    "unlisted_wallet": "Pays to a wallet not in its listing",
    "payout_changed": "Started asking for payment to a new address",
    "self_paid": "Most payments to its wallet came from the seller's own wallets",
    "one_main_payer": "Nearly all payments to its wallet come from one other wallet",
    "shared_wallet": "Pays to a wallet a different service lists",
    "free_but_listed": "Listed as paid but answered for free",
    "broken_terms": "Returns 402 without readable payment terms",
    "dead": "Never answered in this window",
    "gone": "Listed URL returns 404",
    "flaky": "Unreliable (under 90% uptime)",
    "test_network_only": "Listed on a live network but asks for test-network payment",
    "slow": "Slow (median over 3 seconds)",
    # good
    "payout_verified_dns": "Payout address verified (domain DNS + wallet signature)",
    "payout_verified": "Payout address verified (its site + wallet signature)",
    # info (no penalty)
    "payout_declared": "Payout address listed by its own site (not signed)",
    "rotating_wallet": "Fresh payment address each request",
    "sibling_wallet": "Pays into another wallet this service lists",
    "network_changed": "Now asks for payment on a different network than its listing",
    "price_varies": "Price varies by request (within 2×)",
    "variable_price": "Variable pricing (up-to scheme)",
    "needs_input": "Wants input before quoting a price",
    "redirects": "Listed URL redirects elsewhere",
    "route_pattern": "Listed as a route pattern (e.g. /:id), partly checkable",
    "listing_farm": "One of thousands of near-identical listings from this host",
    "retired_by_owner": "Retired by owner",
    "owner_confirmed": "Owner confirmed these terms are intended",
    "delisted": "No longer in the catalog",
    "testnet": "Test network only (not real money)",
    "no_payers": "No paying customers in 30 days",
}
# Red = a confirmed danger to the payer's money: it would go somewhere it shouldn't. A price far above the
# listing is a warning, not red: a stale listing explains it as often as a bad seller does, and the
# payer sees the real price in the 402 before signing.
RED_FLAGS = {"wallet_switched", "burn_address", "private_address", "payout_mismatch"}
GOOD_FLAGS = {"payout_verified_dns", "payout_verified"}
WARN_FLAGS = {"overcharge_10x", "overcharge_2x", "unlisted_wallet", "payout_changed", "self_paid", "shared_wallet", "free_but_listed",
              "broken_terms", "dead", "gone", "flaky", "slow", "test_network_only"}

UP = {"healthy", "free", "needs_input", "redirect"}
NOT_COUNTED = {"inconclusive"}


def severity(flag):
    if flag in GOOD_FLAGS:
        return "good"
    return "red" if flag in RED_FLAGS else "warn" if flag in WARN_FLAGS else "info"


def grade_for(score):
    for cutoff, grade in ((85, "A"), (70, "B"), (55, "C"), (40, "D")):
        if score >= cutoff:
            return grade
    return "F"


def outcome_of(p):
    """Normalise older probe rows (before v0.4 split out these cases)."""
    out = p["outcome"]
    if out in ("http_error", "not_found") and p["status_code"]:
        return classify_status(p["status_code"], p["resource"])
    if out == "unreachable" and any(m in (p["error"] or "") for m in
                                    ("PROTOCOL_VERSION", "TLSV1_ALERT_ACCESS_DENIED")):
        return "inconclusive"
    return out


def domain_of(url):
    """A rough 'same service' key: registrable domain, or the full host on shared platforms."""
    host = (urlsplit(url).hostname or "").lower()
    shared = ("workers.dev", "onrender.com", "run.app", "railway.app", "vercel.app",
              "netlify.app", "fly.dev", "amazonaws.com", "code.run", "herokuapp.com",
              "pages.dev", "github.io", "replit.app", "duckdns.org")
    if host.endswith(shared):
        return host
    return ".".join(host.split(".")[-2:])


class Context:
    """Catalog-wide facts computed once per scoring run."""

    def __init__(self, conn):
        self.wallet_domains = defaultdict(set)   # wallet -> domains whose listings name it
        self.host_listings = defaultdict(int)
        self.host_payers = defaultdict(list)
        for row in conn.execute("SELECT resource, host, catalog_json, l30_payers FROM endpoints"):
            self.host_listings[row["host"]] += 1
            self.host_payers[row["host"]].append(row["l30_payers"] or 0)
            try:
                accepts = json.loads(row["catalog_json"] or "{}").get("accepts") or []
            except ValueError:
                accepts = []
            d = domain_of(row["resource"])
            for a in accepts:
                if a.get("payTo"):
                    self.wallet_domains[norm_addr(a["payTo"])].add(d)
        self.farm_hosts = {
            h for h, n in self.host_listings.items()
            if n >= 1000 and statistics.median(self.host_payers[h]) <= 1
        }
        self.rotating_hosts = set()
        self.notes = owner_notes.load()
        self.payout = payout.load(conn)  # {host: {address: level}} from sellers' own proofs
        try:
            self.onchain = chain.summaries(conn)  # {address: real USDC payments received on Base}
        except Exception:
            self.onchain = {}
        try:
            from . import chain_sol
            self.onchain_sol = chain_sol.summaries(conn)  # {address (case kept): ... on Solana}
        except Exception:
            self.onchain_sol = {}

    def find_rotating_hosts(self, probes_by_host):
        """Hosts that hand out fresh, never-listed payment addresses.

        Two patterns: a new address on nearly every request, or a per-caller deposit
        address (e.g. Stripe on Base) that is reused for a while and then replaced, so
        the same caller sees repeats. The second shows up as several distinct
        addresses, none of which any listing names. A hijack points at one wallet,
        so three or more different never-listed addresses means deposit addresses."""
        for host, quotes in probes_by_host.items():
            unlisted = [q for q in quotes if q not in self.wallet_domains]
            distinct = len(set(unlisted))
            if distinct >= 3 and (distinct >= 0.8 * len(unlisted) or len(unlisted) == len(quotes)):
                self.rotating_hosts.add(host)


def catalog_terms(ep):
    try:
        accepts = json.loads(ep["catalog_json"] or "{}").get("accepts") or []
    except ValueError:
        accepts = []
    wallets, amounts, schemes = set(), defaultdict(list), set()
    for a in accepts:
        if a.get("payTo"):
            wallets.add(norm_addr(a["payTo"]))
        amt = a.get("amount", a.get("maxAmountRequired"))
        if amt is not None:
            try:
                amounts[(norm_network(a.get("network")), norm_addr(a.get("asset")))].append(int(str(amt)))
            except ValueError:
                pass
        schemes.add(a.get("scheme"))
    return wallets, amounts, schemes


BURN_ADDRESSES = {"0x0000000000000000000000000000000000000000",
                  "0x000000000000000000000000000000000000dead",
                  "11111111111111111111111111111111"}


def _example(probe):
    """The seller marked this quoted offer as an example (extra.example), not a real quote."""
    try:
        return bool(probe["quoted_example"])
    except (IndexError, KeyError):
        return False


def _source(ep):
    try:
        return ep["source"] or "bazaar"
    except (IndexError, KeyError):
        return "bazaar"


def _protocol(ep):
    try:
        return ep["protocol"] or "x402"
    except (IndexError, KeyError):
        return "x402"


def wallet_flags(ep, healthy, ctx):
    wallets, amounts, _ = catalog_terms(ep)
    with_wallet = [p for p in healthy if p["quoted_pay_to"]]
    if not with_wallet:
        return set()
    latest_quote = norm_addr(with_wallet[-1]["quoted_pay_to"])
    if latest_quote in BURN_ADDRESSES:
        return {"example_offer"} if _example(with_wallet[-1]) else {"burn_address"}
    # The seller's own proof beats anything the listing says (see payout.py).
    declared = getattr(ctx, "payout", {}).get((ep["host"] or "").split(":")[0])
    all_quotes = [norm_addr(p["quoted_pay_to"]) for p in with_wallet]
    fresh_each_time = len(all_quotes) >= 2 and len(set(all_quotes)) == len(all_quotes)
    if declared and ep["host"] not in ctx.rotating_hosts:
        level = declared.get(latest_quote)
        # Only the seller's purpose-made proof (payout file or DNS) can make a mismatch;
        # a general service manifest might just be out of date.
        strong = any(v != "manifest" for v in declared.values())
        if level is None and strong and not fresh_each_time:
            return {"payout_mismatch"}
        if level is not None:
            return {"payout_verified_dns" if level == "dns_signed" else
                    "payout_verified" if level == "site_signed" else "payout_declared"}
    if not wallets:
        return history_flags(ep, all_quotes, fresh_each_time, ctx)
    # A seller moving to another network (most often test network -> live Base) gets a
    # new address there. Until the listing catches up, that isn't a switched wallet.
    listed_networks = {net for net, _ in amounts if net}
    latest_net = norm_network(with_wallet[-1]["quoted_network"])
    if listed_networks and latest_net and latest_net not in listed_networks:
        if latest_net in TESTNETS and listed_networks - TESTNETS:
            return {"test_network_only"}  # listed as real money, only takes test money
        return {"network_changed"}
    with_wallet = [p for p in with_wallet
                   if not listed_networks or norm_network(p["quoted_network"]) in listed_networks]
    quotes = [norm_addr(p["quoted_pay_to"]) for p in with_wallet]
    if not quotes:
        return set()
    burned = [p for p, q in zip(with_wallet, quotes) if q in BURN_ADDRESSES]
    if burned:
        return {"example_offer"} if all(_example(p) for p in burned) else {"burn_address"}
    matches = [q in wallets for q in quotes]
    if all(matches):
        return set()
    unmatched = {q for q, m in zip(quotes, matches) if not m}
    domain = domain_of(ep["resource"])
    known_anywhere = {q for q in unmatched if q in ctx.wallet_domains}
    if (len(quotes) >= 2 and len(set(quotes)) == len(quotes) and not known_anywhere) \
            or ep["host"] in ctx.rotating_hosts:
        return {"rotating_wallet"}
    if all(domain in ctx.wallet_domains.get(q, ()) for q in unmatched):
        return {"sibling_wallet"}
    latest = quotes[-1]
    if any(matches) and not matches[-1] and latest not in ctx.wallet_domains:
        return {"wallet_switched"}
    if known_anywhere:
        return {"shared_wallet"}
    return {"unlisted_wallet"}


def history_flags(ep, quotes, fresh_each_time, ctx):
    """For listings that don't name a wallet (MPP): compare the endpoint with its own past.

    If it asked for the same address several times and now asks for a different one,
    say so. It's a warning, not a red flag: with nothing listed to compare against, a
    seller changing wallets on purpose looks exactly the same. A payout proof settles it."""
    if len(quotes) < 2:
        return set()
    if fresh_each_time or ep["host"] in ctx.rotating_hosts:
        return {"rotating_wallet"}
    latest, earlier = quotes[-1], quotes[:-1]
    if latest not in earlier and len(earlier) >= 2 and len(set(earlier)) == 1:
        return {"payout_changed"}
    return set()


def onchain_flags(healthy, ctx):
    """What the blockchain says about the wallet this endpoint asks to be paid to."""
    from .chain_sol import CHAIN as SOLANA
    paid = [p for p in healthy if p["quoted_pay_to"] and p["quoted_network"] in (chain.CHAIN, SOLANA)]
    if not paid:
        return set()
    last = paid[-1]
    if last["quoted_network"] == SOLANA:
        s = getattr(ctx, "onchain_sol", {}).get(last["quoted_pay_to"].strip())
    else:
        s = getattr(ctx, "onchain", {}).get(norm_addr(last["quoted_pay_to"]))
    if not s or s["payments"] < 20:
        return set()
    if s["own_share"] >= 0.5:
        return {"self_paid"}
    if s["top_share"] >= 0.8:
        return {"one_main_payer"}
    return set()


def price_flags(ep, healthy):
    _, amounts, schemes = catalog_terms(ep)
    if "upto" in schemes:
        return {"variable_price"}
    ratios = []  # one per comparable quote, oldest first
    for p in healthy:
        if not p["quoted_amount"]:
            continue
        key = (p["quoted_network"], norm_addr(p["quoted_asset"]))
        listed = amounts.get(key)
        if not listed:
            continue  # different network/asset than the listing: can't compare fairly
        try:
            quoted = int(str(p["quoted_amount"]))
        except ValueError:
            continue
        top = max(listed)
        ratios.append(1.0 if quoted in listed else (quoted / top if top else float("inf")))
    # Flags describe what a buyer would pay now. If the latest quote is at or under the
    # listing, the seller has fixed it (or changed prices), so older quotes don't count.
    if not ratios or ratios[-1] <= 1:
        return set()
    worst = max(ratios)
    if worst <= 1:
        return set()
    if worst <= 2:
        return {"price_varies"}
    if worst <= 10:
        return {"overcharge_2x"}
    return {"overcharge_10x"}


def score_endpoint(ep, probes, ctx):
    flags = set()
    outcomes = [outcome_of(p) for p in probes]
    counted = [o for o in outcomes if o not in NOT_COUNTED]
    n = len(counted)

    if any(o == "blocked" for o in outcomes) and any("non-public" in (p["error"] or "") for p in probes):
        flags.add("private_address")
    if ep["delisted"]:
        flags.add("delisted")
    if ep["network"] in TESTNETS:
        flags.add("testnet")
    if ep["l30_payers"] == 0:
        flags.add("no_payers")
    if ep["host"] in ctx.farm_hosts:
        flags.add("listing_farm")

    up = [p for p, o in zip(probes, outcomes) if o in UP]
    healthy = [p for p, o in zip(probes, outcomes) if o == "healthy"]
    uptime = len(up) / n if n else 0.0
    if n and not up and "broken_402" not in outcomes:
        flags.add("gone" if all(o == "not_found" for o in counted) else "dead")
    elif n and uptime < 0.9:
        flags.add("flaky")
    if "free" in outcomes:
        flags.add("free_but_listed")
    if "broken_402" in outcomes:
        flags.add("broken_terms")
    if up and all(outcome_of(p) == "needs_input" for p in up):
        flags.add("needs_input")
    if "redirect" in outcomes:
        flags.add("redirects")
    if is_template(ep["resource"]):
        flags.add("route_pattern")
    flags |= onchain_flags(healthy, ctx)

    latencies = [p["latency_ms"] for p in up if p["latency_ms"] is not None]
    p50 = int(statistics.median(latencies)) if latencies else None
    if p50 is not None and p50 > 3000:
        flags.add("slow")
    speed = 0.0 if p50 is None else max(0.0, min(1.0, (5000 - p50) / 4500))

    flags |= wallet_flags(ep, healthy, ctx)
    flags |= price_flags(ep, healthy)

    note = owner_notes.find(ctx.notes, ep["resource"], probes)
    if note and note["status"] == "retired":
        keep = flags & {"delisted", "listing_farm", "route_pattern", "testnet"}
        return {"probes": n, "uptime": round(uptime, 3), "p50_latency_ms": p50,
                "score": 0, "grade": "—", "flags": sorted(keep | {"retired_by_owner"})}
    if note and note["status"] == "intended":
        flags -= {"overcharge_10x", "overcharge_2x", "price_varies", "unlisted_wallet", "payout_changed",
                  "shared_wallet", "wallet_switched", "rotating_wallet", "sibling_wallet",
                  "free_but_listed"}  # e.g. a no-charge precheck when there's nothing to sell
        # A zero-address quote the owner says is only an example offer (real offers need order
        # details, and USDC itself refuses transfers to the zero address) is information, not a red flag.
        if "burn_address" in flags:
            flags.discard("burn_address")
            flags.add("example_offer")
        flags.add("owner_confirmed")

    consistency = 1.0
    for flag, penalty in (
        ("wallet_switched", 1.0), ("burn_address", 1.0), ("payout_mismatch", 1.0), ("overcharge_10x", 0.8), ("overcharge_2x", 0.4),
        ("unlisted_wallet", 0.4), ("shared_wallet", 0.3), ("payout_changed", 0.3), ("self_paid", 0.2),
        ("free_but_listed", 0.3), ("broken_terms", 0.3),
    ):
        if flag in flags:
            consistency -= penalty
    consistency = max(0.0, consistency)

    if ep["l30_payers"] is None and (_protocol(ep) == "mpp" or _source(ep) == "circle"):
        # The MPP directory and Circle's marketplace don't publish usage, so leave adoption out
        # instead of scoring it zero.
        score = round(100 * (0.50 * uptime + 0.15 * speed + 0.20 * consistency) / 0.85)
    else:
        payers = ep["l30_payers"] or 0
        adoption = min(1.0, math.log10(payers + 1) / 3)  # 1,000 payers = full marks
        score = round(100 * (0.50 * uptime + 0.15 * speed + 0.20 * consistency + 0.15 * adoption))
    # Good uptime shouldn't paper over terms an agent would regret paying.
    if flags & {"overcharge_2x", "unlisted_wallet", "shared_wallet", "free_but_listed", "payout_changed"}:
        score = min(score, 69)
    if "overcharge_10x" in flags:
        score = min(score, 45)
    if flags & {"wallet_switched", "burn_address", "payout_mismatch"}:
        score = min(score, 30)
    if "private_address" in flags:
        score = 0

    if flags & {"wallet_switched", "burn_address", "private_address", "payout_mismatch"}:
        grade = "F"
    elif n < config.MIN_PROBES_FOR_GRADE and not (flags & RED_FLAGS):
        grade = "—"  # not enough history yet
    else:
        grade = grade_for(score)

    return {
        "probes": n,
        "uptime": round(uptime, 3),
        "p50_latency_ms": p50,
        "score": score,
        "grade": grade,
        "flags": sorted(flags),
    }


def availability(conn, days=None, now=None):
    """Per endpoint over the last `days` (Coinbase measures featured listings over 30): how many counted checks
    answered, out of how many, and how many days of history that covers. {resource: (up, counted, history_days)}.
    Aggregated in SQL so a month of checks across every listing stays quick."""
    days = days or config.AVAILABILITY_DAYS
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    up, counted, first = defaultdict(int), defaultdict(int), {}
    for r in conn.execute(
        """SELECT resource, outcome, status_code,
                  (COALESCE(error,'') LIKE '%PROTOCOL_VERSION%' OR COALESCE(error,'') LIKE '%TLSV1_ALERT_ACCESS_DENIED%') AS tls,
                  COUNT(*) AS n, MIN(ts) AS first_ts
           FROM probes WHERE ts >= ? GROUP BY resource, outcome, status_code, tls""", (since,)):
        p = {"outcome": r["outcome"], "status_code": r["status_code"], "resource": r["resource"],
             "error": "PROTOCOL_VERSION" if r["tls"] else ""}
        o = outcome_of(p)
        res = r["resource"]
        if res not in first or r["first_ts"] < first[res]:
            first[res] = r["first_ts"]
        if o in NOT_COUNTED:
            continue
        counted[res] += r["n"]
        if o in UP:
            up[res] += r["n"]
    out = {}
    for res, n in counted.items():
        try:
            start = datetime.strptime(first[res], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            hist = max(1, min(days, (now - start).days + 1))
        except (ValueError, KeyError):
            hist = None
        out[res] = (up[res], n, hist)
    return out


def run(conn=None, log=print):
    conn = conn or db.connect()
    since = (datetime.now(timezone.utc) - timedelta(days=config.SCORE_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    ctx = Context(conn)

    probes_by_resource = defaultdict(list)
    quotes_by_host = defaultdict(list)
    for p in conn.execute(
        "SELECT p.*, e.host FROM probes p JOIN endpoints e USING (resource) "
        "WHERE p.ts >= ? ORDER BY p.ts", (since,)
    ):
        probes_by_resource[p["resource"]].append(p)
        if p["outcome"] == "healthy" and p["quoted_pay_to"]:
            quotes_by_host[p["host"]].append(norm_addr(p["quoted_pay_to"]))
    ctx.find_rotating_hosts(quotes_by_host)

    avail = availability(conn)
    ts = db.now()
    graded = 0
    conn.execute("DELETE FROM scores")
    for ep in conn.execute("SELECT * FROM endpoints").fetchall():
        probes = probes_by_resource.get(ep["resource"], [])
        if not probes and ep["delisted"]:
            continue
        s = score_endpoint(ep, probes, ctx)
        a_up, a_n, a_days = avail.get(ep["resource"], (0, 0, None))
        conn.execute(
            "INSERT OR REPLACE INTO scores (resource, computed_at, probes, uptime, p50_latency_ms, score, grade, flags, "
            "uptime_30d, probes_30d, history_days) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ep["resource"], ts, s["probes"], s["uptime"], s["p50_latency_ms"],
             s["score"], s["grade"], json.dumps(s["flags"]),
             round(a_up / a_n, 4) if a_n else None, a_n or None, a_days),
        )
        graded += 1
    conn.commit()
    log(f"Scored {graded:,} endpoints over the last {config.SCORE_WINDOW_DAYS} days")
    return graded
