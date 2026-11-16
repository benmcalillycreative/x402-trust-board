"""Pull the MPP (Machine Payments Protocol) service directory into the endpoints table.

The directory at mpp.dev publishes every listed service and its paid endpoints
as one JSON file: method, path, and the advertised price per payment method.
It doesn't list who gets paid; that only shows up in each endpoint's live 402,
which the prober records. MPP listings are stored next to x402 ones with
protocol='mpp' and their terms translated into the same "accepts" shape.
"""

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import config, db
from .parse import TEMPO_CHAIN_ID, to_usd


def fetch():
    req = urllib.request.Request(config.MPP_CATALOG_URL, headers={
        "User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def listing(service, endpoint):
    """One paid endpoint -> a catalog entry shaped like a Bazaar item."""
    pay = endpoint.get("payment")
    if not isinstance(pay, dict):
        return None  # free endpoint: nothing to check
    base = (service.get("serviceUrl") or service.get("url") or "").rstrip("/")
    path = endpoint.get("path") or ""
    if not base.startswith(("http://", "https://")) or not path.startswith("/"):
        return None
    method = pay.get("method")
    dynamic = bool(pay.get("dynamic")) or pay.get("amount") is None
    accept = {
        "scheme": "upto" if dynamic or pay.get("intent") == "session" else "exact",
        # Tempo is a known chain. "evm" can be any chain and the directory doesn't say which,
        # so leave it blank rather than guess (prices then just aren't compared).
        "network": f"eip155:{TEMPO_CHAIN_ID}" if method == "tempo" else (None if method == "evm" else method),
        "amount": None if dynamic else str(pay.get("amount")),
        "asset": pay.get("currency"),
        "payTo": None,  # MPP listings don't name a recipient
        "mppMethod": method,
        "mppIntent": pay.get("intent"),
        "amountHint": pay.get("amountHint"),
    }
    return {
        "resource": base + path,
        "protocol": "mpp",
        "method": (endpoint.get("method") or "GET").upper(),
        "serviceName": service.get("name"),
        "serviceId": service.get("id"),
        "description": endpoint.get("description") or service.get("description"),
        "tags": (service.get("categories") or []) + (service.get("tags") or []),
        "integration": service.get("integration"),
        "status": service.get("status"),
        "methods": sorted((service.get("methods") or {}).keys()),
        "accepts": [accept],
    }


def run(conn=None, log=print):
    conn = conn or db.connect()
    ts = db.now()
    data = fetch()
    services = data.get("services") or []
    seen, new, skipped = set(), 0, 0
    for svc in services:
        for ep in svc.get("endpoints") or []:
            item = listing(svc, ep)
            if not item or item["resource"] in seen:
                continue
            seen.add(item["resource"])
            acc = item["accepts"][0]
            row = conn.execute("SELECT protocol FROM endpoints WHERE resource=?", (item["resource"],)).fetchone()
            if row and (row[0] or "x402") != "mpp":
                skipped += 1  # already tracked from the x402 Bazaar; keep that listing
                continue
            values = dict(
                resource=item["resource"], host=urlsplit(item["resource"]).hostname, last_seen=ts,
                description=item["description"], service_name=item["serviceName"],
                tags=db.dumps(item["tags"]), x402_version=None, method=item["method"],
                network=acc["network"], asset=acc["asset"], pay_to=None,
                listed_amount=acc["amount"], listed_usd=to_usd(acc["amount"], acc["asset"]),
                curated=0, l30_calls=None, l30_payers=None, last_called_at=None,
                catalog_json=db.dumps(item), protocol="mpp",
            )
            if row:
                cols = ", ".join(f"{k}=:{k}" for k in values if k != "resource")
                conn.execute(f"UPDATE endpoints SET {cols}, delisted=0 WHERE resource=:resource", values)
            else:
                values["first_seen"] = ts
                cols = ", ".join(values)
                conn.execute(f"INSERT INTO endpoints ({cols}) VALUES ({', '.join(':' + k for k in values)})",
                             values)
                new += 1
    delisted = 0
    if seen:
        grace = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS mpp_seen (resource TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM mpp_seen")
        conn.executemany("INSERT OR IGNORE INTO mpp_seen VALUES (?)", [(r,) for r in seen])
        delisted = conn.execute(
            "UPDATE endpoints SET delisted=1 WHERE protocol='mpp' AND delisted=0 AND last_seen < ? "
            "AND resource NOT IN (SELECT resource FROM mpp_seen)", (grace,)).rowcount
    conn.commit()
    log(f"MPP directory: {len(services)} services, {len(seen):,} paid endpoints, {new} new, {delisted} delisted"
        + (f" ({skipped} already tracked from the x402 Bazaar)" if skipped else ""))
    return {"services": len(services), "endpoints": len(seen), "new": new, "delisted": delisted}
