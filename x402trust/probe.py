"""Knock on every listed endpoint without paying, and record what answers.

A healthy x402 endpoint answers an unpaid request with HTTP 402 and readable
payment terms. We log that answer, how fast it came, and whether the terms
match what the catalog claims. No wallet is involved; nothing is ever paid.
"""

import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from . import config, db, netguard
from .parse import norm_addr, parse_402, pick_requirement, requirement_fields


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a redirect could point at a private address."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(NoRedirect)
_host_locks = defaultdict(lambda: threading.BoundedSemaphore(config.PER_HOST_LIMIT))
_host_locks_guard = threading.Lock()


def _host_sem(host):
    with _host_locks_guard:
        return _host_locks[host]


# Circuit breaker: one dead host can carry thousands of listings. After a few
# connection failures in a row, mark the rest of that host's endpoints down for
# this cycle instead of waiting out a timeout on each one.
HOST_FAILS_TO_TRIP = 5
_host_fails = defaultdict(int)


def _host_tripped(host):
    with _host_locks_guard:
        return _host_fails[host] >= HOST_FAILS_TO_TRIP


def _host_result(host, reachable):
    with _host_locks_guard:
        _host_fails[host] = 0 if reachable else _host_fails[host] + 1


def probe_one(row):
    """Probe a single endpoint row. Returns a dict ready for the probes table."""
    resource, method = row["resource"], (row["method"] or "GET").upper()
    result = {"resource": resource, "ts": db.now(), "protocol": "none"}
    try:
        netguard.check_url(resource)
    except netguard.Blocked as exc:
        return {**result, "outcome": "blocked", "error": str(exc)[:300]}
    host = urlsplit(resource).netloc.lower()  # host:port, for politeness and the breaker

    data, headers = None, {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    if method in ("POST", "PUT", "PATCH"):
        data, headers["Content-Type"] = b"{}", "application/json"
    req = urllib.request.Request(resource, data=data, headers=headers, method=method)

    with _host_sem(host):
        if _host_tripped(host):
            return {**result, "outcome": "unreachable",
                    "error": "host down this cycle (skipped after repeated failures)"}
        start = time.monotonic()
        try:
            resp = _opener.open(req, timeout=config.PROBE_TIMEOUT)
            status, resp_headers, body = resp.status, list(resp.headers.items()), resp.read(65536)
            _host_result(host, True)
        except urllib.error.HTTPError as exc:
            _host_result(host, True)
            status, resp_headers = exc.code, list((exc.headers or {}).items())
            try:
                body = exc.read(65536)
            except Exception:
                body = b""
        except Exception as exc:
            if _client_tls_problem(exc):
                # Our Python's TLS is too old for this server: not the endpoint's fault.
                return {**result, "outcome": "inconclusive",
                        "error": f"{type(exc).__name__}: {exc}"[:300]}
            _host_result(host, False)
            return {
                **result,
                "outcome": "unreachable",
                "latency_ms": int((time.monotonic() - start) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        latency = int((time.monotonic() - start) * 1000)

    result.update(status_code=status, latency_ms=latency)
    if status == 402:
        protocol, terms = parse_402(resp_headers, body)
        if not terms:
            return {**result, "outcome": "broken_402", "error": "402 without readable terms"}
        quoted = requirement_fields(pick_requirement(terms.get("accepts")))
        result.update(
            outcome="healthy",
            protocol=protocol,
            quoted_amount=quoted.get("amount"),
            quoted_usd=quoted.get("usd"),
            quoted_network=quoted.get("network"),
            quoted_asset=quoted.get("asset"),
            quoted_pay_to=quoted.get("pay_to"),
        )
        if row["pay_to"] and quoted.get("pay_to"):
            result["payto_matches"] = int(norm_addr(row["pay_to"]) == norm_addr(quoted["pay_to"]))
        if row["listed_amount"] and quoted.get("amount"):
            result["price_matches"] = int(str(row["listed_amount"]) == str(quoted["amount"]))
        return result
    if 200 <= status < 300:
        # Listed as paid, but served for free. Not dangerous, but the listing is wrong.
        return {**result, "outcome": "free"}
    return {**result, "outcome": classify_status(status, resource), "error": f"HTTP {status}"}


TEMPLATE = re.compile(r"/:[A-Za-z_]|\{[A-Za-z_]+\}|<[A-Za-z_]+>")


def is_template(url):
    """Listings like /price/:address are route patterns, not callable URLs."""
    return bool(TEMPLATE.search(url or ""))


def classify_status(status, url=""):
    """Non-402, non-2xx answers, judged fairly:
    400/422  the server is up and wants input before quoting a price
    404/410  gone, unless the listing is a route pattern we can't fill in
    401/403  on a route pattern: the server is refusing a made-up id (e.g. /inboxes/:inbox_id),
             which says nothing about whether the real route works
    429      we were rate limited: says nothing about the endpoint
    3xx      the server is up and points somewhere else
    """
    if status in (400, 422):
        return "needs_input"
    if status in (404, 410):
        return "inconclusive" if is_template(url) else "not_found"
    if status in (401, 403) and is_template(url):
        return "inconclusive"
    if status == 429:
        return "inconclusive"
    if 300 <= status < 400:
        return "redirect"
    return "http_error"


TLS_CLIENT_ERRORS = ("PROTOCOL_VERSION", "TLSV1_ALERT_ACCESS_DENIED", "UNSUPPORTED_PROTOCOL",
                     "WRONG_VERSION_NUMBER", "SSLV3_ALERT_HANDSHAKE_FAILURE")


def _client_tls_problem(exc):
    text = str(exc)
    return any(marker in text for marker in TLS_CLIENT_ERRORS)


COLUMNS = (
    "resource", "ts", "outcome", "status_code", "latency_ms", "protocol",
    "quoted_amount", "quoted_usd", "quoted_network", "quoted_asset", "quoted_pay_to",
    "payto_matches", "price_matches", "error",
)


def run(conn=None, limit=None, log=print):
    """Probe the endpoints that have gone longest without a check (never-checked first),
    taking at most HOST_CAP_PER_CYCLE from any one host."""
    conn = conn or db.connect()
    batch = limit or config.PROBE_BATCH
    rows = conn.execute(
        """
        WITH last AS (SELECT resource, MAX(ts) AS last_ts FROM probes GROUP BY resource),
        ranked AS (
            SELECT e.resource, e.method, e.pay_to, e.listed_amount, l.last_ts,
                   ROW_NUMBER() OVER (
                       PARTITION BY e.host
                       ORDER BY l.last_ts IS NOT NULL, l.last_ts, RANDOM()) AS rn
            FROM endpoints e LEFT JOIN last l USING (resource)
            WHERE e.delisted = 0
        )
        SELECT resource, method, pay_to, listed_amount FROM ranked
        WHERE rn <= ?
        ORDER BY last_ts IS NOT NULL, last_ts, RANDOM()
        LIMIT ?
        """,
        (config.HOST_CAP_PER_CYCLE, batch),
    ).fetchall()
    rows = [dict(r) for r in rows]
    # Endpoints someone asked for alerts on are checked every cycle, on top of the batch.
    from . import alerts
    picked = {r["resource"] for r in rows}
    watched = [w for w in alerts.watched_resources(conn) if w not in picked]
    for res in watched:
        row = conn.execute("SELECT resource, method, pay_to, listed_amount FROM endpoints "
                           "WHERE resource=? AND delisted=0", (res,)).fetchone()
        if row:
            rows.append(dict(row))
    random.shuffle(rows)  # spread load across hosts
    total = conn.execute("SELECT COUNT(*) FROM endpoints WHERE delisted=0").fetchone()[0]
    _host_fails.clear()
    log(f"Probing {len(rows):,} of {total:,} listed endpoints ({len(watched)} watched), oldest-checked first, "
        f"max {config.HOST_CAP_PER_CYCLE} per host ({config.PROBE_WORKERS} at a time, never paying)")
    if not getattr(ssl, "HAS_TLSv1_3", False):
        log(f"  Note: this Python ({ssl.OPENSSL_VERSION}) can't do TLS 1.3, so some endpoints "
            "will show as inconclusive. Python from python.org fixes that.")

    counts = defaultdict(int)
    done = 0
    with ThreadPoolExecutor(max_workers=config.PROBE_WORKERS) as pool:
        futures = [pool.submit(probe_one, r) for r in rows]
        for fut in as_completed(futures):
            res = fut.result()
            counts[res["outcome"]] += 1
            conn.execute(
                f"INSERT INTO probes ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [res.get(c) for c in COLUMNS],
            )
            done += 1
            if done % 500 == 0:
                conn.commit()
            if done % 2500 == 0:
                log(f"  probed {done:,}/{len(rows):,}")
    conn.commit()
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    log(f"Probe done: {summary or 'nothing to probe'}")
    return dict(counts)
