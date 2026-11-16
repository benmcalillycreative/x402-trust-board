"""Pull the whole Bazaar catalog into the endpoints table."""

import json
import time
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from . import config, db
from .parse import pick_requirement, requirement_fields


def fetch_page(offset, limit):
    query = urllib.parse.urlencode({"type": "http", "limit": limit, "offset": offset})
    req = urllib.request.Request(
        f"{config.DISCOVERY_URL}?{query}",
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
        page = fetch_page(offset, config.CRAWL_PAGE_SIZE)
        items = page.get("items") or []
        pag = page.get("pagination") or {}
        total = pag.get("total", total)
        for item in items:
            yield item, total
        offset += len(items)
        done = not items or (total is not None and offset >= total)
        if offset % 5000 < len(items) or done:
            log(f"  catalog: {offset:,}" + (f" / {total:,}" if total else ""))
        if done:
            break
        time.sleep(config.CRAWL_DELAY)


def upsert(conn, item, ts):
    resource = item.get("resource")
    if not resource:
        return False
    req = requirement_fields(pick_requirement(item.get("accepts") or []))
    bazaar = (item.get("extensions") or {}).get("bazaar") or {}
    method = (((bazaar.get("info") or {}).get("input") or {}).get("method") or "GET").upper()
    quality = item.get("quality") or {}

    existing = conn.execute("SELECT 1 FROM endpoints WHERE resource=?", (resource,)).fetchone()
    values = dict(
        resource=resource,
        host=urlsplit(resource).hostname,
        last_seen=ts,
        description=item.get("description"),
        service_name=item.get("serviceName"),
        tags=db.dumps(item.get("tags") or []),
        x402_version=item.get("x402Version"),
        method=method,
        network=req.get("network"),
        asset=req.get("asset"),
        pay_to=req.get("pay_to"),
        listed_amount=req.get("amount"),
        listed_usd=req.get("usd"),
        curated=1 if item.get("curated") else 0,
        l30_calls=quality.get("l30DaysTotalCalls"),
        l30_payers=quality.get("l30DaysUniquePayers"),
        last_called_at=quality.get("lastCalledAt"),
        catalog_json=db.dumps(item),
        protocol="x402",
    )
    if existing:
        cols = ", ".join(f"{k}=:{k}" for k in values if k != "resource")
        conn.execute(f"UPDATE endpoints SET {cols}, delisted=0 WHERE resource=:resource", values)
        return False
    values["first_seen"] = ts
    cols = ", ".join(values)
    conn.execute(
        f"INSERT INTO endpoints ({cols}) VALUES ({', '.join(':' + k for k in values)})", values
    )
    return True


def due(conn) -> bool:
    """True if the last successful crawl is older than CRAWL_EVERY_HOURS."""
    from datetime import datetime, timedelta, timezone
    row = conn.execute("SELECT MAX(ts) FROM crawls WHERE fetched > 0").fetchone()
    if not row or not row[0]:
        return True
    last = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(hours=config.CRAWL_EVERY_HOURS)


def run(conn=None, log=print):
    conn = conn or db.connect()
    ts = db.now()
    fetched = new = 0
    total = None
    seen = set()
    log(f"Crawling {config.DISCOVERY_URL}")
    for item, total in iter_catalog(log):
        fetched += 1
        seen.add(item.get("resource"))
        if upsert(conn, item, ts):
            new += 1
        if fetched % 500 == 0:
            conn.commit()
    # Anything we didn't see this crawl has dropped out of the catalog.
    # (Only trust that if the crawl actually returned something.)
    delisted = 0
    if seen:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen (resource TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM seen")
        conn.executemany("INSERT OR IGNORE INTO seen VALUES (?)", [(r,) for r in seen if r])
        # The catalog shifts while we page through it, so a listing can be skipped in one
        # crawl without having left. Only call it delisted once it's been missing ~2 days.
        from datetime import datetime, timedelta, timezone
        grace = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        delisted = conn.execute(
            "UPDATE endpoints SET delisted=1 WHERE delisted=0 AND last_seen < ? "
            "AND COALESCE(protocol, 'x402') = 'x402' "
            "AND resource NOT IN (SELECT resource FROM seen)", (grace,)
        ).rowcount
    conn.execute(
        "INSERT OR REPLACE INTO crawls VALUES (?,?,?,?,?)", (ts, total, fetched, new, delisted)
    )
    conn.commit()
    log(f"Crawl done: {fetched} listed, {new} new, {delisted} delisted")
    return {"fetched": fetched, "new": new, "delisted": delisted, "total": total}
