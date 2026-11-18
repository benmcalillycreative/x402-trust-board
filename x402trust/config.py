"""Settings. Every value can be overridden with an environment variable."""

import os
from pathlib import Path


def _load_settings_file():
    """Read settings.env next to the package, so manual commands see the same settings
    as the schedule. Values already in the environment win."""
    f = Path(__file__).resolve().parent.parent / "settings.env"
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = os.path.expandvars(value.strip().strip('"').strip("'"))
        if value:
            os.environ.setdefault(key.strip(), value)


_load_settings_file()

# Where the catalog comes from. CDP's discovery API is public (no key needed).
DISCOVERY_URL = os.environ.get(
    "X402T_DISCOVERY_URL",
    "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources",
)

# Where everything is stored.
DATA_DIR = Path(os.environ.get("X402T_DATA_DIR", Path.home() / "x402-trust-data"))
DB_PATH = DATA_DIR / "trust.db"
REPORT_DIR = DATA_DIR / "reports"

# Identify the crawler honestly so endpoint owners know who is knocking.
CONTACT = os.environ.get("X402T_CONTACT", "")
USER_AGENT = "x402trust/0.9 (independent endpoint monitor; never pays; {})".format(
    CONTACT or "contact not set"
)

# Probing behaviour.
# The catalog is large (66K+ listings), so each cycle checks the endpoints that have
# gone longest without a check, up to PROBE_BATCH. At 4 cycles a day that is roughly
# one check per endpoint per day.
PROBE_BATCH = int(os.environ.get("X402T_PROBE_BATCH", "15000"))
# One host can list tens of thousands of near-identical endpoints. Cap how many of a
# single host's endpoints get checked per cycle so real services aren't crowded out;
# big hosts are sampled instead (100 per cycle is still 400 a day).
HOST_CAP_PER_CYCLE = int(os.environ.get("X402T_HOST_CAP", "100"))
PROBE_TIMEOUT = float(os.environ.get("X402T_PROBE_TIMEOUT", "10"))
PROBE_WORKERS = int(os.environ.get("X402T_PROBE_WORKERS", "16"))
PER_HOST_LIMIT = int(os.environ.get("X402T_PER_HOST_LIMIT", "2"))
CRAWL_PAGE_SIZE = 100
CRAWL_DELAY = float(os.environ.get("X402T_CRAWL_DELAY", "0.5"))
# A full crawl is ~660 requests to Coinbase; once a day is plenty and polite.
CRAWL_EVERY_HOURS = float(os.environ.get("X402T_CRAWL_EVERY_HOURS", "24"))

# The leaderboard page shows the top endpoints plus every red flag (each capped);
# scores.json always has everything.
REPORT_MAX_ROWS = int(os.environ.get("X402T_REPORT_MAX_ROWS", "500"))

# Safety: refuse to probe private/home-network addresses unless explicitly allowed
# (a malicious listing could otherwise point the prober at your router).
ALLOW_PRIVATE = os.environ.get("X402T_ALLOW_PRIVATE", "") == "1"

# Scoring.
SCORE_WINDOW_DAYS = int(os.environ.get("X402T_SCORE_WINDOW_DAYS", "7"))
MIN_PROBES_FOR_GRADE = int(os.environ.get("X402T_MIN_PROBES", "3"))

