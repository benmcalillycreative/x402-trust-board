"""SQLite storage. One file, no server."""

import json
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    resource            TEXT PRIMARY KEY,
    host                TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,      -- last time it appeared in a catalog crawl
    delisted            INTEGER DEFAULT 0,  -- 1 if it dropped out of the catalog
    description         TEXT,
    service_name        TEXT,
    tags                TEXT,               -- JSON list
    x402_version        INTEGER,
    method              TEXT,
    network             TEXT,
    asset               TEXT,
    pay_to              TEXT,
    listed_amount       TEXT,               -- atomic units, as listed in the catalog
    listed_usd          REAL,
    curated             INTEGER DEFAULT 0,
    l30_calls           INTEGER,
    l30_payers          INTEGER,
    last_called_at      TEXT,
    catalog_json        TEXT,               -- the raw catalog entry, for later re-analysis
    protocol            TEXT DEFAULT 'x402' -- x402 (Coinbase Bazaar) | mpp (MPP directory)
);

CREATE TABLE IF NOT EXISTS probes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    resource            TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    outcome             TEXT NOT NULL,      -- healthy | free | broken_402 | http_error | unreachable | blocked
    status_code         INTEGER,
    latency_ms          INTEGER,
    protocol            TEXT,               -- v1 | v2 | none
    quoted_amount       TEXT,
    quoted_usd          REAL,
    quoted_network      TEXT,
    quoted_asset        TEXT,
    quoted_pay_to       TEXT,
    payto_matches       INTEGER,            -- 1/0/NULL vs. catalog listing
    price_matches       INTEGER,            -- 1/0/NULL vs. catalog listing
    error               TEXT
);
CREATE INDEX IF NOT EXISTS probes_resource_ts ON probes(resource, ts);

CREATE TABLE IF NOT EXISTS crawls (
    ts                  TEXT PRIMARY KEY,
    total_reported      INTEGER,
    fetched             INTEGER,
    new_endpoints       INTEGER,
    delisted            INTEGER
);

CREATE TABLE IF NOT EXISTS scores (
    resource            TEXT PRIMARY KEY,
    computed_at         TEXT NOT NULL,
    probes              INTEGER,
    uptime              REAL,
    p50_latency_ms      INTEGER,
    score               INTEGER,
    grade               TEXT,
    flags               TEXT                -- JSON list
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after a database was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(endpoints)")}
    if "protocol" not in cols:  # v0.7: endpoints from more than one catalog
        conn.execute("ALTER TABLE endpoints ADD COLUMN protocol TEXT DEFAULT 'x402'")
        conn.commit()
    if "source" not in cols:  # v0.9.4: x402 listings from Circle's marketplace as well as the Bazaar
        conn.execute("ALTER TABLE endpoints ADD COLUMN source TEXT DEFAULT 'bazaar'")
        conn.execute("ALTER TABLE endpoints ADD COLUMN circle_seen TEXT")
        conn.commit()
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(probes)")}
    if "quoted_example" not in pcols:  # v0.9.4: the seller marked the quoted offer as an example
        conn.execute("ALTER TABLE probes ADD COLUMN quoted_example INTEGER")
        conn.commit()


def dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
