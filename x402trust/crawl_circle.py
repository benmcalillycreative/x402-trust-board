"""Circle's Agent Marketplace: a second x402 catalog.

Circle publishes the x402 resources it knows about at a public discovery endpoint
(GET /v2/x402/discovery/resources, paged with limit/offset). The payment requirements
are ordinary x402 `accepts` entries, so a listing here is probed and graded exactly
like a Bazaar listing. What differs is the metadata: the seller's name, website and
category sit under metadata.provider.

A resource that is also in the Bazaar stays a Bazaar row; we only note that Circle
lists it too (circle_seen). A resource only Circle lists becomes a row with
source='circle', and only this crawl can delist it.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, crawl, db

PAGE = 200  # the API's maximum


def fetch_page(offset, limit=None):
    query = urllib.parse.urlencode({"limit": limit or PAGE, "offset": offset})
    req = urllib.request.Request(
        f"{config.CIRCLE_CATALOG_URL}?{query}",
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def iter_catalog(log=print):
    offset, total = 0, None
    while True:
        page = fetch_page(offset)
        items = page.get("items") or page.get("resources") or page.get("data") or []
        pag = page.get("pagination") or {}
        total = pag.get("total", total)
        for item in items:
            yield item, total
        offset += len(items)
        if not items or (total is not None and offset >= total) or (total is None and len(items) < PAGE):
            log(f"  circle: {offset:,}" + (f" / {total:,}" if total else ""))
            break
        time.sleep(config.CRAWL_DELAY)


def to_bazaar_shape(item):
    """Rewrite a Circle listing into the shape crawl.upsert reads."""
    meta = item.get("metadata") or {}
    provider = meta.get("provider") or {}
    method = (meta.get("method") or "GET").upper()
    out = {
        "resource": item.get("resource"),
        "type": item.get("type", "http"),
        "x402Version": item.get("x402Version"),
        "accepts": item.get("accepts") or [],
        "description": meta.get("description") or provider.get("description"),
        "serviceName": provider.get("name"),
        "tags": [t for t in (provider.get("tags") or []) if isinstance(t, str)]
                + ([provider["category"]] if isinstance(provider.get("category"), str) else []),
        "extensions": {"bazaar": {"info": {"input": {"method": method}}}},
        "circle": {
            "provider": provider,
            "path": meta.get("path"),
            "supportsVanillax402": meta.get("supportsVanillax402"),
            "supportsCircleGateway": meta.get("supportsCircleGateway"),
        },
    }
    return out


def run(conn=None, log=print):
    conn = conn or db.connect()
    ts = db.now()
    fetched = new = also = 0
    seen = set()
    log(f"Crawling {config.CIRCLE_CATALOG_URL}")
    for item, _total in iter_catalog(log):
        if (item.get("type") or "http") != "http":
            continue
        item = to_bazaar_shape(item)
        resource = item["resource"]
        if not resource:
            continue
        fetched += 1
        seen.add(resource)
        row = conn.execute(
            "SELECT COALESCE(source, 'bazaar') src, COALESCE(protocol, 'x402') proto, delisted "
            "FROM endpoints WHERE resource=?", (resource,)).fetchone()
        if row and (row["src"] != "circle" or row["proto"] != "x402") and not row["delisted"]:
            # Already tracked from another catalog: just remember Circle lists it too.
            conn.execute("UPDATE endpoints SET circle_seen=? WHERE resource=?", (ts, resource))
            also += 1
            continue
        if crawl.upsert(conn, item, ts):
            new += 1
        conn.execute("UPDATE endpoints SET source='circle', circle_seen=? WHERE resource=?", (ts, resource))
        if fetched % 500 == 0:
            conn.commit()

    delisted = 0
    if seen:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS circle_seen_now (resource TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM circle_seen_now")
        conn.executemany("INSERT OR IGNORE INTO circle_seen_now VALUES (?)", [(r,) for r in seen])
        grace = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        delisted = conn.execute(
            "UPDATE endpoints SET delisted=1 WHERE delisted=0 AND source='circle' AND last_seen < ? "
            "AND resource NOT IN (SELECT resource FROM circle_seen_now)", (grace,)
        ).rowcount
    conn.commit()
    log(f"Circle crawl done: {fetched} listed, {new} new, {also} already tracked, {delisted} delisted")
    return {"fetched": fetched, "new": new, "also": also, "delisted": delisted}