# Alerts (email). For Gmail: X402T_SMTP_USER is your address and X402T_SMTP_PASS is an
# app password from https://myaccount.google.com/apppasswords (needs 2-Step Verification).
SMTP_HOST = os.environ.get("X402T_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("X402T_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("X402T_SMTP_USER", "")
SMTP_PASS = os.environ.get("X402T_SMTP_PASS", "")
ALERT_FROM = os.environ.get("X402T_ALERT_FROM", "")
ALERT_FROM_NAME = os.environ.get("X402T_ALERT_FROM_NAME", "Settle Watch")
# Self-serve sign-ups (see signups.py and worker/). API_URL is the deployed Worker, e.g.
# https://x402-trust-check.<you>.workers.dev; ALERTS_TOKEN is the same secret the Worker has.
API_URL = os.environ.get("X402T_API_URL", "").rstrip("/")
ALERTS_TOKEN = os.environ.get("X402T_ALERTS_TOKEN", "")
# What the board's own pages call (sign-up forms, the listing check). Defaults to the public check API, so the forms
# work even before API_URL/ALERTS_TOKEN are set here; sign-ups then wait in the Worker's queue until they are.
PUBLIC_API = os.environ.get("X402T_PUBLIC_API", API_URL or "https://api.settlewatch.com").rstrip("/")
# Coinbase features Bazaar listings with at least this availability over 30 days (platform-measured).
CURATION_BAR = 0.99
AVAILABILITY_DAYS = 30

# Payout verification (see payout.py). DNS lookups go over HTTPS because the standard
# library has no DNS client; smart-wallet signatures are checked on Base.
DOH_URL = os.environ.get("X402T_DOH_URL", "https://cloudflare-dns.com/dns-query")
BASE_RPC_URL = os.environ.get("X402T_BASE_RPC_URL", "https://mainnet.base.org")

# The MPP (Machine Payments Protocol) service directory. Set to "" to turn MPP off.
MPP_CATALOG_URL = os.environ.get("X402T_MPP_CATALOG_URL", "https://mpp.dev/api/services")
# Circle's Agent Marketplace lists x402 sellers too, many of them not in the Bazaar. Empty turns it off.
CIRCLE_CATALOG_URL = os.environ.get(
    "X402T_CIRCLE_CATALOG_URL", "https://api.circle.com/v2/x402/discovery/resources")

# On-chain payment counting (see chain.py): USDC transfers to sellers' Base addresses.
# A free public RPC works but is slow; an Alchemy/QuickNode Base URL in X402T_BASE_RPC_URL is faster.
CHAIN_ENABLED = os.environ.get("X402T_CHAIN", "1") == "1"
CHAIN_WINDOW_DAYS = int(os.environ.get("X402T_CHAIN_WINDOW_DAYS", "30"))
CHAIN_BLOCK_STEP = int(os.environ.get("X402T_CHAIN_BLOCK_STEP", "5000"))
CHAIN_GROUP = int(os.environ.get("X402T_CHAIN_GROUP", "200"))
CHAIN_MAX_SECONDS = int(os.environ.get("X402T_CHAIN_MAX_SECONDS", "900"))
# Free public Base RPCs rate-limit hard, so the payment check spreads its requests over several
# and paces itself. X402T_BASE_RPC_URL (if set) is tried first; add more with X402T_BASE_RPC_URLS.
CHAIN_RPC_URLS = [u.strip() for u in (
    ([os.environ["X402T_BASE_RPC_URL"]] if os.environ.get("X402T_BASE_RPC_URL") else [])
    + os.environ.get("X402T_BASE_RPC_URLS",
                     "https://mainnet.base.org,https://base-rpc.publicnode.com,https://base.drpc.org,"
                     "https://1rpc.io/base").split(",")) if u.strip()]
CHAIN_MAX_WAIT = int(os.environ.get("X402T_CHAIN_MAX_WAIT", "300"))  # seconds to sit out rate limits before stopping
CHAIN_MIN_INTERVAL = float(os.environ.get("X402T_CHAIN_MIN_INTERVAL", "0.3"))  # seconds between requests
BASE_BLOCK_SECONDS = 2
# Solana USDC payments (chain_sol.py). Public RPCs are slow and strict, so each run is capped.
SOL_ENABLED = os.environ.get("X402T_SOLANA", "1") == "1"
SOL_RPC_URLS = [u.strip() for u in os.environ.get(
    "X402T_SOLANA_RPC_URLS", "https://api.mainnet-beta.solana.com,https://solana-rpc.publicnode.com").split(",")
    if u.strip()]
SOL_MIN_INTERVAL = float(os.environ.get("X402T_SOLANA_MIN_INTERVAL", "0.25"))
SOL_MAX_SECONDS = int(os.environ.get("X402T_SOLANA_MAX_SECONDS", "600"))
SOL_MAX_TX = int(os.environ.get("X402T_SOLANA_MAX_TX", "20000"))  # per token account per 30 days
